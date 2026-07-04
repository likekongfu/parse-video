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

# 输出 / 下载配置
OUTPUT_DIR = Path(os.getenv("DOCUMENT_CONVERTER_OUTPUT_DIR", "data/document-output"))
PUBLIC_BASE_URL = os.getenv("DOCUMENT_CONVERTER_PUBLIC_BASE_URL", "").strip().rstrip("/")
OUTPUT_TTL_SECONDS = int(os.getenv("DOCUMENT_CONVERTER_OUTPUT_TTL_SECONDS", "7200"))
ALLOWED_DOWNLOAD_EXTENSIONS: set[str] = {".png", ".jpg", ".jpeg", ".pdf", ".docx"}

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


def render_pdf_pages(
    input_path: Path, page_range: str | None, output_dir: Path
) -> list[dict]:
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

    written_files: list[Path] = []
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

            # 写入输出目录，UUID 文件名
            server_name = uuid.uuid4().hex + ".png"
            image_path = output_dir / server_name
            image_path.write_bytes(image_bytes)
            written_files.append(image_path)

            download_url = ""
            if PUBLIC_BASE_URL:
                download_url = f"{PUBLIC_BASE_URL}/files/{server_name}"

            images.append(
                {
                    "page": page_no,
                    "name": f"pdf-page-{page_no}-{index}.png",
                    "imageType": "png",
                    "size": len(image_bytes),
                    "downloadUrl": download_url,
                }
            )
        return images
    except Exception:
        # 渲染失败时清理已写入的图片文件
        for fp in written_files:
            try:
                fp.unlink()
            except OSError:
                pass
        raise
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


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def cleanup_expired_outputs() -> int:
    """删除超过 TTL 的输出图片。返回删除数量。"""
    ensure_dir(OUTPUT_DIR)
    now = __import__("time").time()
    removed = 0
    for entry in OUTPUT_DIR.iterdir():
        if not entry.is_file():
            continue
        try:
            if now - entry.stat().st_mtime >= OUTPUT_TTL_SECONDS:
                entry.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def validate_download_filename(filename: str) -> str:
    """下载接口安全校验：禁止目录穿越，只允许白名单后缀。"""
    name = Path(filename).name
    if name != filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename",
        )
    if ".." in name or "/" in name or "\\" in name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename",
        )
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_DOWNLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only {', '.join(sorted(ALLOWED_DOWNLOAD_EXTENSIONS))} files are allowed",
        )
    return name


@app.get("/files/{filename}", dependencies=[Depends(require_token)])
async def download_file(filename: str):
    # ---- 1. 安全校验 ----
    safe_name = validate_download_filename(filename)

    # ---- 2. 查找文件 ----
    file_path = OUTPUT_DIR / safe_name
    resolved = file_path.resolve()
    allowed = OUTPUT_DIR.resolve()
    if not str(resolved).startswith(str(allowed) + os.sep) and resolved != allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found or expired",
        )

    # ---- 3. 确定 Content-Type ----
    suffix = file_path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        media_type = "image/jpeg"
    elif suffix == ".pdf":
        media_type = "application/pdf"
    elif suffix == ".docx":
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    else:
        media_type = "image/png"

    return FileResponse(
        file_path,
        media_type=media_type,
        filename=safe_name,
        headers={"Cache-Control": "no-store"},
    )


@app.on_event("startup")
async def startup_event() -> None:
    ensure_dir(OUTPUT_DIR)
    cleanup_expired_outputs()


@app.get("/health", include_in_schema=False)
def health_check() -> dict:
    return {"status": "ok", "service": "document-converter"}


@app.post("/document/word-to-pdf", dependencies=[Depends(require_token)])
async def word_to_pdf(file: UploadFile = File(...)):
    filename = safe_filename(file.filename)
    validate_extension(filename)

    work_dir = Path(tempfile.mkdtemp(prefix="word-to-pdf-"))
    input_path = work_dir / (uuid.uuid4().hex + Path(filename).suffix.lower())
    server_name = uuid.uuid4().hex + ".pdf"
    ensure_dir(OUTPUT_DIR)
    output_path = OUTPUT_DIR / server_name

    try:
        size = await save_upload(file, input_path)
        if size <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded file is empty",
            )
        # LibreOffice 输出到临时目录，再移动到 OUTPUT_DIR
        temp_output_dir = work_dir / "out"
        temp_output_dir.mkdir(parents=True, exist_ok=True)
        temp_pdf = run_libreoffice(input_path, temp_output_dir)
        # 移动到 OUTPUT_DIR 并重命名为 UUID
        shutil.move(str(temp_pdf), str(output_path))
        file_size = output_path.stat().st_size
        output_name = Path(filename).with_suffix(".pdf").name

        download_url = ""
        if PUBLIC_BASE_URL:
            download_url = f"{PUBLIC_BASE_URL}/files/{server_name}"

        cleanup_expired_outputs()
        return api_response(
            0,
            "ok",
            {
                "fileName": output_name,
                "size": file_size,
                "downloadUrl": download_url,
                "expiresIn": OUTPUT_TTL_SECONDS,
            },
        )
    except Exception:
        # 转换失败时清理残留输出文件（输入由 finally 统一清理）
        try:
            output_path.unlink()
        except OSError:
            pass
        raise
    finally:
        # 输入文件转换结束后立即删除
        cleanup_paths([work_dir])


@app.post("/document/pdf-to-word", dependencies=[Depends(require_token)])
async def pdf_to_word(file: UploadFile = File(...)):
    filename = safe_filename(file.filename)
    validate_pdf_extension(filename)

    work_dir = Path(tempfile.mkdtemp(prefix="pdf-to-word-"))
    input_path = work_dir / (uuid.uuid4().hex + ".pdf")
    server_name = uuid.uuid4().hex + ".docx"
    ensure_dir(OUTPUT_DIR)
    output_path = OUTPUT_DIR / server_name

    try:
        size = await save_upload(file, input_path)
        if size <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded file is empty",
            )
        docx_path = run_pdf2docx(input_path, output_path)
        file_size = docx_path.stat().st_size
        output_name = Path(filename).with_suffix(".docx").name

        download_url = ""
        if PUBLIC_BASE_URL:
            download_url = f"{PUBLIC_BASE_URL}/files/{server_name}"

        cleanup_expired_outputs()
        return api_response(
            0,
            "ok",
            {
                "fileName": output_name,
                "size": file_size,
                "downloadUrl": download_url,
                "expiresIn": OUTPUT_TTL_SECONDS,
            },
        )
    except Exception:
        # 转换失败时清理残留输出文件（输入由 finally 统一清理）
        try:
            output_path.unlink()
        except OSError:
            pass
        raise
    finally:
        # 输入文件转换结束后立即删除
        cleanup_paths([work_dir])


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
        ensure_dir(OUTPUT_DIR)
        images = render_pdf_pages(input_path, pages, OUTPUT_DIR)
        cleanup_expired_outputs()
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
