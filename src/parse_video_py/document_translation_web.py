"""Authenticated document translation and export routes."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from parse_video_py.document_summary_web import _current_user
from parse_video_py.document_translation import (
    DocumentTranslationBusyError,
    DocumentTranslationError,
    render_translation_export,
    translate_document,
)

router = APIRouter(prefix="/auth/documents", tags=["document-translation"])


class GlossaryItem(BaseModel):
    source: str = Field(min_length=1, max_length=120)
    target: str = Field(min_length=1, max_length=240)


class TranslationRequest(BaseModel):
    source_language: str = "auto"
    target_language: str
    mode: str = "translation"
    style: str = "general"
    glossary: list[GlossaryItem] = Field(default_factory=list, max_length=100)


def _service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc).strip("'"))
    if isinstance(exc, DocumentTranslationBusyError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, DocumentTranslationError):
        message = str(exc)
        if message == "DeepSeek API 未配置":
            return HTTPException(status_code=503, detail=message)
        if message == "可能为扫描件，请使用 OCR":
            return HTTPException(status_code=422, detail=message)
        return HTTPException(status_code=502, detail=message)
    return HTTPException(status_code=500, detail="文档翻译失败")


@router.post("/{document_id}/translate")
async def translate_uploaded_document(
    request: Request, document_id: str, payload: TranslationRequest
):
    user = _current_user(request)
    try:
        return await translate_document(
            user.id,
            document_id,
            source_language=payload.source_language,
            target_language=payload.target_language,
            mode=payload.mode,
            style=payload.style,
            glossary=[item.model_dump() for item in payload.glossary],
        )
    except Exception as exc:
        raise _service_error(exc) from exc


@router.get("/{document_id}/translations/{translation_id}/export")
def export_translation(
    request: Request,
    document_id: str,
    translation_id: str,
    format: str = Query(pattern="^(docx|txt)$"),
):
    user = _current_user(request)
    try:
        content, media_type, filename = render_translation_export(
            user.id, document_id, translation_id, format
        )
        ascii_name = f"translation.{format}"
        disposition = (
            f"attachment; filename={ascii_name}; filename*=UTF-8''{quote(filename)}"
        )
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": disposition},
        )
    except Exception as exc:
        raise _service_error(exc) from exc
