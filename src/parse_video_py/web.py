import dataclasses
import json
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi_mcp import FastApiMCP
import httpx
from sqlalchemy import (
    BigInteger,
    Column,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    insert,
    select,
)

from parse_video_py import VideoSource, parse_video_id, parse_video_share_url
from parse_video_py.utils import create_async_client, extract_url


_MATERIAL_DB_PATH = Path(os.getenv("MATERIAL_DB_PATH", "data/materials.db"))
_MYSQL_HOST = os.getenv("MYSQL_HOST", "").strip()
_MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
_MYSQL_USER = os.getenv("MYSQL_USER", "").strip()
_MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
_MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "").strip()
_MYSQL_CHARSET = os.getenv("MYSQL_CHARSET", "utf8mb4")
_USE_MYSQL = bool(_MYSQL_HOST and _MYSQL_USER and _MYSQL_DATABASE)
_MATERIAL_RECORD_TTL_SECONDS = int(
    os.getenv(
        "MATERIAL_RECORD_TTL_SECONDS",
        os.getenv("MATERIAL_CACHE_TTL_SECONDS", "3600"),
    )
)
_DOWNLOAD_TIMEOUT_SECONDS = float(os.getenv("MATERIAL_DOWNLOAD_TIMEOUT_SECONDS", "120"))

_metadata = MetaData()
_material_records = Table(
    "material_records",
    _metadata,
    Column("material_id", String(64), primary_key=True),
    Column("resource_type", String(20), nullable=False, default=""),
    Column("title", String(255), nullable=False, default=""),
    Column("video_url", Text, nullable=True),
    Column("cover_url", Text, nullable=True),
    Column("music_url", Text, nullable=True),
    Column("images_json", Text, nullable=True),
    Column("author_json", Text, nullable=True),
    Column("payload_json", Text, nullable=True),
    Column("created_at", BigInteger, nullable=False),
    Column("expires_at", BigInteger, nullable=False, index=True),
)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _create_app() -> FastAPI:
    docs_enabled = not _env_flag("DISABLE_DOCS")
    return FastAPI(
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )


app = _create_app()

mcp = FastApiMCP(app)
mcp.mount_http()


def _api_response(code: int, msg: str, data: Any | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"code": code, "msg": msg}
    if data is not None:
        response["data"] = data
    return response


def _build_database_url() -> str:
    if _USE_MYSQL:
        user = quote_plus(_MYSQL_USER)
        password = quote_plus(_MYSQL_PASSWORD)
        host = _MYSQL_HOST
        database = quote_plus(_MYSQL_DATABASE)
        return (
            f"mysql+pymysql://{user}:{password}@{host}:{_MYSQL_PORT}/{database}"
            f"?charset={_MYSQL_CHARSET}"
        )

    _MATERIAL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{_MATERIAL_DB_PATH.as_posix()}"


_engine = create_engine(
    _build_database_url(),
    pool_pre_ping=_USE_MYSQL,
    future=True,
)


def _init_material_db() -> None:
    _metadata.create_all(_engine)


def _cleanup_material_records() -> None:
    now = int(time.time())
    with _engine.begin() as conn:
        conn.execute(
            delete(_material_records).where(_material_records.c.expires_at < now)
        )


def _store_material(data: dict[str, Any]) -> str:
    _init_material_db()
    _cleanup_material_records()
    material_id = "mat_" + uuid.uuid4().hex
    now = int(time.time())
    expires_at = now + _MATERIAL_RECORD_TTL_SECONDS
    resource_type = "video" if data.get("video_url") else "image"
    images = data.get("images") if isinstance(data.get("images"), list) else []
    author = data.get("author") if isinstance(data.get("author"), dict) else {}

    with _engine.begin() as conn:
        conn.execute(
            insert(_material_records).values(
                material_id=material_id,
                resource_type=resource_type,
                title=str(data.get("title") or ""),
                video_url=str(data.get("video_url") or ""),
                cover_url=str(data.get("cover_url") or ""),
                music_url=str(data.get("music_url") or ""),
                images_json=json.dumps(images, ensure_ascii=False),
                author_json=json.dumps(author, ensure_ascii=False),
                payload_json=json.dumps(data, ensure_ascii=False),
                created_at=now,
                expires_at=expires_at,
            )
        )
    return material_id


def _attach_material_id(data: dict[str, Any]) -> dict[str, Any]:
    material_id = _store_material(data)
    data["id"] = material_id
    data["material_id"] = material_id
    return data


def _get_material(material_id: str) -> dict[str, Any]:
    _init_material_db()
    _cleanup_material_records()
    with _engine.begin() as conn:
        row = conn.execute(
            select(_material_records).where(
                _material_records.c.material_id == material_id
            )
        ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Material record not found")
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    try:
        images = json.loads(row["images_json"] or "[]")
    except json.JSONDecodeError:
        images = []
    try:
        author = json.loads(row["author_json"] or "{}")
    except json.JSONDecodeError:
        author = {}

    payload.update(
        {
            "id": row["material_id"],
            "material_id": row["material_id"],
            "title": row["title"],
            "video_url": row["video_url"],
            "cover_url": row["cover_url"],
            "music_url": row["music_url"],
            "images": images,
            "author": author,
        }
    )
    return payload


def _get_image_url(images: list[Any], index: int, prefer_live_photo: bool = False) -> str:
    if index < 0 or index >= len(images):
        raise HTTPException(status_code=404, detail="Image index not found")
    image = images[index]
    if isinstance(image, dict):
        if prefer_live_photo:
            return str(image.get("live_photo_url") or image.get("url") or "")
        return str(image.get("url") or image.get("live_photo_url") or "")
    return str(image or "")


def _resolve_material_url(material: dict[str, Any], resource_type: str, index: int) -> str:
    if resource_type == "image":
        resource_url = _get_image_url(material.get("images") or [], index)
    elif resource_type == "live_photo":
        resource_url = _get_image_url(
            material.get("images") or [], index, prefer_live_photo=True
        )
    else:
        resource_url = str(material.get("video_url") or "")
    if not resource_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=404, detail="Material resource url not found")
    return resource_url


def _content_type_for(resource_type: str, source_content_type: str | None) -> str:
    if source_content_type:
        return source_content_type.split(";")[0].strip()
    if resource_type == "image":
        return "image/jpeg"
    return "video/mp4"


def _download_filename(resource_type: str, content_type: str) -> str:
    if resource_type == "image":
        if "png" in content_type:
            return "material.png"
        if "webp" in content_type:
            return "material.webp"
        return "material.jpg"
    return "material.mp4"


def _build_auth_dependency() -> list[Depends]:
    """Build Basic Auth dependencies when username and password are configured."""
    basic_auth_username = os.getenv("PARSE_VIDEO_USERNAME")
    basic_auth_password = os.getenv("PARSE_VIDEO_PASSWORD")

    if not (basic_auth_username and basic_auth_password):
        return []

    security = HTTPBasic()

    def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
        correct_username = secrets.compare_digest(
            credentials.username, basic_auth_username
        )
        correct_password = secrets.compare_digest(
            credentials.password, basic_auth_password
        )
        if not (correct_username and correct_password):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect username or password",
                headers={"WWW-Authenticate": "Basic"},
            )
        return credentials

    return [Depends(verify_credentials)]


_auth_dependency = _build_auth_dependency()


@app.on_event("startup")
async def startup_event():
    _init_material_db()
    _cleanup_material_records()


@app.get("/health", include_in_schema=False)
async def health_check():
    return {"status": "ok"}


@app.get("/video/share/url/parse", dependencies=_auth_dependency)
async def share_url_parse(url: str):
    video_share_url = extract_url(url)
    if video_share_url is None:
        return _api_response(400, "未检测到有效的分享链接")

    try:
        video_info = await parse_video_share_url(video_share_url)
        data = _attach_material_id(dataclasses.asdict(video_info))
        return _api_response(200, "解析成功", data)
    except Exception as err:
        return _api_response(500, str(err))


@app.get("/video/id/parse", dependencies=_auth_dependency)
async def video_id_parse(source: VideoSource, video_id: str):
    try:
        video_info = await parse_video_id(source, video_id)
        data = _attach_material_id(dataclasses.asdict(video_info))
        return _api_response(200, "解析成功", data)
    except Exception as err:
        return _api_response(500, str(err))


@app.get("/material/download", dependencies=_auth_dependency)
async def material_download(id: str, type: str = "video", index: int = 0):
    resource_type = type.strip().lower()
    if resource_type not in {"video", "image", "live_photo"}:
        raise HTTPException(status_code=400, detail="Unsupported material type")

    material = _get_material(id)
    resource_url = _resolve_material_url(material, resource_type, index)

    client = create_async_client(
        follow_redirects=True,
        timeout=httpx.Timeout(
            connect=15.0,
            read=_DOWNLOAD_TIMEOUT_SECONDS,
            write=15.0,
            pool=15.0,
        ),
    )

    try:
        request = client.build_request(
            "GET",
            resource_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                    "Mobile/15E148 Safari/604.1"
                ),
                "Accept": "*/*",
            },
        )
        response = await client.send(request, stream=True)
        if response.status_code < 200 or response.status_code >= 300:
            await response.aclose()
            await client.aclose()
            raise HTTPException(
                status_code=502,
                detail=f"Upstream download failed: {response.status_code}",
            )
    except HTTPException:
        raise
    except Exception as err:
        await client.aclose()
        raise HTTPException(status_code=502, detail=str(err)) from err

    content_type = _content_type_for(
        resource_type, response.headers.get("content-type")
    )
    filename = _download_filename(resource_type, content_type)

    async def body_iterator():
        try:
            async for chunk in response.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(
        body_iterator(),
        media_type=content_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


mcp.setup_server()
