import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Iterable

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pdf2docx import Converter
from starlette.background import BackgroundTask

# 内容安全审核
from parse_video_py.content_security import (
    WxSecurityError,
    WxSecurityRejectedError,
    WxSecurityServiceError,
    extract_pdf_text,
    check_text,
    verify_openid_token,
)
from parse_video_py.content_security import WX_CONTENT_SECURITY_ENABLED as _SEC_ENABLED

ALLOWED_EXTENSIONS = {".doc", ".docx", ".rtf", ".odt"}
PDF_EXTENSIONS = {".pdf"}
MAX_UPLOAD_BYTES = int(os.getenv("DOCUMENT_CONVERTER_MAX_UPLOAD_BYTES", "20971520"))
CONVERT_TIMEOUT_SECONDS = int(os.getenv("DOCUMENT_CONVERTER_TIMEOUT_SECONDS", "90"))
PDF_IMAGE_ZOOM = float(os.getenv("DOCUMENT_CONVERTER_PDF_IMAGE_ZOOM", "1.6"))
PDF_IMAGE_MAX_PAGES = int(os.getenv("DOCUMENT_CONVERTER_PDF_IMAGE_MAX_PAGES", "20"))
PDF_IMAGE_MAX_RESPONSE_BYTES = int(
    os.getenv("DOCUMENT_CONVERTER_PDF_IMAGE_MAX_RESPONSE_BYTES", "12582912")
)
PDF_PASSWORD_MIN_LENGTH = int(
    os.getenv("DOCUMENT_CONVERTER_PDF_PASSWORD_MIN_LENGTH", "6")
)
PDF_PASSWORD_MAX_LENGTH = int(
    os.getenv("DOCUMENT_CONVERTER_PDF_PASSWORD_MAX_LENGTH", "32")
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
PUBLIC_BASE_URL = (
    os.getenv("DOCUMENT_CONVERTER_PUBLIC_BASE_URL", "").strip().rstrip("/")
)
OUTPUT_TTL_SECONDS = int(os.getenv("DOCUMENT_CONVERTER_OUTPUT_TTL_SECONDS", "7200"))
ALLOWED_DOWNLOAD_EXTENSIONS: set[str] = {".png", ".jpg", ".jpeg", ".pdf", ".docx"}

PDF_COMPRESS_LEVELS = {
    "normal": {
        "dpi_threshold": 180,
        "dpi_target": 150,
        "quality": 82,
        "garbage": 3,
        "compression_effort": 6,
    },
    "heavy": {
        "dpi_threshold": 150,
        "dpi_target": 110,
        "quality": 68,
        "garbage": 4,
        "compression_effort": 8,
    },
    "extreme": {
        "dpi_threshold": 120,
        "dpi_target": 96,
        "quality": 55,
        "garbage": 4,
        "compression_effort": 9,
    },
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


def validate_pdf_password(password: str) -> str:
    value = (password or "").strip()
    if not value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password is required",
        )
    if len(value) < PDF_PASSWORD_MIN_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Password must be at least {PDF_PASSWORD_MIN_LENGTH} characters",
        )
    if len(value) > PDF_PASSWORD_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Password must be at most {PDF_PASSWORD_MAX_LENGTH} characters",
        )
    return value


def validate_compress_level(level: str | None) -> str:
    value = (level or "normal").strip().lower()
    if value not in PDF_COMPRESS_LEVELS:
        allowed = ", ".join(sorted(PDF_COMPRESS_LEVELS))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported compression level. Allowed: {allowed}",
        )
    return value


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
        message = (
            result.stderr or result.stdout or "LibreOffice conversion failed"
        ).strip()
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


def build_pdf_permissions(
    allow_print: bool,
    allow_copy: bool,
    allow_modify: bool,
) -> int:
    try:
        import fitz
    except ImportError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="PyMuPDF is not installed",
        ) from error

    permissions = fitz.PDF_PERM_ACCESSIBILITY
    if allow_print:
        permissions |= fitz.PDF_PERM_PRINT | fitz.PDF_PERM_PRINT_HQ
    if allow_copy:
        permissions |= fitz.PDF_PERM_COPY
    if allow_modify:
        permissions |= (
            fitz.PDF_PERM_MODIFY
            | fitz.PDF_PERM_ANNOTATE
            | fitz.PDF_PERM_FORM
            | fitz.PDF_PERM_ASSEMBLE
        )
    return permissions


def build_pikepdf_permissions(
    allow_print: bool,
    allow_copy: bool,
    allow_modify: bool,
):
    try:
        import pikepdf
    except ImportError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="pikepdf is not installed",
        ) from error

    return pikepdf.Permissions(
        accessibility=True,
        extract=allow_copy,
        modify_annotation=allow_modify,
        modify_assembly=allow_modify,
        modify_form=allow_modify,
        modify_other=allow_modify,
        print_lowres=allow_print,
        print_highres=allow_print,
    )


def run_pdf_encrypt(
    input_path: Path,
    output_path: Path,
    password: str,
    allow_print: bool = True,
    allow_copy: bool = False,
    allow_modify: bool = False,
) -> Path:
    try:
        import pikepdf
    except ImportError:
        return run_pdf_encrypt_with_pymupdf(
            input_path,
            output_path,
            password,
            allow_print=allow_print,
            allow_copy=allow_copy,
            allow_modify=allow_modify,
        )

    try:
        with pikepdf.open(str(input_path)) as pdf:
            pdf.save(
                str(output_path),
                encryption=pikepdf.Encryption(
                    owner=uuid.uuid4().hex + uuid.uuid4().hex,
                    user=password,
                    R=6,
                    allow=build_pikepdf_permissions(
                        allow_print=allow_print,
                        allow_copy=allow_copy,
                        allow_modify=allow_modify,
                    ),
                ),
            )
    except pikepdf.PasswordError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Encrypted PDF is not supported",
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error)[-800:] or "PDF encryption failed",
        ) from error

    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="PDF encryption failed",
        )
    return output_path


def run_pdf_encrypt_with_pymupdf(
    input_path: Path,
    output_path: Path,
    password: str,
    allow_print: bool = True,
    allow_copy: bool = False,
    allow_modify: bool = False,
) -> Path:
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
        if document.needs_pass:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Encrypted PDF is not supported",
            )
        document.save(
            str(output_path),
            garbage=4,
            clean=True,
            deflate=True,
            deflate_images=True,
            deflate_fonts=True,
            use_objstms=1,
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw=uuid.uuid4().hex,
            user_pw=password,
            permissions=build_pdf_permissions(
                allow_print=allow_print,
                allow_copy=allow_copy,
                allow_modify=allow_modify,
            ),
        )
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error)[-800:] or "PDF encryption failed",
        ) from error
    finally:
        document.close()

    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="PDF encryption failed",
        )
    return output_path


def run_pdf_compress(input_path: Path, output_path: Path, level: str) -> Path:
    try:
        import fitz
    except ImportError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="PyMuPDF is not installed",
        ) from error

    options = PDF_COMPRESS_LEVELS[level]
    try:
        document = fitz.open(str(input_path))
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error)[-800:] or "Failed to open PDF",
        ) from error

    try:
        if document.needs_pass:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Encrypted PDF is not supported",
            )

        if hasattr(document, "rewrite_images"):
            document.rewrite_images(
                dpi_threshold=options["dpi_threshold"],
                dpi_target=options["dpi_target"],
                quality=options["quality"],
                lossy=True,
                lossless=True,
                bitonal=True,
                color=True,
                gray=True,
            )

        document.save(
            str(output_path),
            garbage=options["garbage"],
            clean=True,
            deflate=True,
            deflate_images=True,
            deflate_fonts=True,
            use_objstms=1,
            compression_effort=options["compression_effort"],
        )
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error)[-800:] or "PDF compression failed",
        ) from error
    finally:
        document.close()

    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="PDF compression failed",
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
        media_type = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
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
    openid: str = Form(default=""),
    openid_token: str = Form(default=""),
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

        # ---- 内容安全：提取 PDF 文本并审核（同步） ----
        if _SEC_ENABLED:
            # 优先使用 openid_token（签名验证），fallback 到原始 openid（向后兼容）
            verified_openid = ""
            if openid_token and openid_token.strip():
                try:
                    verified_openid = verify_openid_token(openid_token)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"openid_token 无效: {exc}",
                    )
                except WxSecurityError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail=f"openid 验证服务异常: {exc.message}",
                    )
            elif openid and openid.strip():
                # 向后兼容：允许旧版直接传 openid（不推荐）
                verified_openid = openid.strip()
            else:
                # 安全审核已开启但未提供 openid → 拒绝
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="内容安全已开启，需提供有效的 openid_token",
                )

            try:
                pdf_text = extract_pdf_text(input_path, max_chars=2500)
                if pdf_text:
                    check_text(pdf_text, openid=verified_openid)
            except WxSecurityRejectedError:
                # 严格模式：内容违规 → 清理输入并拒绝
                cleanup_paths([work_dir])
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="内容安全审核不通过，请修改后重试",
                )
            except WxSecurityServiceError:
                # 审核服务异常 → 清理输入并拒绝（安全优先）
                cleanup_paths([work_dir])
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="内容安全服务异常，请稍后重试",
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
    except HTTPException:
        raise
    finally:
        cleanup_paths([work_dir])


@app.post("/document/pdf-compress", dependencies=[Depends(require_token)])
async def pdf_compress(
    file: UploadFile = File(...),
    level: str = Form(default="normal"),
):
    filename = safe_filename(file.filename)
    validate_pdf_extension(filename)
    compress_level = validate_compress_level(level)

    work_dir = Path(tempfile.mkdtemp(prefix="pdf-compress-"))
    input_path = work_dir / (uuid.uuid4().hex + ".pdf")
    server_name = uuid.uuid4().hex + ".pdf"
    ensure_dir(OUTPUT_DIR)
    output_path = OUTPUT_DIR / server_name

    try:
        original_size = await save_upload(file, input_path)
        if original_size <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded file is empty",
            )

        compressed_path = run_pdf_compress(input_path, output_path, compress_level)
        compressed_size = compressed_path.stat().st_size
        output_name = (
            Path(filename).with_suffix("").name + f"_compressed_{compress_level}.pdf"
        )
        saved_percent = 0.0
        if original_size > 0:
            saved_percent = round((1 - compressed_size / original_size) * 100, 1)

        download_url = ""
        if PUBLIC_BASE_URL:
            download_url = f"{PUBLIC_BASE_URL}/files/{server_name}"

        cleanup_expired_outputs()
        return api_response(
            0,
            "ok",
            {
                "fileName": output_name,
                "size": compressed_size,
                "originalSize": original_size,
                "compressedSize": compressed_size,
                "savedPercent": saved_percent,
                "level": compress_level,
                "downloadUrl": download_url,
                "expiresIn": OUTPUT_TTL_SECONDS,
            },
        )
    except Exception:
        try:
            output_path.unlink()
        except OSError:
            pass
        raise
    finally:
        cleanup_paths([work_dir])


@app.post("/document/pdf-encrypt", dependencies=[Depends(require_token)])
async def pdf_encrypt(
    file: UploadFile = File(...),
    password: str = Form(...),
    allow_print: bool = Form(default=True),
    allow_copy: bool = Form(default=False),
    allow_modify: bool = Form(default=False),
):
    filename = safe_filename(file.filename)
    validate_pdf_extension(filename)
    validated_password = validate_pdf_password(password)

    work_dir = Path(tempfile.mkdtemp(prefix="pdf-encrypt-"))
    input_path = work_dir / (uuid.uuid4().hex + ".pdf")
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

        encrypted_path = run_pdf_encrypt(
            input_path,
            output_path,
            validated_password,
            allow_print=allow_print,
            allow_copy=allow_copy,
            allow_modify=allow_modify,
        )
        file_size = encrypted_path.stat().st_size
        output_name = Path(filename).with_suffix("").name + "_encrypted.pdf"

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
                "allowPrint": allow_print,
                "allowCopy": allow_copy,
                "allowModify": allow_modify,
                "downloadUrl": download_url,
                "expiresIn": OUTPUT_TTL_SECONDS,
            },
        )
    except Exception:
        try:
            output_path.unlink()
        except OSError:
            pass
        raise
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
