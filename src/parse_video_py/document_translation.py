"""Cached paragraph-oriented document translation backed by DeepSeek."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time
import uuid
from typing import Any, Iterable

import httpx
from docx import Document
from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from parse_video_py.document_summary import get_owned_document
from parse_video_py.user_db import (
    _engine,
    document_translations,
    init_user_database,
)

PROCESSING_STALE_SECONDS = 10 * 60
TRANSLATION_BATCH_CHARS = int(os.getenv("DOCUMENT_TRANSLATION_BATCH_CHARS", "12000"))
DEEPSEEK_API_URL = os.getenv(
    "DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions"
).strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
DEEPSEEK_TIMEOUT_SECONDS = float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "120"))

ALLOWED_MODES = {"translation", "bilingual"}
ALLOWED_STYLES = {"general", "business", "legal", "technical"}
STYLE_LABELS = {
    "general": "通用：自然、准确、清晰",
    "business": "商务：专业、简洁、正式",
    "legal": "法律：严谨、保守，不弱化义务、限制和条件",
    "technical": "技术：准确保留技术术语、参数、单位和代码",
}
_LANGUAGE_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,15}$")


class DocumentTranslationError(RuntimeError):
    pass


class DocumentTranslationBusyError(DocumentTranslationError):
    pass


def normalize_glossary(value: Any) -> list[dict[str, str]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise DocumentTranslationError("术语表格式无效")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            raise DocumentTranslationError("术语表格式无效")
        source = str(item.get("source") or "").strip()
        target = str(item.get("target") or "").strip()
        if not source or not target:
            raise DocumentTranslationError("术语表的原词和译词不能为空")
        if len(source) > 120 or len(target) > 240:
            raise DocumentTranslationError("单条术语过长")
        key = (source, target)
        if key not in seen:
            result.append({"source": source, "target": target})
            seen.add(key)
    if len(result) > 100:
        raise DocumentTranslationError("术语表最多支持 100 条")
    return result


def normalize_options(
    *,
    source_language: str,
    target_language: str,
    mode: str,
    style: str,
    glossary: Any,
) -> dict[str, Any]:
    source_language = (source_language or "auto").strip()
    target_language = (target_language or "").strip()
    mode = (mode or "translation").strip()
    style = (style or "general").strip()
    if source_language != "auto" and not _LANGUAGE_CODE.fullmatch(source_language):
        raise DocumentTranslationError("源语言无效")
    if not _LANGUAGE_CODE.fullmatch(target_language):
        raise DocumentTranslationError("请选择有效的目标语言")
    if mode not in ALLOWED_MODES:
        raise DocumentTranslationError("翻译模式无效")
    if style not in ALLOWED_STYLES:
        raise DocumentTranslationError("翻译风格无效")
    return {
        "source_language": source_language,
        "target_language": target_language,
        "mode": mode,
        "style": style,
        "glossary": normalize_glossary(glossary),
    }


def _options_hash(options: dict[str, Any]) -> bytes:
    canonical = dict(options)
    canonical["glossary"] = sorted(
        options["glossary"], key=lambda item: (item["source"], item["target"])
    )
    serialized = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).digest()


def segment_document(text: str) -> list[dict[str, str]]:
    """Split on document line/paragraph boundaries, never on sentences."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [block.strip() for block in re.split(r"\n+", normalized) if block.strip()]
    result = []
    for index, block in enumerate(blocks, 1):
        compact = re.sub(r"\s+", " ", block).strip()
        is_heading = len(compact) <= 80 and (
            bool(
                re.match(
                    r"^(第[一二三四五六七八九十百0-9]+[章节条]|[0-9一二三四五六七八九十]+[.、])",
                    compact,
                )
            )
            or not re.search(r"[。！？；.!?;]$", compact)
        )
        result.append(
            {
                "segment_id": f"seg-{index:04d}",
                "source_text": block,
                "kind": "heading" if is_heading else "paragraph",
            }
        )
    if not result:
        raise DocumentTranslationError("可能为扫描件，请使用 OCR")
    return result


def iter_segment_batches(
    segments: list[dict[str, str]], max_chars: int = TRANSLATION_BATCH_CHARS
) -> Iterable[list[dict[str, str]]]:
    batch: list[dict[str, str]] = []
    size = 0
    for segment in segments:
        segment_size = len(segment["source_text"])
        if batch and size + segment_size > max_chars:
            yield batch
            batch = []
            size = 0
        batch.append(segment)
        size += segment_size
    if batch:
        yield batch


def _normalize_ai_result(
    payload: Any, expected_segments: list[dict[str, str]]
) -> tuple[str | None, list[dict[str, str]]]:
    if not isinstance(payload, dict) or not isinstance(
        payload.get("translations"), list
    ):
        raise DocumentTranslationError("AI 返回的翻译格式无效")
    expected_ids = [item["segment_id"] for item in expected_segments]
    translated: dict[str, str] = {}
    for item in payload["translations"]:
        if not isinstance(item, dict):
            raise DocumentTranslationError("AI 返回的翻译格式无效")
        segment_id = str(item.get("segment_id") or "")
        text = str(item.get("translated_text") or "").strip()
        if segment_id in translated or segment_id not in expected_ids or not text:
            raise DocumentTranslationError("AI 返回的翻译段落不完整")
        translated[segment_id] = text
    if set(translated) != set(expected_ids):
        raise DocumentTranslationError("AI 返回的翻译段落不完整")
    detected = str(payload.get("detected_source_language") or "").strip() or None
    return detected, [
        {"segment_id": segment_id, "translated_text": translated[segment_id]}
        for segment_id in expected_ids
    ]


async def call_deepseek_translation(
    segments: list[dict[str, str]],
    *,
    source_language: str,
    target_language: str,
    style: str,
    glossary: list[dict[str, str]],
    user_id: str,
) -> tuple[str | None, list[dict[str, str]]]:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise DocumentTranslationError("DeepSeek API 未配置")
    source_instruction = (
        "自动识别源语言，并在 detected_source_language 中返回语言代码"
        if source_language == "auto"
        else f"源语言代码为 {source_language}"
    )
    glossary_text = json.dumps(glossary, ensure_ascii=False, separators=(",", ":"))
    source_segments = [
        {
            "segment_id": item["segment_id"],
            "kind": item["kind"],
            "text": item["source_text"],
        }
        for item in segments
    ]
    prompt = (
        f"将以下文档段落翻译为 {target_language}。{source_instruction}。"
        f"风格要求：{STYLE_LABELS[style]}。一次处理的是完整标题和段落，禁止拆成逐句结果。"
        "必须保持输入段落顺序和 segment_id，不得合并、遗漏或新增段落。"
        "原样保留编号、金额、货币、日期、计量单位、网址、邮箱、代码和无法确定的专有名词。"
        "优先严格使用术语表。只输出 JSON，不要 Markdown。"
        'JSON 格式为 {"detected_source_language":"语言代码",'
        '"translations":[{"segment_id":"seg-0001",'
        '"translated_text":"..."}]}。\n'
        f"术语表：{glossary_text}\n"
        f"段落：{json.dumps(source_segments, ensure_ascii=False, separators=(',', ':'))}"
    )
    request_body = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "你是专业文档翻译引擎，只返回严格有效的 JSON。",
            },
            {"role": "user", "content": prompt},
        ],
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 8000,
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
        content = response.json()["choices"][0]["message"]["content"]
        return _normalize_ai_result(json.loads(content), segments)
    except httpx.TimeoutException as exc:
        raise DocumentTranslationError("AI 翻译超时，请重试") from exc
    except httpx.HTTPStatusError as exc:
        raise DocumentTranslationError(
            f"DeepSeek API 请求失败（{exc.response.status_code}）"
        ) from exc
    except httpx.RequestError as exc:
        raise DocumentTranslationError("DeepSeek API 网络连接失败，请重试") from exc
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise DocumentTranslationError("AI 返回的翻译格式无效") from exc


def _translation_row(user_id: str, document_id: str, options_hash: bytes):
    with _engine.connect() as conn:
        return (
            conn.execute(
                select(document_translations).where(
                    document_translations.c.user_id == user_id,
                    document_translations.c.document_id == document_id,
                    document_translations.c.options_hash == options_hash,
                )
            )
            .mappings()
            .first()
        )


def _acquire_translation(
    user_id: str, document_id: str, options: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    init_user_database()
    options_hash = _options_hash(options)
    now = int(time.time())
    row = _translation_row(user_id, document_id, options_hash)
    if row and row["status"] == "completed" and row["result_json"]:
        return dict(row), True
    if row:
        with _engine.begin() as conn:
            acquired = conn.execute(
                update(document_translations)
                .where(
                    document_translations.c.id == row["id"],
                    or_(
                        document_translations.c.status == "failed",
                        and_(
                            document_translations.c.status == "processing",
                            document_translations.c.updated_at
                            < now - PROCESSING_STALE_SECONDS,
                        ),
                    ),
                )
                .values(status="processing", error_message=None, updated_at=now)
            )
        if acquired.rowcount != 1:
            raise DocumentTranslationBusyError("文档正在翻译中，请稍后重试")
        return dict(row), False

    values = {
        "id": str(uuid.uuid4()),
        "document_id": document_id,
        "user_id": user_id,
        "options_hash": options_hash,
        "source_language": options["source_language"],
        "detected_source_language": None,
        "target_language": options["target_language"],
        "mode": options["mode"],
        "style": options["style"],
        "glossary_json": json.dumps(
            options["glossary"], ensure_ascii=False, separators=(",", ":")
        ),
        "result_json": None,
        "status": "processing",
        "error_message": None,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }
    try:
        with _engine.begin() as conn:
            conn.execute(insert(document_translations).values(**values))
        return values, False
    except IntegrityError:
        winner = _translation_row(user_id, document_id, options_hash)
        if winner and winner["status"] == "completed" and winner["result_json"]:
            return dict(winner), True
        raise DocumentTranslationBusyError("文档正在翻译中，请稍后重试")


def _finish_translation(
    translation_id: str,
    user_id: str,
    *,
    result: dict[str, Any] | None = None,
    detected_source_language: str | None = None,
    error: str | None = None,
) -> None:
    now = int(time.time())
    values: dict[str, Any] = {
        "status": "failed" if error else "completed",
        "error_message": error[:1000] if error else None,
        "updated_at": now,
        "completed_at": None if error else now,
    }
    if result is not None:
        values["result_json"] = json.dumps(
            result, ensure_ascii=False, separators=(",", ":")
        )
        values["detected_source_language"] = detected_source_language
    with _engine.begin() as conn:
        conn.execute(
            update(document_translations)
            .where(
                document_translations.c.id == translation_id,
                document_translations.c.user_id == user_id,
            )
            .values(**values)
        )


async def translate_document(
    user_id: str,
    document_id: str,
    *,
    source_language: str = "auto",
    target_language: str,
    mode: str = "translation",
    style: str = "general",
    glossary: Any = None,
) -> dict[str, Any]:
    document = get_owned_document(user_id, document_id)
    if document["extraction_status"] != "completed" or not document["extracted_text"]:
        raise DocumentTranslationError("请先完成文档解析")
    options = normalize_options(
        source_language=source_language,
        target_language=target_language,
        mode=mode,
        style=style,
        glossary=glossary,
    )
    task, cached = _acquire_translation(user_id, document_id, options)
    if cached:
        return {
            "document_id": document_id,
            "translation_id": task["id"],
            **json.loads(task["result_json"]),
            "cached": True,
        }
    try:
        segments = segment_document(document["extracted_text"])
        translated_by_id: dict[str, str] = {}
        detected_language: str | None = None
        resolved_source = options["source_language"]
        for batch in iter_segment_batches(segments):
            detected, translated = await call_deepseek_translation(
                batch,
                source_language=resolved_source,
                target_language=options["target_language"],
                style=options["style"],
                glossary=options["glossary"],
                user_id=user_id,
            )
            if detected_language is None and detected:
                detected_language = detected
                if resolved_source == "auto":
                    resolved_source = detected
            translated_by_id.update(
                (item["segment_id"], item["translated_text"]) for item in translated
            )
        result_segments = [
            {**segment, "translated_text": translated_by_id[segment["segment_id"]]}
            for segment in segments
        ]
        result = {
            "source_language": options["source_language"],
            "detected_source_language": detected_language,
            "target_language": options["target_language"],
            "mode": options["mode"],
            "style": options["style"],
            "segments": result_segments,
        }
        _finish_translation(
            task["id"],
            user_id,
            result=result,
            detected_source_language=detected_language,
        )
        return {
            "document_id": document_id,
            "translation_id": task["id"],
            **result,
            "cached": False,
        }
    except DocumentTranslationError as exc:
        _finish_translation(task["id"], user_id, error=str(exc))
        raise
    except Exception as exc:
        error = "文档翻译失败，请重试"
        _finish_translation(task["id"], user_id, error=error)
        raise DocumentTranslationError(error) from exc


def get_translation(
    user_id: str, document_id: str, translation_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    document = get_owned_document(user_id, document_id)
    init_user_database()
    with _engine.connect() as conn:
        row = (
            conn.execute(
                select(document_translations).where(
                    document_translations.c.id == translation_id,
                    document_translations.c.document_id == document_id,
                    document_translations.c.user_id == user_id,
                )
            )
            .mappings()
            .first()
        )
    if not row:
        raise KeyError("翻译结果不存在")
    if row["status"] != "completed" or not row["result_json"]:
        raise DocumentTranslationError("翻译结果尚未生成")
    return dict(document), json.loads(row["result_json"])


def render_translation_export(
    user_id: str, document_id: str, translation_id: str, export_format: str
) -> tuple[bytes, str, str]:
    document, result = get_translation(user_id, document_id, translation_id)
    if export_format not in {"docx", "txt"}:
        raise DocumentTranslationError("仅支持导出 DOCX 或 TXT")
    bilingual = result["mode"] == "bilingual"
    segments = result["segments"]
    if export_format == "txt":
        parts = []
        for segment in segments:
            if bilingual:
                parts.append(
                    f"原文：\n{segment['source_text']}\n\n译文：\n{segment['translated_text']}"
                )
            else:
                parts.append(segment["translated_text"])
        payload = "\n\n".join(parts).encode("utf-8-sig")
        media_type = "text/plain; charset=utf-8"
    else:
        output_document = Document()
        for segment in segments:
            if bilingual:
                source = output_document.add_paragraph()
                source.add_run(segment["source_text"]).italic = True
                output_document.add_paragraph(segment["translated_text"])
            elif segment["kind"] == "heading":
                output_document.add_heading(segment["translated_text"], level=2)
            else:
                output_document.add_paragraph(segment["translated_text"])
        stream = io.BytesIO()
        output_document.save(stream)
        payload = stream.getvalue()
        media_type = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
    base_name = document["filename"].rsplit(".", 1)[0] or "translation"
    return payload, media_type, f"{base_name}-translated.{export_format}"
