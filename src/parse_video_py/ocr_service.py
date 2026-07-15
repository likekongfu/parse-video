"""Lazy PaddleOCR 3.x adapter and stable result normalization."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
import logging

logger = logging.getLogger(__name__)


class OcrUnavailableError(RuntimeError):
    """PaddleOCR is not installed or failed to initialize."""


_engine: Any | None = None
_engine_lock = threading.Lock()


def _get_engine():
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        try:
            from paddleocr import PaddleOCR

            _engine = PaddleOCR(
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                enable_mkldnn=False,
                cpu_threads=2
            )
        except Exception as exc:
            raise OcrUnavailableError(
                "PaddleOCR 初始化失败，请检查模型和运行依赖"
            ) from exc
    return _engine


def recognize_images(paths: list[Path]) -> list[dict[str, Any]]:
    engine = _get_engine()
    pages: list[dict[str, Any]] = []
    # Paddle predictors are not guaranteed to be thread-safe. Serialize inference
    # while still letting FastAPI run it outside the event loop.
    with _engine_lock:
        for page_number, path in enumerate(paths, start=1):
            try:
                results = engine.predict(str(path))
            except Exception as exc:
                logger.exception(
                    "第 %s 页 OCR 识别失败，图片路径：%s",
                    page_number,
                    path,
                )
                raise RuntimeError(
                    f"第 {page_number} 页 OCR 识别失败：" f"{type(exc).__name__}: {exc}"
                ) from exc
            lines: list[dict[str, Any]] = []
            for result in results or []:
                lines.extend(_normalize_result(result))
            pages.append(
                {
                    "page_number": page_number,
                    "text": "\n".join(line["text"] for line in lines),
                    "lines": lines,
                }
            )
    return pages


def _normalize_result(result: Any) -> list[dict[str, Any]]:
    payload = _to_mapping(result)
    if payload:
        data = (
            payload.get("res") if isinstance(payload.get("res"), Mapping) else payload
        )
        texts = data.get("rec_texts") if isinstance(data, Mapping) else None
        if isinstance(texts, Sequence) and not isinstance(texts, (str, bytes)):
            scores = data.get("rec_scores", [])
            boxes = data.get("rec_boxes", data.get("rec_polys", []))
            return [
                {
                    "text": str(text).strip(),
                    "confidence": _number_at(scores, index),
                    "bounding_box": _serializable_at(boxes, index),
                }
                for index, text in enumerate(texts)
                if str(text).strip()
            ]
    return _normalize_legacy(result)


def _to_mapping(result: Any) -> dict[str, Any] | None:
    if isinstance(result, Mapping):
        return dict(result)
    value = getattr(result, "json", None)
    if callable(value):
        value = value()
    if isinstance(value, Mapping):
        return dict(value)
    return None


def _normalize_legacy(value: Any) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return lines
    for item in value:
        if (
            isinstance(item, Sequence)
            and len(item) == 2
            and isinstance(item[1], Sequence)
            and len(item[1]) >= 2
            and isinstance(item[1][0], str)
        ):
            text = item[1][0].strip()
            if text:
                lines.append(
                    {
                        "text": text,
                        "confidence": _as_float(item[1][1]),
                        "bounding_box": _serializable(item[0]),
                    }
                )
        else:
            lines.extend(_normalize_legacy(item))
    return lines


def _number_at(values: Any, index: int) -> float | None:
    try:
        return _as_float(values[index])
    except (IndexError, KeyError, TypeError):
        return None


def _serializable_at(values: Any, index: int) -> Any | None:
    try:
        return _serializable(values[index])
    except (IndexError, KeyError, TypeError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _serializable(value: Any) -> Any:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_serializable(item) for item in value]
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)
