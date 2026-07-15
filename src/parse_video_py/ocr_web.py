"""Authenticated PaddleOCR endpoint for images and scanned PDFs."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import fitz
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from parse_video_py.ocr_service import OcrUnavailableError, recognize_images
from parse_video_py.qr_auth import verify_web_session

router = APIRouter(prefix="/auth/ocr", tags=["ocr"])
MAX_UPLOAD_BYTES = int(os.getenv("OCR_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
MAX_PDF_PAGES = int(os.getenv("OCR_MAX_PDF_PAGES", "20"))
PDF_RENDER_SCALE = float(os.getenv("OCR_PDF_RENDER_SCALE", "2.0"))
TASK_TTL_SECONDS = int(os.getenv("OCR_TASK_TTL_SECONDS", "7200"))
TASK_WORKERS = max(1, int(os.getenv("OCR_TASK_WORKERS", "1")))
MAX_PENDING_TASKS = max(1, int(os.getenv("OCR_MAX_PENDING_TASKS", "20")))
TASK_ROOT = Path(
    os.getenv(
        "OCR_TASK_DIR",
        str(Path(tempfile.gettempdir()) / "parse-video-py-ocr-tasks"),
    )
)
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}
_tasks: dict[str, dict[str, Any]] = {}
_tasks_lock = threading.Lock()
_executor = ThreadPoolExecutor(
    max_workers=TASK_WORKERS, thread_name_prefix="paddle-ocr"
)


def _current_user(request: Request):
    token = request.cookies.get("web_session", "")
    if not token:
        raise HTTPException(status_code=401, detail="请先登录")
    try:
        return verify_web_session(token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="登录状态无效或已过期") from exc


async def _save_upload(upload: UploadFile, target: Path) -> int:
    total = 0
    try:
        with target.open("wb") as stream:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="OCR 文件不能超过 20MB")
                stream.write(chunk)
    finally:
        await upload.close()
    if not total:
        raise HTTPException(status_code=400, detail="上传文件不能为空")
    return total


def _validate_signature(path: Path, extension: str) -> None:
    header = path.read_bytes()[:8]
    valid = (
        (extension == ".pdf" and header.startswith(b"%PDF-"))
        or (extension == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n"))
        or (extension in {".jpg", ".jpeg"} and header.startswith(b"\xff\xd8\xff"))
    )
    if not valid:
        raise HTTPException(status_code=400, detail="文件内容与扩展名不匹配")


def _prepare_pages(source: Path, extension: str, directory: Path) -> list[Path]:
    if extension != ".pdf":
        return [source]
    try:
        document = fitz.open(source)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="PDF 文件无法读取") from exc
    try:
        if document.needs_pass:
            raise HTTPException(status_code=400, detail="暂不支持加密 PDF")
        if document.page_count > MAX_PDF_PAGES:
            raise HTTPException(
                status_code=400, detail=f"PDF 不能超过 {MAX_PDF_PAGES} 页"
            )
        if document.page_count < 1:
            raise HTTPException(status_code=400, detail="PDF 没有可识别页面")
        paths: list[Path] = []
        matrix = fitz.Matrix(PDF_RENDER_SCALE, PDF_RENDER_SCALE)
        for index, page in enumerate(document):
            target = directory / f"page-{index + 1}.png"
            page.get_pixmap(matrix=matrix, alpha=False).save(target)
            paths.append(target)
        return paths
    finally:
        document.close()


def _result_payload(
    filename: str,
    size: int,
    extension: str,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    lines = [line for page in results for line in page["lines"]]
    confidences = [
        line["confidence"] for line in lines if line["confidence"] is not None
    ]
    return {
        "filename": filename,
        "file_size": size,
        "file_type": extension.lstrip("."),
        "engine": "PaddleOCR",
        "total_pages": len(results),
        "total_lines": len(lines),
        "average_confidence": (
            round(sum(confidences) / len(confidences), 6) if confidences else None
        ),
        "full_text": "\n\n".join(page["text"] for page in results if page["text"]),
        "pages": results,
    }


def _cleanup_tasks(now: float | None = None) -> None:
    cutoff = (now or time.time()) - TASK_TTL_SECONDS
    with _tasks_lock:
        stale = [
            job_id
            for job_id, task in _tasks.items()
            if task["status"] != "processing" and task["updated_at"] < cutoff
        ]
        for job_id in stale:
            _tasks.pop(job_id, None)


def _update_task(job_id: str, **values: Any) -> None:
    with _tasks_lock:
        task = _tasks.get(job_id)
        if task is not None:
            task.update(values, updated_at=time.time())


def _run_ocr_task(
    job_id: str,
    directory: Path,
    source: Path,
    extension: str,
    filename: str,
    size: int,
) -> None:
    try:
        pages = _prepare_pages(source, extension, directory)
        results = recognize_images(pages)
        _update_task(
            job_id,
            status="completed",
            result=_result_payload(filename, size, extension, results),
        )
    except OcrUnavailableError as exc:
        _update_task(job_id, status="failed", error=str(exc))
    except HTTPException as exc:
        _update_task(job_id, status="failed", error=str(exc.detail))
    except Exception as exc:
        _update_task(
            job_id,
            status="failed",
            error=f"OCR 识别失败：{type(exc).__name__}: {exc}",
        )
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@router.post("", status_code=202)
async def recognize_file(request: Request, file: UploadFile = File(...)):
    user = _current_user(request)
    _cleanup_tasks()
    filename = (file.filename or "").strip()
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        await file.close()
        raise HTTPException(status_code=400, detail="仅支持 JPG、PNG、PDF 文件")
    with _tasks_lock:
        pending = sum(task["status"] == "processing" for task in _tasks.values())
    if pending >= MAX_PENDING_TASKS:
        await file.close()
        raise HTTPException(status_code=429, detail="OCR 任务较多，请稍后重试")

    TASK_ROOT.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    directory = Path(tempfile.mkdtemp(prefix=f"{job_id}-", dir=TASK_ROOT))
    source = directory / f"source{extension}"
    try:
        size = await _save_upload(file, source)
        _validate_signature(source, extension)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise

    now = time.time()
    with _tasks_lock:
        _tasks[job_id] = {
            "job_id": job_id,
            "user_id": str(user.id),
            "status": "processing",
            "filename": filename,
            "created_at": now,
            "updated_at": now,
            "result": None,
            "error": None,
        }
    _executor.submit(
        _run_ocr_task, job_id, directory, source, extension, filename, size
    )
    return {"job_id": job_id, "status": "processing"}


@router.get("/status/{job_id}")
def recognize_status(request: Request, job_id: str):
    user = _current_user(request)
    _cleanup_tasks()
    with _tasks_lock:
        task = _tasks.get(job_id)
        if task is None or task["user_id"] != str(user.id):
            raise HTTPException(status_code=404, detail="OCR 任务不存在或已过期")
        status = task["status"]
        if status == "completed":
            return {"job_id": job_id, "status": status, "result": task["result"]}
        if status == "failed":
            return {"job_id": job_id, "status": status, "error": task["error"]}
        return {"job_id": job_id, "status": status}
