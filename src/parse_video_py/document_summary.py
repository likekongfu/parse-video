"""Persistent PDF/DOCX extraction and DeepSeek summary service."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from parse_video_py.user_db import (
    _engine,
    document_tasks,
    documents,
    init_user_database,
)

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
PROCESSING_STALE_SECONDS = 10 * 60
DEEPSEEK_API_URL = os.getenv(
    "DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions"
).strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
DEEPSEEK_TIMEOUT_SECONDS = float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "120"))
DEEPSEEK_MAX_INPUT_CHARS = int(os.getenv("DEEPSEEK_MAX_INPUT_CHARS", "100000"))


class DocumentSummaryError(RuntimeError):
    pass


class DocumentBusyError(DocumentSummaryError):
    pass


class EmptyDocumentTextError(DocumentSummaryError):
    pass


def _document_row(user_id: str, document_id: str):
    init_user_database()
    with _engine.connect() as conn:
        return conn.execute(select(documents).where(
            documents.c.id == document_id,
            documents.c.user_id == user_id,
        )).mappings().first()


def get_owned_document(user_id: str, document_id: str):
    row = _document_row(user_id, document_id)
    if not row:
        raise KeyError("文档不存在")
    return row


def register_document(
    *,
    user_id: str,
    filename: str,
    file_type: str,
    file_size: int,
    content_hash: bytes,
    temporary_path: Path,
    upload_dir: Path,
) -> tuple[dict[str, Any], bool]:
    """Persist an upload, reusing an existing document for identical user content."""
    init_user_database()
    with _engine.connect() as conn:
        existing = conn.execute(select(documents).where(
            documents.c.user_id == user_id,
            documents.c.content_hash == content_hash,
        )).mappings().first()
    if existing:
        temporary_path.unlink(missing_ok=True)
        return dict(existing), True

    now = int(time.time())
    document_id = str(uuid.uuid4())
    upload_dir.mkdir(parents=True, exist_ok=True)
    final_path = upload_dir / f"{document_id}.{file_type}"
    temporary_path.replace(final_path)
    values = {
        "id": document_id,
        "user_id": user_id,
        "filename": Path(filename).name[:255] or f"document.{file_type}",
        "file_type": file_type,
        "file_size": file_size,
        "content_hash": content_hash,
        "storage_path": str(final_path),
        "extracted_text": None,
        "extraction_status": "pending",
        "summary_json": None,
        "summary_status": "pending",
        "error_message": None,
        "saved_at": None,
        "created_at": now,
        "updated_at": now,
    }
    try:
        with _engine.begin() as conn:
            conn.execute(insert(documents).values(**values))
    except IntegrityError:
        final_path.unlink(missing_ok=True)
        with _engine.connect() as conn:
            existing = conn.execute(select(documents).where(
                documents.c.user_id == user_id,
                documents.c.content_hash == content_hash,
            )).mappings().one()
        return dict(existing), True
    except Exception:
        final_path.unlink(missing_ok=True)
        raise
    return values, False


def sha256_file(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def _set_task(conn, document_id: str, user_id: str, task_type: str, status: str,
              error_message: str | None = None) -> None:
    now = int(time.time())
    existing_id = conn.execute(select(document_tasks.c.id).where(
        document_tasks.c.document_id == document_id,
        document_tasks.c.task_type == task_type,
    )).scalar_one_or_none()
    values = {
        "status": status,
        "error_message": error_message[:1000] if error_message else None,
        "updated_at": now,
        "completed_at": now if status == "completed" else None,
    }
    if existing_id:
        conn.execute(update(document_tasks).where(
            document_tasks.c.id == existing_id
        ).values(**values))
    else:
        conn.execute(insert(document_tasks).values(
            id=str(uuid.uuid4()), document_id=document_id, user_id=user_id,
            task_type=task_type, created_at=now, **values,
        ))


def _acquire(document_id: str, user_id: str, kind: str):
    status_column = (
        documents.c.extraction_status if kind == "parse" else documents.c.summary_status
    )
    result_column = (
        documents.c.extracted_text if kind == "parse" else documents.c.summary_json
    )
    now = int(time.time())
    with _engine.begin() as conn:
        row = conn.execute(select(documents).where(
            documents.c.id == document_id,
            documents.c.user_id == user_id,
        )).mappings().first()
        if not row:
            raise KeyError("文档不存在")
        if row[status_column.name] == "completed" and row[result_column.name]:
            return dict(row), True
        acquired = conn.execute(update(documents).where(
            documents.c.id == document_id,
            documents.c.user_id == user_id,
            or_(
                status_column.in_(["pending", "failed"]),
                and_(
                    status_column == "processing",
                    documents.c.updated_at < now - PROCESSING_STALE_SECONDS,
                ),
            ),
        ).values(**{
            status_column.name: "processing",
            "error_message": None,
            "updated_at": now,
        }))
        if acquired.rowcount != 1:
            raise DocumentBusyError("文档正在处理中，请稍后重试")
        _set_task(conn, document_id, user_id, kind, "processing")
    return dict(row), False


def _finish(document_id: str, user_id: str, kind: str, *, result: str | None = None,
            error: str | None = None) -> None:
    status_column = (
        documents.c.extraction_status if kind == "parse" else documents.c.summary_status
    )
    result_column = (
        documents.c.extracted_text if kind == "parse" else documents.c.summary_json
    )
    status_value = "failed" if error else "completed"
    values: dict[str, Any] = {
        status_column.name: status_value,
        "error_message": error[:1000] if error else None,
        "updated_at": int(time.time()),
    }
    if result is not None:
        values[result_column.name] = result
    with _engine.begin() as conn:
        conn.execute(update(documents).where(
            documents.c.id == document_id,
            documents.c.user_id == user_id,
        ).values(**values))
        _set_task(conn, document_id, user_id, kind, status_value, error)


def _extract_pdf(path: Path) -> str:
    import fitz

    with fitz.open(str(path)) as document:
        return "\n\n".join(page.get_text("text") for page in document)


def _extract_docx(path: Path) -> str:
    from docx import Document

    document = Document(str(path))
    parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def parse_document(user_id: str, document_id: str) -> dict[str, Any]:
    row, cached = _acquire(document_id, user_id, "parse")
    if cached:
        return {"document_id": document_id, "text_length": len(row["extracted_text"]), "cached": True}
    try:
        path = Path(row["storage_path"])
        text = _extract_pdf(path) if row["file_type"] == "pdf" else _extract_docx(path)
        text = text.strip()
        if not text:
            raise EmptyDocumentTextError("可能为扫描件，请使用 OCR")
        _finish(document_id, user_id, "parse", result=text)
        return {"document_id": document_id, "text_length": len(text), "cached": False}
    except EmptyDocumentTextError as exc:
        _finish(document_id, user_id, "parse", error=str(exc))
        raise
    except Exception as exc:
        message = f"文档解析失败：{exc}"
        _finish(document_id, user_id, "parse", error=message)
        raise DocumentSummaryError(message) from exc


def _normalize_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not str(payload.get("summary") or "").strip():
        raise DocumentSummaryError("AI 返回的总结格式无效")
    return {
        "summary": str(payload["summary"]).strip(),
        "key_points": _normalize_list(payload.get("key_points")),
        "people": _normalize_list(payload.get("people")),
        "dates": _normalize_list(payload.get("dates")),
        "amounts": _normalize_list(payload.get("amounts")),
        "risks": _normalize_list(payload.get("risks")),
    }


async def call_deepseek(text: str, user_id: str) -> dict[str, Any]:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise DocumentSummaryError("DeepSeek API 未配置")
    if len(text) > DEEPSEEK_MAX_INPUT_CHARS:
        head_length = int(DEEPSEEK_MAX_INPUT_CHARS * 0.7)
        tail_length = DEEPSEEK_MAX_INPUT_CHARS - head_length
        source_text = (
            text[:head_length]
            + "\n\n[文档中间部分因模型上下文限制已省略]\n\n"
            + text[-tail_length:]
        )
    else:
        source_text = text
    prompt = (
        "请阅读下面的文档文本并输出严格 JSON。不得使用 Markdown 代码块。"
        "JSON 必须包含 summary 字符串，以及 key_points、people、dates、amounts、risks "
        "五个字符串数组。没有信息时返回空数组；risks 应提取风险、限制、待办或不确定事项。\n\n"
        f"文档文本：\n{source_text}"
    )
    request_body = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": "你是严谨的中文文档分析助手，只输出有效 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 3000,
        "stream": False,
        "user_id": user_id,
    }
    try:
        async with httpx.AsyncClient(timeout=DEEPSEEK_TIMEOUT_SECONDS) as client:
            response = await client.post(
                DEEPSEEK_API_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json=request_body,
            )
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        return _normalize_summary(json.loads(content))
    except httpx.TimeoutException as exc:
        raise DocumentSummaryError("AI 总结超时，请重试") from exc
    except httpx.HTTPStatusError as exc:
        raise DocumentSummaryError(f"DeepSeek API 请求失败（{exc.response.status_code}）") from exc
    except httpx.RequestError as exc:
        raise DocumentSummaryError("DeepSeek API 网络连接失败，请重试") from exc
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise DocumentSummaryError("AI 返回的总结格式无效") from exc


async def summarize_document(user_id: str, document_id: str) -> dict[str, Any]:
    row, cached = _acquire(document_id, user_id, "summary")
    if cached:
        return {"document_id": document_id, **json.loads(row["summary_json"]), "cached": True}
    current = get_owned_document(user_id, document_id)
    if current["extraction_status"] != "completed" or not current["extracted_text"]:
        error = "请先完成文档解析"
        _finish(document_id, user_id, "summary", error=error)
        raise DocumentSummaryError(error)
    try:
        summary = await call_deepseek(current["extracted_text"], user_id)
        summary["source_truncated"] = (
            len(current["extracted_text"]) > DEEPSEEK_MAX_INPUT_CHARS
        )
        serialized = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
        _finish(document_id, user_id, "summary", result=serialized)
        return {"document_id": document_id, **summary, "cached": False}
    except DocumentSummaryError as exc:
        _finish(document_id, user_id, "summary", error=str(exc))
        raise
    except Exception as exc:
        error = "AI 总结失败，请重试"
        _finish(document_id, user_id, "summary", error=error)
        raise DocumentSummaryError(error) from exc


def save_to_history(user_id: str, document_id: str) -> None:
    get_owned_document(user_id, document_id)
    now = int(time.time())
    with _engine.begin() as conn:
        conn.execute(update(documents).where(
            documents.c.id == document_id,
            documents.c.user_id == user_id,
        ).values(saved_at=now, updated_at=now))


def list_history(user_id: str, limit: int = 30) -> list[dict[str, Any]]:
    init_user_database()
    with _engine.connect() as conn:
        rows = conn.execute(select(documents).where(
            documents.c.user_id == user_id,
            documents.c.saved_at.is_not(None),
        ).order_by(documents.c.saved_at.desc()).limit(limit)).mappings().all()
    result = []
    for row in rows:
        summary = json.loads(row["summary_json"]) if row["summary_json"] else None
        result.append({
            "document_id": row["id"],
            "filename": row["filename"],
            "file_type": row["file_type"],
            "file_size": row["file_size"],
            "summary": summary,
            "saved_at": row["saved_at"],
        })
    return result
