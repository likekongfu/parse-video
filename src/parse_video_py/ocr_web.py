"""Authenticated PaddleOCR endpoint for images and scanned PDFs."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import fitz
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from parse_video_py.ocr_service import OcrUnavailableError, recognize_images
from parse_video_py.qr_auth import verify_web_session

router = APIRouter(prefix="/auth/ocr", tags=["ocr"])
MAX_UPLOAD_BYTES = int(os.getenv("OCR_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
MAX_PDF_PAGES = int(os.getenv("OCR_MAX_PDF_PAGES", "20"))
PDF_RENDER_SCALE = float(os.getenv("OCR_PDF_RENDER_SCALE", "2.0"))
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}


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


@router.post("")
async def recognize_file(request: Request, file: UploadFile = File(...)):
    _current_user(request)
    filename = (file.filename or "").strip()
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        await file.close()
        raise HTTPException(status_code=400, detail="仅支持 JPG、PNG、PDF 文件")
    with tempfile.TemporaryDirectory(prefix="paddle-ocr-") as temporary:
        directory = Path(temporary)
        source = directory / f"source{extension}"
        size = await _save_upload(file, source)
        _validate_signature(source, extension)
        pages = await run_in_threadpool(_prepare_pages, source, extension, directory)
        try:
            results = await run_in_threadpool(recognize_images, pages)
        except OcrUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
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
