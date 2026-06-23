import dataclasses
import os
import secrets

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi_mcp import FastApiMCP

from parse_video_py import VideoSource, parse_video_id, parse_video_share_url
from parse_video_py.utils import extract_url

app = FastAPI()

mcp = FastApiMCP(app)
mcp.mount_http()


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


@app.get("/video/share/url/parse", dependencies=_auth_dependency)
async def share_url_parse(url: str):
    video_share_url = extract_url(url)
    if video_share_url is None:
        return {
            "code": 400,
            "msg": "No valid share URL detected",
        }

    try:
        video_info = await parse_video_share_url(video_share_url)
        return {
            "code": 200,
            "msg": "Parse successful",
            "data": dataclasses.asdict(video_info),
        }
    except Exception as err:
        return {
            "code": 500,
            "msg": str(err),
        }


@app.get("/video/id/parse", dependencies=_auth_dependency)
async def video_id_parse(source: VideoSource, video_id: str):
    try:
        video_info = await parse_video_id(source, video_id)
        return {
            "code": 200,
            "msg": "Parse successful",
            "data": dataclasses.asdict(video_info),
        }
    except Exception as err:
        return {
            "code": 500,
            "msg": str(err),
        }


mcp.setup_server()
