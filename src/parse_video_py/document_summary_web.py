"""Authenticated document upload, parsing, AI summary, and history routes."""

from __future__ import annotations

import os
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel

from parse_video_py.document_summary import (
    MAX_UPLOAD_BYTES,
    DocumentBusyError,
    DocumentSummaryError,
    EmptyDocumentTextError,
    get_owned_document,
    list_history,
    parse_document,
    register_document,
    save_to_history,
    sha256_file,
    summarize_document,
)
from parse_video_py.qr_auth import verify_web_session

router = APIRouter(prefix="/auth/documents", tags=["document-summary"])
UPLOAD_DIR = Path(os.getenv("DOCUMENT_SUMMARY_UPLOAD_DIR", "data/document-summary"))
ALLOWED_EXTENSIONS = {".pdf": "pdf", ".docx": "docx"}


class SummaryRequest(BaseModel):
    """Optional controls; an empty request preserves the original API behavior."""

    document_type: str | None = None
    regenerate: bool = False


def _current_user(request: Request):
    token = request.cookies.get("web_session", "")
    if not token:
        raise HTTPException(status_code=401, detail="请先登录")
    try:
        return verify_web_session(token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="登录状态无效或已过期") from exc


async def _save_upload(upload: UploadFile, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with target.open("wb") as stream:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="文件大小不能超过 5MB")
                stream.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    if total == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="上传文件不能为空")
    return total


def _validate_file(path: Path, file_type: str) -> None:
    if file_type == "pdf":
        if not path.read_bytes()[:5].startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="文件内容不是有效的 PDF")
        return
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise HTTPException(status_code=400, detail="文件内容不是有效的 DOCX")
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="文件内容不是有效的 DOCX") from exc


def _service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="文档不存在")
    if isinstance(exc, DocumentBusyError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, EmptyDocumentTextError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, DocumentSummaryError):
        if str(exc) == "不支持的文档类型":
            return HTTPException(status_code=400, detail=str(exc))
        status_code = 503 if str(exc) == "DeepSeek API 未配置" else 502
        return HTTPException(status_code=status_code, detail=str(exc))
    return HTTPException(status_code=500, detail="文档处理失败")


@router.post("/upload")
async def upload_document(request: Request, file: UploadFile = File(...)):
    user = _current_user(request)
    filename = file.filename or ""
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        await file.close()
        raise HTTPException(status_code=400, detail="仅支持 PDF、DOCX 文件")
    temporary = UPLOAD_DIR / ".tmp" / f"{uuid.uuid4().hex}{extension}"
    try:
        size = await _save_upload(file, temporary)
        file_type = ALLOWED_EXTENSIONS[extension]
        _validate_file(temporary, file_type)
        row, cached = register_document(
            user_id=user.id,
            filename=filename,
            file_type=file_type,
            file_size=size,
            content_hash=sha256_file(temporary),
            temporary_path=temporary,
            upload_dir=UPLOAD_DIR,
        )
        return {
            "document_id": row["id"],
            "filename": row["filename"],
            "file_type": row["file_type"],
            "file_size": row["file_size"],
            "cached": cached,
        }
    except HTTPException:
        temporary.unlink(missing_ok=True)
        raise
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="文件保存失败") from exc


@router.post("/{document_id}/parse")
def parse_uploaded_document(request: Request, document_id: str):
    user = _current_user(request)
    try:
        return parse_document(user.id, document_id)
    except Exception as exc:
        raise _service_error(exc) from exc


@router.post("/{document_id}/summarize")
async def summarize_uploaded_document(
    request: Request,
    document_id: str,
    payload: SummaryRequest | None = None,
):
    user = _current_user(request)
    try:
        return await summarize_document(
            user.id,
            document_id,
            document_type=payload.document_type if payload else None,
            regenerate=payload.regenerate if payload else False,
        )
    except Exception as exc:
        raise _service_error(exc) from exc


@router.post("/{document_id}/save")
def save_document_history(request: Request, document_id: str):
    user = _current_user(request)
    try:
        document = get_owned_document(user.id, document_id)
        if document["summary_status"] != "completed":
            raise DocumentSummaryError("请先完成 AI 总结")
        save_to_history(user.id, document_id)
        return {"status": "saved", "document_id": document_id}
    except Exception as exc:
        raise _service_error(exc) from exc


@router.get("/history")
def document_history(request: Request, limit: int = 30):
    user = _current_user(request)
    return {"documents": list_history(user.id, min(max(limit, 1), 100))}
