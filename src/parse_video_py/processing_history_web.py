"""Authenticated API for unified PDF, image, video, and document history."""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from parse_video_py.processing_history import (
    create_processing_history,
    delete_processing_history,
    list_processing_history,
)
from parse_video_py.qr_auth import verify_web_session

router = APIRouter(prefix="/auth/processing-history", tags=["processing-history"])
ALLOWED_CATEGORIES = {"ai", "document", "pdf", "image", "video"}
ALLOWED_STATUSES = {"processing", "completed", "failed"}


class ProcessingHistoryRequest(BaseModel):
    source_filename: str
    source_file_size: int
    category: str
    tool_type: str
    tool_label: str
    status: str = "completed"
    output_filename: str | None = None
    output_url: str | None = None
    output_expires_at: int | None = None
    error_message: str | None = None


def _current_user(request: Request):
    token = request.cookies.get("web_session", "")
    if not token:
        raise HTTPException(status_code=401, detail="请先登录")
    try:
        return verify_web_session(token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="登录状态无效或已过期") from exc


def _clean(value: str | None, maximum: int) -> str | None:
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    if len(cleaned) > maximum:
        raise HTTPException(status_code=400, detail="历史记录字段过长")
    return cleaned


def _clean_download_url(value: str | None) -> str | None:
    cleaned = _clean(value, 2048)
    if not cleaned:
        return None
    if cleaned.startswith("/") and not cleaned.startswith("//"):
        return cleaned
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="下载地址格式无效")
    return cleaned


@router.post("")
def add_processing_history(request: Request, payload: ProcessingHistoryRequest):
    user = _current_user(request)
    source_filename = _clean(payload.source_filename, 255)
    tool_type = _clean(payload.tool_type, 64)
    tool_label = _clean(payload.tool_label, 120)
    if not source_filename or not tool_type or not tool_label:
        raise HTTPException(status_code=400, detail="文件名和功能类型不能为空")
    if payload.source_file_size < 0:
        raise HTTPException(status_code=400, detail="文件大小不能为负数")
    if payload.category not in ALLOWED_CATEGORIES:
        raise HTTPException(status_code=400, detail="不支持的历史分类")
    if payload.status not in ALLOWED_STATUSES:
        raise HTTPException(status_code=400, detail="不支持的处理状态")
    output_expires_at = payload.output_expires_at
    if output_expires_at is not None and output_expires_at <= 0:
        raise HTTPException(status_code=400, detail="下载过期时间无效")
    record = create_processing_history(
        user_id=user.id,
        source_filename=source_filename,
        source_file_size=payload.source_file_size,
        category=payload.category,
        tool_type=tool_type,
        tool_label=tool_label,
        status=payload.status,
        output_filename=_clean(payload.output_filename, 255),
        output_url=_clean_download_url(payload.output_url),
        output_expires_at=output_expires_at,
        error_message=_clean(payload.error_message, 1000),
    )
    return {"history": record}


@router.get("")
def get_processing_history(request: Request, limit: int = 30):
    user = _current_user(request)
    return {"history": list_processing_history(user.id, min(max(limit, 1), 100))}


@router.delete("/{history_id}")
def remove_processing_history(request: Request, history_id: str):
    user = _current_user(request)
    try:
        normalized_id = str(uuid.UUID(history_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="历史记录编号无效") from exc
    if not delete_processing_history(user.id, normalized_id):
        raise HTTPException(status_code=404, detail="历史记录不存在")
    return {"deleted": True, "id": normalized_id}
