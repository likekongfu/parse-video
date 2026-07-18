"""Cached paragraph-oriented document translation backed by DeepSeek."""

from __future__ import annotations

import hashlib
import html
import io
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
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
TRANSLATION_BATCH_SIZE = max(1, int(os.getenv("TRANSLATION_BATCH_SIZE", "20")))
TRANSLATION_BATCH_RETRIES = 2
TRANSLATION_PIPELINE_VERSION = "pdf-layout-v1"
PDF_TRANSLATION_MIN_SCALE = min(
    1.0, max(0.25, float(os.getenv("PDF_TRANSLATION_MIN_SCALE", "0.50")))
)
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
logger = logging.getLogger(__name__)


class DocumentTranslationError(RuntimeError):
    pass


class DocumentTranslationBusyError(DocumentTranslationError):
    pass


class InvalidTranslationResponseError(DocumentTranslationError):
    """The model response cannot be decoded as a translation payload."""

    def __init__(self, message: str, *, finish_reason: str | None = None) -> None:
        super().__init__(message)
        self.finish_reason = finish_reason


class TruncatedTranslationResponseError(InvalidTranslationResponseError):
    """The model stopped because its output token limit was reached."""


class IncompleteTranslationResponseError(DocumentTranslationError):
    """The model returned only a usable subset of the requested segments."""

    def __init__(
        self,
        translated: list[dict[str, str]],
        detected_source_language: str | None,
        *,
        finish_reason: str | None = None,
    ) -> None:
        super().__init__("AI 返回的翻译段落不完整")
        self.translated = translated
        self.detected_source_language = detected_source_language
        self.finish_reason = finish_reason


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
    canonical["pipeline_version"] = TRANSLATION_PIPELINE_VERSION
    canonical["glossary"] = sorted(
        options["glossary"], key=lambda item: (item["source"], item["target"])
    )
    serialized = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).digest()


def _segment_kind(text: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    is_heading = len(compact) <= 80 and (
        bool(
            re.match(
                r"^(第[一二三四五六七八九十百0-9]+[章节条]|[0-9一二三四五六七八九十]+[.、])",
                compact,
            )
        )
        or not re.search(r"[。！？；.!?;]$", compact)
    )
    return "heading" if is_heading else "paragraph"


def segment_document(text: str) -> list[dict[str, str]]:
    """Split on document line/paragraph boundaries, never on sentences."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [block.strip() for block in re.split(r"\n+", normalized) if block.strip()]
    result = []
    for index, block in enumerate(blocks, 1):
        result.append(
            {
                "segment_id": f"seg-{index:04d}",
                "source_text": block,
                "kind": _segment_kind(block),
            }
        )
    if not result:
        raise DocumentTranslationError("可能为扫描件，请使用 OCR")
    return result


def _pdf_block_alignment(block_bbox: tuple[float, ...], lines: list[dict]) -> str:
    block_left, _, block_right, _ = block_bbox
    block_width = max(1.0, block_right - block_left)
    line_boxes = [line.get("bbox") for line in lines if line.get("bbox")]
    if not line_boxes:
        return "left"
    left_gap = min(float(box[0]) for box in line_boxes) - block_left
    right_gap = block_right - max(float(box[2]) for box in line_boxes)
    if abs(left_gap - right_gap) <= max(4.0, block_width * 0.08):
        return "center"
    if left_gap > right_gap * 2 + 4:
        return "right"
    return "left"


def segment_pdf_document(path: Path) -> list[dict[str, Any]]:
    """Extract stable page/block segments and the geometry needed for PDF export."""
    import fitz

    segments: list[dict[str, Any]] = []
    with fitz.open(str(path)) as source:
        for page_index, page in enumerate(source):
            page_dict = page.get_text("dict", sort=True)
            text_block_index = 0
            for block in page_dict.get("blocks", []):
                if block.get("type") != 0:
                    continue
                lines = block.get("lines") or []
                line_texts: list[str] = []
                spans: list[dict[str, Any]] = []
                for line in lines:
                    line_spans = line.get("spans") or []
                    spans.extend(line_spans)
                    line_text = "".join(
                        str(span.get("text") or "") for span in line_spans
                    )
                    if line_text.strip():
                        line_texts.append(line_text.strip())
                source_text = "\n".join(line_texts).strip()
                if not source_text:
                    continue
                bbox = tuple(float(value) for value in block.get("bbox", ()))
                if len(bbox) != 4 or bbox[2] - bbox[0] < 1 or bbox[3] - bbox[1] < 1:
                    continue
                text_block_index += 1
                primary_span = max(
                    spans,
                    key=lambda span: len(str(span.get("text") or "")),
                    default={},
                )
                font_size = max(
                    4.0,
                    float(primary_span.get("size") or max(8.0, bbox[3] - bbox[1])),
                )
                color = int(primary_span.get("color") or 0) & 0xFFFFFF
                font_name = str(primary_span.get("font") or "")
                segments.append(
                    {
                        "segment_id": f"p{page_index + 1:04d}-b{text_block_index:04d}",
                        "source_text": source_text,
                        "kind": _segment_kind(source_text),
                        "layout": {
                            "page_number": page_index + 1,
                            "block_index": text_block_index,
                            "bbox": [round(value, 3) for value in bbox],
                            "font_size": round(font_size, 2),
                            "font_color": f"#{color:06x}",
                            "font_family": (
                                "monospace"
                                if "courier" in font_name.lower()
                                else (
                                    "serif"
                                    if any(
                                        name in font_name.lower()
                                        for name in ("times", "serif", "song", "ming")
                                    )
                                    else "sans-serif"
                                )
                            ),
                            "text_align": _pdf_block_alignment(bbox, lines),
                        },
                    }
                )
    if not segments:
        raise DocumentTranslationError("可能为扫描件，请使用 OCR")
    return segments


def iter_segment_batches(
    segments: list[dict[str, str]], batch_size: int = TRANSLATION_BATCH_SIZE
) -> Iterable[list[dict[str, str]]]:
    """Yield ordered batches capped by segment count, never by output size guesses."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    for offset in range(0, len(segments), batch_size):
        yield segments[offset : offset + batch_size]


def _normalize_ai_result(
    payload: Any, expected_segments: list[dict[str, str]]
) -> tuple[str | None, list[dict[str, str]]]:
    if not isinstance(payload, dict) or not isinstance(
        payload.get("translations"), list
    ):
        raise InvalidTranslationResponseError("AI 返回的翻译格式无效")
    expected_ids = [item["segment_id"] for item in expected_segments]
    translated: dict[str, str] = {}
    for item in payload["translations"]:
        if not isinstance(item, dict):
            raise InvalidTranslationResponseError("AI 返回的翻译格式无效")
        segment_id = str(item.get("segment_id") or "")
        text = str(item.get("translated_text") or "").strip()
        # Ignore hallucinated/duplicate entries. Required IDs are still checked below,
        # and only expected translations are ever persisted.
        if segment_id in translated or segment_id not in expected_ids or not text:
            continue
        translated[segment_id] = text
    detected = str(payload.get("detected_source_language") or "").strip() or None
    if set(translated) != set(expected_ids):
        partial = [
            {"segment_id": segment_id, "translated_text": translated[segment_id]}
            for segment_id in expected_ids
            if segment_id in translated
        ]
        raise IncompleteTranslationResponseError(partial, detected)
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
    request_id: str = "unknown",
    batch_index: int = 0,
    attempt: int = 0,
) -> tuple[str | None, list[dict[str, str]]]:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise DocumentTranslationError("DeepSeek API 未配置")
    source_instruction = (
        "自动识别源语言"
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
        "每个返回项只能包含 segment_id 和 translated_text。"
        'JSON 格式为 {"translations":[{"segment_id":"seg-0001",'
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
        choice = response.json()["choices"][0]
        finish_reason = str(choice.get("finish_reason") or "unknown")
        logger.info(
            "translation_batch_response request_id=%s batch_index=%d "
            "segment_count=%d attempt=%d finish_reason=%s",
            request_id,
            batch_index,
            len(segments),
            attempt,
            finish_reason,
        )
        if finish_reason == "length":
            raise TruncatedTranslationResponseError(
                "AI 翻译输出被截断", finish_reason=finish_reason
            )
        content = choice["message"]["content"]
        try:
            payload = json.loads(content)
            return _normalize_ai_result(payload, segments)
        except IncompleteTranslationResponseError as exc:
            exc.finish_reason = finish_reason
            raise
        except InvalidTranslationResponseError as exc:
            exc.finish_reason = finish_reason
            raise
        except json.JSONDecodeError as exc:
            raise InvalidTranslationResponseError(
                "AI 返回的翻译 JSON 无效", finish_reason=finish_reason
            ) from exc
    except httpx.TimeoutException as exc:
        logger.warning(
            "translation_batch_request_failed request_id=%s batch_index=%d "
            "segment_count=%d attempt=%d finish_reason=unknown reason=timeout",
            request_id,
            batch_index,
            len(segments),
            attempt,
        )
        raise DocumentTranslationError("AI 翻译超时，请重试") from exc
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "translation_batch_request_failed request_id=%s batch_index=%d "
            "segment_count=%d attempt=%d finish_reason=unknown reason=http_%d",
            request_id,
            batch_index,
            len(segments),
            attempt,
            exc.response.status_code,
        )
        raise DocumentTranslationError(
            f"DeepSeek API 请求失败（{exc.response.status_code}）"
        ) from exc
    except httpx.RequestError as exc:
        logger.warning(
            "translation_batch_request_failed request_id=%s batch_index=%d "
            "segment_count=%d attempt=%d finish_reason=unknown reason=network",
            request_id,
            batch_index,
            len(segments),
            attempt,
        )
        raise DocumentTranslationError("DeepSeek API 网络连接失败，请重试") from exc
    except (KeyError, IndexError, TypeError) as exc:
        raise InvalidTranslationResponseError("AI 返回的翻译格式无效") from exc


async def translate_batch_with_recovery(
    segments: list[dict[str, str]],
    *,
    source_language: str,
    target_language: str,
    style: str,
    glossary: list[dict[str, str]],
    user_id: str,
    request_id: str = "unknown",
    batch_index: int = 0,
    max_retries: int = TRANSLATION_BATCH_RETRIES,
    split_depth: int = 0,
) -> tuple[str | None, list[dict[str, str]]]:
    """Retry a batch at most twice, then bisect it until a clear singleton error."""
    if not segments:
        return None, []
    last_error: (
        InvalidTranslationResponseError | IncompleteTranslationResponseError | None
    ) = None
    for attempt in range(max_retries + 1):
        try:
            return await call_deepseek_translation(
                segments,
                source_language=source_language,
                target_language=target_language,
                style=style,
                glossary=glossary,
                user_id=user_id,
                request_id=request_id,
                batch_index=batch_index,
                attempt=attempt,
            )
        except (
            InvalidTranslationResponseError,
            IncompleteTranslationResponseError,
        ) as exc:
            last_error = exc
            logger.warning(
                "translation_batch_retry request_id=%s batch_index=%d "
                "segment_count=%d attempt=%d finish_reason=%s split_depth=%d reason=%s",
                request_id,
                batch_index,
                len(segments),
                attempt,
                getattr(exc, "finish_reason", None) or "unknown",
                split_depth,
                type(exc).__name__,
            )

    if len(segments) == 1:
        segment_id = segments[0]["segment_id"]
        raise DocumentTranslationError(
            f"段落 {segment_id} 翻译失败：AI 返回内容无效或不完整"
        ) from last_error

    midpoint = len(segments) // 2
    logger.warning(
        "translation_batch_split request_id=%s batch_index=%d segment_count=%d "
        "finish_reason=%s split_depth=%d",
        request_id,
        batch_index,
        len(segments),
        getattr(last_error, "finish_reason", None) or "unknown",
        split_depth,
    )
    detected_language: str | None = None
    translated_by_id: dict[str, str] = {}
    for smaller_batch in (segments[:midpoint], segments[midpoint:]):
        detected, translated = await translate_batch_with_recovery(
            smaller_batch,
            source_language=source_language,
            target_language=target_language,
            style=style,
            glossary=glossary,
            user_id=user_id,
            request_id=request_id,
            batch_index=batch_index,
            max_retries=max_retries,
            split_depth=split_depth + 1,
        )
        detected_language = detected_language or detected
        translated_by_id.update(
            (item["segment_id"], item["translated_text"]) for item in translated
        )
    return detected_language, [
        {
            "segment_id": segment["segment_id"],
            "translated_text": translated_by_id[segment["segment_id"]],
        }
        for segment in segments
    ]


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
    request_id = uuid.uuid4().hex
    try:
        if document.get("file_type") == "pdf":
            segments = segment_pdf_document(Path(document["storage_path"]))
        else:
            segments = segment_document(document["extracted_text"])
        translated_by_id: dict[str, str] = {}
        detected_language: str | None = None
        resolved_source = options["source_language"]
        batches = list(iter_segment_batches(segments))
        logger.info(
            "translation_started request_id=%s document_id=%s segment_count=%d batch_count=%d",
            request_id,
            document_id,
            len(segments),
            len(batches),
        )
        for batch_index, batch in enumerate(batches, 1):
            detected, translated = await translate_batch_with_recovery(
                batch,
                source_language=resolved_source,
                target_language=options["target_language"],
                style=options["style"],
                glossary=options["glossary"],
                user_id=user_id,
                request_id=request_id,
                batch_index=batch_index,
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
        logger.info(
            "translation_batches_completed request_id=%s document_id=%s "
            "segment_count=%d batch_count=%d",
            request_id,
            document_id,
            len(result_segments),
            len(batches),
        )
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


def _normalize_segment_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _set_paragraph_text(paragraph, text: str) -> None:
    if paragraph.runs:
        paragraph.runs[0].text = text
        for run in paragraph.runs[1:]:
            run.text = ""
    else:
        paragraph.add_run(text)


def _table_row_text(row) -> str:
    cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
    return " | ".join(cells)


def _replace_table_row_text(row, translated_text: str) -> None:
    translated_cells = [part.strip() for part in translated_text.split("|")]
    non_empty_cells = [cell for cell in row.cells if cell.text.strip()]
    target_cells = non_empty_cells or list(row.cells)
    if len(translated_cells) == len(target_cells) and len(target_cells) > 1:
        for cell, text in zip(target_cells, translated_cells, strict=False):
            if cell.paragraphs:
                _set_paragraph_text(cell.paragraphs[0], text)
                for paragraph in cell.paragraphs[1:]:
                    _set_paragraph_text(paragraph, "")
            else:
                cell.text = text
        return

    if target_cells:
        first = target_cells[0]
        if first.paragraphs:
            _set_paragraph_text(first.paragraphs[0], translated_text)
            for paragraph in first.paragraphs[1:]:
                _set_paragraph_text(paragraph, "")
        else:
            first.text = translated_text
        for cell in target_cells[1:]:
            for paragraph in cell.paragraphs:
                _set_paragraph_text(paragraph, "")


def _docx_translation_targets(document: Document) -> list[tuple[str, Any, str]]:
    targets: list[tuple[str, Any, str]] = []
    for paragraph in document.paragraphs:
        source = paragraph.text.strip()
        if source:
            targets.append(("paragraph", paragraph, source))
    for table in document.tables:
        for row in table.rows:
            source = _table_row_text(row)
            if source:
                targets.append(("table_row", row, source))
    return targets


def _render_docx_from_original(
    document_row: dict[str, Any], segments: list[dict[str, str]]
) -> bytes | None:
    source_path = Path(str(document_row.get("storage_path") or ""))
    if document_row.get("file_type") != "docx" or not source_path.is_file():
        return None

    output_document = Document(str(source_path))
    targets = _docx_translation_targets(output_document)
    if len(targets) != len(segments):
        logger.info(
            "translation_docx_template_mismatch document_id=%s target_count=%d segment_count=%d",
            document_row.get("id"),
            len(targets),
            len(segments),
        )
        return None

    for (target_type, target, source), segment in zip(targets, segments, strict=False):
        if _normalize_segment_text(source) != _normalize_segment_text(
            segment["source_text"]
        ):
            logger.info(
                "translation_docx_template_text_mismatch document_id=%s segment_id=%s",
                document_row.get("id"),
                segment.get("segment_id"),
            )
            return None
        if target_type == "paragraph":
            _set_paragraph_text(target, segment["translated_text"])
        else:
            _replace_table_row_text(target, segment["translated_text"])

    stream = io.BytesIO()
    output_document.save(stream)
    return stream.getvalue()


def _pdf_segments_with_layout(
    document_row: dict[str, Any], segments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if all(isinstance(segment.get("layout"), dict) for segment in segments):
        return segments

    # Compatibility for a completed translation created before layout metadata was
    # persisted. Only attach coordinates when block boundaries still match exactly.
    current = segment_pdf_document(Path(document_row["storage_path"]))
    if len(current) != len(segments):
        raise DocumentTranslationError("旧翻译结果缺少版式信息，请重新翻译后再导出 PDF")
    merged: list[dict[str, Any]] = []
    for stored, extracted in zip(segments, current, strict=False):
        if _normalize_segment_text(stored["source_text"]) != _normalize_segment_text(
            extracted["source_text"]
        ):
            raise DocumentTranslationError(
                "旧翻译结果缺少版式信息，请重新翻译后再导出 PDF"
            )
        merged.append({**stored, "layout": extracted["layout"]})
    return merged


def _apply_pdf_text_redactions(pdf_document, segments: list[dict[str, Any]]) -> None:
    import fitz

    by_page: dict[int, list[dict[str, Any]]] = {}
    for segment in segments:
        page_number = int(segment["layout"]["page_number"])
        by_page.setdefault(page_number, []).append(segment)

    for page_number, page_segments in by_page.items():
        if page_number < 1 or page_number > pdf_document.page_count:
            raise DocumentTranslationError(f"PDF 第 {page_number} 页不存在")
        page = pdf_document[page_number - 1]
        for segment in page_segments:
            rect = fitz.Rect(segment["layout"]["bbox"])
            if rect.is_empty or rect.is_infinite:
                raise DocumentTranslationError(
                    f"PDF 文本块 {segment['segment_id']} 坐标无效"
                )
            page.add_redact_annot(rect, fill=None, cross_out=False)
        redaction_options = {
            "images": getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0),
            "graphics": getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0),
        }
        try:
            page.apply_redactions(
                **redaction_options,
                text=getattr(fitz, "PDF_REDACT_TEXT_REMOVE", 0),
            )
        except TypeError:  # PyMuPDF versions before the text option was introduced.
            page.apply_redactions(**redaction_options)


def _insert_pdf_translations(pdf_document, segments: list[dict[str, Any]]) -> None:
    import fitz

    for segment in segments:
        layout = segment["layout"]
        page = pdf_document[int(layout["page_number"]) - 1]
        rect = fitz.Rect(layout["bbox"])
        translated_text = html.escape(segment["translated_text"]).replace("\n", "<br>")
        font_size = max(4.0, float(layout.get("font_size") or 10.0))
        font_color = str(layout.get("font_color") or "#000000")
        font_family = str(layout.get("font_family") or "sans-serif")
        text_align = str(layout.get("text_align") or "left")
        css = (
            "* { margin: 0; padding: 0; } "
            f"body {{ font-family: {font_family}; font-size: {font_size:.2f}pt; "
            f"line-height: 1.08; color: {font_color}; text-align: {text_align}; }}"
        )
        spare_height, scale = page.insert_htmlbox(
            rect,
            f"<div>{translated_text}</div>",
            css=css,
            scale_low=PDF_TRANSLATION_MIN_SCALE,
            overlay=True,
        )
        if spare_height < 0:
            raise DocumentTranslationError(
                f"PDF 第 {layout['page_number']} 页文本块 {segment['segment_id']} "
                "无法在原区域内完整排版，请缩短译文或降低 PDF_TRANSLATION_MIN_SCALE"
            )
        logger.debug(
            "translation_pdf_block_fitted document_page=%s segment_id=%s scale=%.3f",
            layout["page_number"],
            segment["segment_id"],
            scale,
        )


def _render_pdf_from_original(
    document_row: dict[str, Any], result: dict[str, Any]
) -> bytes:
    if document_row.get("file_type") != "pdf":
        raise DocumentTranslationError("只有 PDF 原文件可以导出原版式 PDF")
    if result.get("mode") != "translation":
        raise DocumentTranslationError("原版式 PDF 暂只支持纯译文模式")
    source_path = Path(str(document_row.get("storage_path") or ""))
    if not source_path.is_file():
        raise DocumentTranslationError("PDF 原文件不存在")

    import fitz

    segments = _pdf_segments_with_layout(document_row, result["segments"])
    try:
        with fitz.open(str(source_path)) as output_document:
            if output_document.needs_pass:
                raise DocumentTranslationError("暂不支持加密 PDF 的原版式翻译")
            _apply_pdf_text_redactions(output_document, segments)
            _insert_pdf_translations(output_document, segments)
            return output_document.tobytes(garbage=4, deflate=True)
    except DocumentTranslationError:
        raise
    except Exception as exc:
        logger.exception(
            "translation_pdf_export_failed document_id=%s translation_mode=%s",
            document_row.get("id"),
            result.get("mode"),
        )
        raise DocumentTranslationError("原版式 PDF 生成失败") from exc


def render_translation_export(
    user_id: str, document_id: str, translation_id: str, export_format: str
) -> tuple[bytes, str, str]:
    document, result = get_translation(user_id, document_id, translation_id)
    if export_format not in {"docx", "txt", "pdf"}:
        raise DocumentTranslationError("仅支持导出 PDF、DOCX 或 TXT")
    bilingual = result["mode"] == "bilingual"
    segments = result["segments"]
    if export_format == "pdf":
        payload = _render_pdf_from_original(document, result)
        media_type = "application/pdf"
    elif export_format == "txt":
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
        payload = None if bilingual else _render_docx_from_original(document, segments)
        if payload is None:
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
