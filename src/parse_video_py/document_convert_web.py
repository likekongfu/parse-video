import base64
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Iterable

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pdf2docx import Converter
from starlette.background import BackgroundTask


ALLOWED_EXTENSIONS = {".doc", ".docx", ".rtf", ".odt"}
PDF_EXTENSIONS = {".pdf"}
MAX_UPLOAD_BYTES = int(os.getenv("DOCUMENT_CONVERTER_MAX_UPLOAD_BYTES", "15728640"))
CONVERT_TIMEOUT_SECONDS = int(os.getenv("DOCUMENT_CONVERTER_TIMEOUT_SECONDS", "90"))
PDF_IMAGE_ZOOM = float(os.getenv("DOCUMENT_CONVERTER_PDF_IMAGE_ZOOM", "1.6"))
PDF_IMAGE_MAX_PAGES = int(os.getenv("DOCUMENT_CONVERTER_PDF_IMAGE_MAX_PAGES", "20"))
PDF_IMAGE_MAX_RESPONSE_BYTES = int(
    os.getenv("DOCUMENT_CONVERTER_PDF_IMAGE_MAX_RESPONSE_BYTES", "12582912")
)
LIBREOFFICE_BIN = os.getenv("LIBREOFFICE_BIN", "libreoffice")
API_TOKEN = os.getenv("DOCUMENT_CONVERTER_TOKEN", "").strip()
DISABLE_DOCS = os.getenv("DISABLE_DOCS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

app = FastAPI(
    title="Document Converter Service",
    docs_url=None if DISABLE_DOCS else "/docs",
    redoc_url=None if DISABLE_DOCS else "/redoc",
    openapi_url=None if DISABLE_DOCS else "/openapi.json",
)


def require_token(authorization: str | None = Header(default=None)) -> None:
    if not API_TOKEN:
        return
    expected = "Bearer " + API_TOKEN
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid converter token",
        )


def api_response(code: int, msg: str, data: dict | None = None) -> dict:
    body = {"code": code, "msg": msg}
    if data is not None:
        body["data"] = data
    return body


def safe_filename(name: str | None) -> str:
    raw_name = Path(name or "document.docx").name
    cleaned = "".join(ch for ch in raw_name if ch.isalnum() or ch in "._- ()[]")
    return cleaned or "document.docx"


def validate_extension(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type. Allowed: {allowed}",
        )
    return suffix


def validate_pdf_extension(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in PDF_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported file type. Allowed: .pdf",
        )
    return suffix


def parse_page_range(text: str | None, total_pages: int) -> list[int]:
    value = (text or "").replace(" ", "").strip()
    if not value:
        return list(range(1, total_pages + 1))
    pages: list[int] = []
    for part in value.split(","):
        if not part:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid page range",
            )
        start_text, separator, end_text = part.partition("-")
        if not start_text.isdigit() or (separator and not end_text.isdigit()):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid page range",
            )
        start = int(start_text)
        end = int(end_text or start_text)
        if start < 1 or end < start or end > total_pages:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Page range must be between 1 and {total_pages}",
            )
        for page in range(start, end + 1):
            if page not in pages:
                pages.append(page)
    return pages


async def save_upload(upload: UploadFile, target_path: Path) -> int:
    total = 0
    with target_path.open("wb") as target_file:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="File is too large",
                )
            target_file.write(chunk)
    return total


def build_soffice_command(input_path: Path, output_dir: Path) -> list[str]:
    return [
        LIBREOFFICE_BIN,
        "--headless",
        "--nologo",
        "--nofirststartwizard",
        "--norestore",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output_dir),
        str(input_path),
    ]


def run_libreoffice(input_path: Path, output_dir: Path) -> Path:
    command = build_soffice_command(input_path, output_dir)
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=CONVERT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="LibreOffice is not installed or LIBREOFFICE_BIN is invalid",
        ) from error
    except subprocess.TimeoutExpired as error:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Document conversion timed out",
        ) from error

    pdf_path = output_dir / (input_path.stem + ".pdf")
    if result.returncode != 0 or not pdf_path.exists():
        message = (result.stderr or result.stdout or "LibreOffice conversion failed").strip()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=message[-800:],
        )
    return pdf_path


def run_pdf2docx(input_path: Path, output_path: Path) -> Path:
    converter = Converter(str(input_path))
    try:
        converter.convert(str(output_path), start=0, end=None)
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error)[-800:] or "PDF to Word conversion failed",
        ) from error
    finally:
        converter.close()

    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="PDF to Word conversion failed",
        )
    return output_path


def render_pdf_pages(input_path: Path, page_range: str | None) -> list[dict]:
    try:
        import fitz
    except ImportError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="PyMuPDF is not installed",
        ) from error

    try:
        document = fitz.open(str(input_path))
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error)[-800:] or "Failed to open PDF",
        ) from error

    try:
        total_pages = document.page_count
        pages = parse_page_range(page_range, total_pages)
        if not pages:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No pages selected",
            )
        if len(pages) > PDF_IMAGE_MAX_PAGES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Too many pages selected. Max: {PDF_IMAGE_MAX_PAGES}",
            )

        matrix = fitz.Matrix(PDF_IMAGE_ZOOM, PDF_IMAGE_ZOOM)
        images = []
        total_bytes = 0
        for index, page_no in enumerate(pages, start=1):
            page = document.load_page(page_no - 1)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            image_bytes = pixmap.tobytes("png")
            total_bytes += len(image_bytes)
            if total_bytes > PDF_IMAGE_MAX_RESPONSE_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="Rendered images are too large, please select fewer pages",
                )
            images.append(
                {
                    "page": page_no,
                    "name": f"pdf-page-{page_no}-{index}.png",
                    "mimeType": "image/png",
                    "size": len(image_bytes),
                    "base64": base64.b64encode(image_bytes).decode("ascii"),
                }
            )
        return images
    finally:
        document.close()


def cleanup_paths(paths: Iterable[Path]) -> None:
    for path in paths:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        except OSError:
            pass


@app.get("/health", include_in_schema=False)
def health_check() -> dict:
    return {"status": "ok", "service": "document-converter"}


@app.post("/document/word-to-pdf", dependencies=[Depends(require_token)])
async def word_to_pdf(file: UploadFile = File(...)):
    return await convert_to_pdf(file)


@app.post("/document/pdf-to-word", dependencies=[Depends(require_token)])
async def pdf_to_word(file: UploadFile = File(...)):
    filename = safe_filename(file.filename)
    validate_pdf_extension(filename)

    work_dir = Path(tempfile.mkdtemp(prefix="pdf-to-word-"))
    input_path = work_dir / (uuid.uuid4().hex + ".pdf")
    output_path = work_dir / (uuid.uuid4().hex + ".docx")

    try:
        size = await save_upload(file, input_path)
        if size <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded file is empty",
            )
        docx_path = run_pdf2docx(input_path, output_path)
        output_name = Path(filename).with_suffix(".docx").name
        return FileResponse(
            docx_path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=output_name,
            background=BackgroundTask(cleanup_paths, [work_dir]),
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        cleanup_paths([work_dir])
        raise


@app.post("/document/pdf-to-images", dependencies=[Depends(require_token)])
async def pdf_to_images(
    file: UploadFile = File(...),
    pages: str = Form(default=""),
):
    filename = safe_filename(file.filename)
    validate_pdf_extension(filename)

    work_dir = Path(tempfile.mkdtemp(prefix="pdf-to-images-"))
    input_path = work_dir / (uuid.uuid4().hex + ".pdf")

    try:
        size = await save_upload(file, input_path)
        if size <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded file is empty",
            )
        images = render_pdf_pages(input_path, pages)
        return api_response(
            0,
            "ok",
            {
                "sourceFileName": filename,
                "imageCount": len(images),
                "images": images,
            },
        )
    finally:
        cleanup_paths([work_dir])


@app.post("/document/convert-to-pdf", dependencies=[Depends(require_token)])
async def convert_to_pdf(file: UploadFile = File(...)):
    filename = safe_filename(file.filename)
    validate_extension(filename)

    work_dir = Path(tempfile.mkdtemp(prefix="doc-convert-"))
    input_path = work_dir / (uuid.uuid4().hex + Path(filename).suffix.lower())
    output_dir = work_dir / "out"
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        size = await save_upload(file, input_path)
        if size <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded file is empty",
            )
        pdf_path = run_libreoffice(input_path, output_dir)
        output_name = Path(filename).with_suffix(".pdf").name
        return FileResponse(
            pdf_path,
            media_type="application/pdf",
            filename=output_name,
            background=BackgroundTask(cleanup_paths, [work_dir]),
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        cleanup_paths([work_dir])
        raise
