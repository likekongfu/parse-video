import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse


# ---------------------------------------------------------------------------
# 配置（全部通过环境变量注入，模块级常量）
# ---------------------------------------------------------------------------

ALLOWED_INPUT_EXTENSIONS: set[str] = {".mp4", ".mov"}
ALLOWED_OUTPUT_FORMATS: set[str] = {"mp3", "m4a"}

MAX_UPLOAD_BYTES: int = int(
    os.getenv("MEDIA_CONVERTER_MAX_UPLOAD_BYTES", "104857600")  # 100 MB
)
MAX_VIDEO_DURATION_SECONDS: int = int(
    os.getenv("MEDIA_CONVERTER_MAX_DURATION_SECONDS", "600")  # 10 分钟
)
CONVERT_TIMEOUT_SECONDS: int = int(
    os.getenv("MEDIA_CONVERTER_TIMEOUT_SECONDS", "300")
)
PROBE_TIMEOUT_SECONDS: int = int(
    os.getenv("MEDIA_CONVERTER_PROBE_TIMEOUT_SECONDS", "30")
)
OUTPUT_TTL_SECONDS: int = int(
    os.getenv("MEDIA_CONVERTER_OUTPUT_TTL_SECONDS", "7200")  # 2 小时
)

FFMPEG_BIN: str = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN: str = os.getenv("FFPROBE_BIN", "ffprobe")

# ---- 水印配置 ----
ALLOWED_WATERMARK_POSITIONS: set[str] = {
    "top_left", "top_right", "bottom_left", "bottom_right", "center",
}
WATERMARK_MAX_TEXT_CHARS: int = 40
WATERMARK_FONT_SIZE_MIN: int = 16
WATERMARK_FONT_SIZE_MAX: int = 96
WATERMARK_OPACITY_MIN: float = 0.1
WATERMARK_OPACITY_MAX: float = 1.0
WATERMARK_MARGIN_MIN: int = 0
WATERMARK_MARGIN_MAX: int = 100
WATERMARK_CONVERT_TIMEOUT_SECONDS: int = int(
    os.getenv("MEDIA_WATERMARK_TIMEOUT_SECONDS", "300")
)
WATERMARK_FONT_FILE: str = os.getenv(
    "MEDIA_WATERMARK_FONT_FILE",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)

API_TOKEN: str = os.getenv("MEDIA_CONVERTER_TOKEN", "").strip()
DISABLE_DOCS: bool = os.getenv("DISABLE_DOCS", "").strip().lower() in {
    "1", "true", "yes", "on",
}

# 公开访问基础地址 — 用于构造下载 URL
PUBLIC_BASE_URL: str = (
    os.getenv("MEDIA_CONVERTER_PUBLIC_BASE_URL", "").strip().rstrip("/")
)

OUTPUT_DIR: Path = Path(os.getenv("MEDIA_CONVERTER_OUTPUT_DIR", "data/media-output"))
UPLOAD_DIR: Path = Path(os.getenv("MEDIA_CONVERTER_UPLOAD_DIR", "data/media-upload"))


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Media Converter Service",
    docs_url=None if DISABLE_DOCS else "/docs",
    redoc_url=None if DISABLE_DOCS else "/redoc",
    openapi_url=None if DISABLE_DOCS else "/openapi.json",
)


# ---------------------------------------------------------------------------
# 鉴权（与 document-converter 保持一致的模式）
# ---------------------------------------------------------------------------

def require_token(authorization: str | None = Header(default=None)) -> None:
    if not API_TOKEN:
        return
    expected = "Bearer " + API_TOKEN
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid converter token",
        )


# ---------------------------------------------------------------------------
# 响应 / 错误构建
# ---------------------------------------------------------------------------

# 错误码映射：detail 消息前缀 → (code, message)
_ERROR_CODE_MAP: list[tuple[str, str, str]] = [
    # 格式在前，优先匹配更具体的消息
    ("Unsupported output format", "INVALID_OUTPUT_FORMAT", "Unsupported output format"),
    ("Unsupported input format", "INVALID_FORMAT", "Unsupported input file format"),
    ("File exceeds", "FILE_TOO_LARGE", "File exceeds 100 MB limit"),
    ("Uploaded file is empty", "FILE_EMPTY", "Uploaded file is empty"),
    ("Cannot probe media file", "INVALID_MEDIA", "Cannot recognize media file"),
    ("Cannot parse media probe", "INVALID_MEDIA", "Cannot recognize media file"),
    ("No streams found", "INVALID_MEDIA", "No streams found in media file"),
    ("does not contain a video stream", "NO_VIDEO_STREAM", "File does not contain a video stream"),
    ("does not contain an audio track", "NO_AUDIO_STREAM", "Video does not contain an audio track"),
    ("Cannot determine media duration", "INVALID_MEDIA", "Cannot determine media duration"),
    ("duration exceeds", "DURATION_EXCEEDED", "Video duration exceeds limit"),
    ("timed out", "PROCESS_TIMEOUT", "Processing timed out"),
    ("Audio extraction failed", "PROCESS_FAILED", "Audio extraction failed"),
    ("Audio extraction produced no output", "PROCESS_FAILED", "Audio extraction produced no output"),
    ("Watermark text is empty", "INVALID_WATERMARK_TEXT", "Watermark text cannot be empty"),
    ("Watermark text exceeds", "INVALID_WATERMARK_TEXT", "Watermark text exceeds 40 characters"),
    ("Invalid watermark position", "INVALID_WATERMARK_POSITION", "Invalid watermark position"),
    ("Invalid font size", "INVALID_WATERMARK_PARAM", "Font size must be 16-96"),
    ("Invalid font color format", "INVALID_WATERMARK_PARAM", "Font color must be #RRGGBB"),
    ("Invalid opacity", "INVALID_WATERMARK_PARAM", "Opacity must be 0.1-1.0"),
    ("Invalid margin", "INVALID_WATERMARK_PARAM", "Margin must be 0-100"),
    ("Watermark font file not found", "PROCESS_FAILED", "Watermark font file not found on server"),
    ("Watermark failed", "PROCESS_FAILED", "Watermark processing failed"),
    ("Watermark produced no output", "PROCESS_FAILED", "Watermark produced no output"),
    ("ffprobe is not installed", "PROCESS_FAILED", "Server processing capability unavailable"),
    ("ffmpeg is not installed", "PROCESS_FAILED", "Server processing capability unavailable"),
    ("Invalid converter token", "UNAUTHORIZED", "Invalid converter token"),
    ("Invalid filename", "INVALID_FILENAME", "Invalid filename"),
    ("Only .mp3 and .m4a", "INVALID_FORMAT", "Only .mp3, .m4a and .mp4 files are allowed"),
    ("File not found or expired", "FILE_EXPIRED", "File not found or expired"),
    ("Access denied", "ACCESS_DENIED", "Access denied"),
]


def _resolve_error(detail: str) -> tuple[str, str]:
    """根据 HTTPException detail 文本匹配错误码和消息。"""
    for prefix, code, message in _ERROR_CODE_MAP:
        if detail.startswith(prefix) or prefix in detail:
            return code, message
    return "UNKNOWN_ERROR", detail


def api_success(data: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"success": True}
    if data is not None:
        body.update(data)
    return body


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code, message = _resolve_error(str(exc.detail))
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "code": code,
            "message": message,
        },
    )


# ---------------------------------------------------------------------------
# 文件名校验
# ---------------------------------------------------------------------------

def safe_display_name(name: str | None) -> str:
    """从原始文件名中提取安全的展示用名称。"""
    raw = Path(name or "video.mp4").name
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in "._- ()[]")
    return cleaned or "video.mp4"


def validate_output_format(output_format: str) -> str:
    fmt = output_format.strip().lower()
    if fmt not in ALLOWED_OUTPUT_FORMATS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported output format. Allowed: {', '.join(sorted(ALLOWED_OUTPUT_FORMATS))}",
        )
    return fmt


def validate_input_extension(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_INPUT_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported input format. Allowed: {', '.join(sorted(ALLOWED_INPUT_EXTENSIONS))}",
        )
    return suffix


def validate_safe_filename(filename: str) -> str:
    """下载接口安全校验：禁止目录穿越，只允许 mp3/m4a/mp4。"""
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
    if suffix not in {".mp3", ".m4a", ".mp4"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .mp3, .m4a and .mp4 files are allowed",
        )
    return name


# ---------------------------------------------------------------------------
# 水印参数校验
# ---------------------------------------------------------------------------

_WATERMARK_COLOR_RE = __import__("re").compile(r"^#[0-9A-Fa-f]{6}$")


def validate_watermark_text(text: str) -> str:
    t = (text or "").strip()
    if not t:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Watermark text is empty",
        )
    if len(t) > WATERMARK_MAX_TEXT_CHARS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Watermark text exceeds {WATERMARK_MAX_TEXT_CHARS} characters",
        )
    return t


def validate_watermark_position(position: str) -> str:
    p = (position or "").strip().lower()
    if p not in ALLOWED_WATERMARK_POSITIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid watermark position. Allowed: {', '.join(sorted(ALLOWED_WATERMARK_POSITIONS))}",
        )
    return p


def validate_watermark_font_size(value: str | int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid font size",
        )
    if n < WATERMARK_FONT_SIZE_MIN or n > WATERMARK_FONT_SIZE_MAX:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid font size. Must be {WATERMARK_FONT_SIZE_MIN}-{WATERMARK_FONT_SIZE_MAX}",
        )
    return n


def validate_watermark_font_color(color: str) -> str:
    c = (color or "#FFFFFF").strip()
    if not _WATERMARK_COLOR_RE.match(c):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid font color format. Expected #RRGGBB",
        )
    return c


def validate_watermark_opacity(value: str | float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid opacity",
        )
    if n < WATERMARK_OPACITY_MIN or n > WATERMARK_OPACITY_MAX:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid opacity. Must be {WATERMARK_OPACITY_MIN}-{WATERMARK_OPACITY_MAX}",
        )
    return n


def validate_watermark_margin(value: str | int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid margin",
        )
    if n < WATERMARK_MARGIN_MIN or n > WATERMARK_MARGIN_MAX:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid margin. Must be {WATERMARK_MARGIN_MIN}-{WATERMARK_MARGIN_MAX}",
        )
    return n


# ---------------------------------------------------------------------------
# 目录 / 清理
# ---------------------------------------------------------------------------

def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def cleanup_expired_outputs() -> int:
    """删除超过 TTL 的输出文件。返回删除数量。"""
    ensure_dir(OUTPUT_DIR)
    now = time.time()
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


def cleanup_paths(paths: list[Path]) -> None:
    """尽力删除一组路径（文件或目录）。"""
    for p in paths:
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 上传（分块写入，避免整文件读入内存；过程中检查大小）
# ---------------------------------------------------------------------------

async def save_upload(upload: UploadFile, target_path: Path) -> int:
    total = 0
    with target_path.open("wb") as f:
        while True:
            chunk = await upload.read(1024 * 1024)  # 1 MB 分块
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="File exceeds 100 MB limit",
                )
            f.write(chunk)
    return total


# ---------------------------------------------------------------------------
# ffprobe 检查
# ---------------------------------------------------------------------------

def _run_ffprobe(input_path: Path) -> dict[str, Any]:
    """执行 ffprobe 并返回 JSON 结果。"""
    command = [
        FFPROBE_BIN,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(input_path),
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ffprobe is not installed or FFPROBE_BIN is invalid",
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Media probe timed out",
        )

    if result.returncode != 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot probe media file — file may be corrupted or not a valid media",
        )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot parse media probe result",
        )


def probe_and_validate(input_path: Path) -> dict[str, Any]:
    """
    使用 ffprobe 检查并返回媒体信息。
    校验：有效媒体、包含视频流、包含音频流、时长不超过上限。
    """
    info = _run_ffprobe(input_path)

    streams: list[dict[str, Any]] = info.get("streams", [])
    if not streams:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No streams found in media file",
        )

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    if not video_streams:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="File does not contain a video stream",
        )

    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio_streams:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Video does not contain an audio track",
        )

    duration_str = info.get("format", {}).get("duration")
    if duration_str is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot determine media duration",
        )
    try:
        duration_sec = float(duration_str)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot determine media duration",
        )

    if duration_sec > MAX_VIDEO_DURATION_SECONDS:
        minutes = MAX_VIDEO_DURATION_SECONDS // 60
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Video duration exceeds {minutes} minutes limit",
        )

    first_audio = audio_streams[0]
    return {
        "duration": duration_sec,
        "audio_codec": first_audio.get("codec_name", "unknown"),
        "audio_channels": first_audio.get("channels"),
        "audio_sample_rate": first_audio.get("sample_rate"),
        "video_codec": video_streams[0].get("codec_name", "unknown"),
    }


def probe_video_basic(input_path: Path) -> dict[str, Any]:
    """
    使用 ffprobe 进行基础视频校验（水印等场景）：
    有效媒体、包含视频流、时长不超过上限。
    不要求音频流——无音轨视频也能正常处理。
    """
    info = _run_ffprobe(input_path)

    streams: list[dict[str, Any]] = info.get("streams", [])
    if not streams:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No streams found in media file",
        )

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    if not video_streams:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="File does not contain a video stream",
        )

    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    duration_str = info.get("format", {}).get("duration")
    if duration_str is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot determine media duration",
        )
    try:
        duration_sec = float(duration_str)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot determine media duration",
        )

    if duration_sec > MAX_VIDEO_DURATION_SECONDS:
        minutes = MAX_VIDEO_DURATION_SECONDS // 60
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Video duration exceeds {minutes} minutes limit",
        )

    return {
        "duration": duration_sec,
        "has_audio": len(audio_streams) > 0,
        "video_codec": video_streams[0].get("codec_name", "unknown"),
        "width": video_streams[0].get("width"),
        "height": video_streams[0].get("height"),
    }


# ---------------------------------------------------------------------------
# FFmpeg 转码
# ---------------------------------------------------------------------------

def _build_ffmpeg_command(
    input_path: Path,
    output_path: Path,
    output_format: str,
) -> list[str]:
    """构建 FFmpeg 参数数组。"""
    common: list[str] = [
        FFMPEG_BIN,
        "-y",
        "-i", str(input_path),
        "-vn",
        "-map", "0:a:0",
    ]

    if output_format == "mp3":
        codec_args = ["-c:a", "libmp3lame", "-b:a", "192k"]
    else:  # m4a
        codec_args = ["-c:a", "aac", "-b:a", "192k"]

    return common + codec_args + [str(output_path)]


def run_ffmpeg(input_path: Path, output_path: Path, output_format: str) -> Path:
    command = _build_ffmpeg_command(input_path, output_path, output_format)
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=CONVERT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ffmpeg is not installed or FFMPEG_BIN is invalid",
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Audio extraction timed out",
        )

    if result.returncode != 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Audio extraction failed",
        )

    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Audio extraction produced no output",
        )

    return output_path


# ---------------------------------------------------------------------------
# 水印位置 → drawtext x/y 表达式
# ---------------------------------------------------------------------------

_WATERMARK_POSITION_EXPR: dict[str, tuple[str, str]] = {
    "top_left":     ("{margin}", "{margin}"),
    "top_right":    ("w-text_w-{margin}", "{margin}"),
    "bottom_left":  ("{margin}", "h-text_h-{margin}"),
    "bottom_right": ("w-text_w-{margin}", "h-text_h-{margin}"),
    "center":       ("(w-text_w)/2", "(h-text_h)/2"),
}


def _build_drawtext_filter(
    textfile_path: Path,
    font_size: int,
    font_color: str,
    opacity: float,
    position: str,
    margin: int,
) -> str:
    """构建 drawtext 滤镜字符串。"""
    color_hex = font_color.lstrip("#")
    x_expr, y_expr = _WATERMARK_POSITION_EXPR[position]
    m = str(margin)

    # 转义字体路径中的冒号（Windows 盘符 C: 等会被 FFmpeg 误解析为过滤器参数分隔符）
    fontfile_escaped = WATERMARK_FONT_FILE.replace("\\", "/").replace(":", "\\:")

    return (
        f"drawtext="
        f"textfile={textfile_path.as_posix()}:"
        f"fontfile={fontfile_escaped}:"
        f"fontsize={font_size}:"
        f"fontcolor=0x{color_hex}@{opacity}:"
        f"x={x_expr.replace('{margin}', m)}:"
        f"y={y_expr.replace('{margin}', m)}"
    )


def _build_watermark_command(
    input_path: Path,
    output_path: Path,
    textfile_path: Path,
    font_size: int,
    font_color: str,
    opacity: float,
    position: str,
    margin: int,
    has_audio: bool,
) -> list[str]:
    """构建水印 FFmpeg 参数数组。"""
    vf = _build_drawtext_filter(
        textfile_path, font_size, font_color, opacity, position, margin
    )
    command: list[str] = [
        FFMPEG_BIN,
        "-y",
        "-i", str(input_path),
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]
    if has_audio:
        command += ["-c:a", "aac"]
    else:
        command += ["-an"]
    command.append(str(output_path))
    return command


def run_watermark_ffmpeg(
    input_path: Path,
    output_path: Path,
    textfile_path: Path,
    font_size: int,
    font_color: str,
    opacity: float,
    position: str,
    margin: int,
    has_audio: bool,
) -> Path:
    if not Path(WATERMARK_FONT_FILE).exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Watermark font file not found on server",
        )

    command = _build_watermark_command(
        input_path, output_path, textfile_path,
        font_size, font_color, opacity, position, margin, has_audio,
    )
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=WATERMARK_CONVERT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ffmpeg is not installed or FFMPEG_BIN is invalid",
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Watermark processing timed out",
        )

    if result.returncode != 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Watermark failed",
        )

    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Watermark produced no output",
        )

    return output_path


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event() -> None:
    ensure_dir(UPLOAD_DIR)
    ensure_dir(OUTPUT_DIR)
    cleanup_expired_outputs()


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@app.get("/health", include_in_schema=False)
def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "media-converter"}


@app.post("/media/video/extract-audio", dependencies=[Depends(require_token)])
async def extract_audio(
    file: UploadFile = File(...),
    output_format: str = Form(default="mp3"),
):
    # ---- 1. 基础校验 ----
    display_name = safe_display_name(file.filename)
    validate_input_extension(display_name)
    fmt = validate_output_format(output_format)

    # ---- 2. 保存上传文件（分块写入） ----
    ensure_dir(UPLOAD_DIR)
    server_filename = uuid.uuid4().hex
    input_suffix = Path(display_name).suffix.lower()
    input_path = UPLOAD_DIR / (server_filename + input_suffix)

    try:
        size = await save_upload(file, input_path)
        if size <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded file is empty",
            )
    except HTTPException:
        # 上传失败时清理已写入的部分
        cleanup_paths([input_path])
        raise

    # ---- 3. ffprobe 深度校验 ----
    try:
        media_info = probe_and_validate(input_path)
    except HTTPException:
        cleanup_paths([input_path])
        raise

    # ---- 4. FFmpeg 转码 ----
    ensure_dir(OUTPUT_DIR)
    output_filename = server_filename + "." + fmt
    output_path = OUTPUT_DIR / output_filename

    try:
        run_ffmpeg(input_path, output_path, fmt)
    except HTTPException:
        # 失败时清理输入视频 和 可能残留的输出文件
        cleanup_paths([input_path, output_path])
        raise

    # ---- 5. 清理输入视频 ----
    cleanup_paths([input_path])

    # ---- 6. 清理过期输出 ----
    cleanup_expired_outputs()

    # ---- 7. 构造响应 ----
    download_url = ""
    if PUBLIC_BASE_URL:
        download_url = f"{PUBLIC_BASE_URL}/files/{output_filename}"

    output_display = Path(display_name).with_suffix("." + fmt).name

    return api_success({
        "filename": output_display,
        "download_url": download_url,
        "expires_in": OUTPUT_TTL_SECONDS,
    })


@app.post("/media/video/add-text-watermark", dependencies=[Depends(require_token)])
async def add_text_watermark(
    file: UploadFile = File(...),
    text: str = Form(...),
    position: str = Form(default="top_left"),
    font_size: int = Form(default=36),
    font_color: str = Form(default="#FFFFFF"),
    opacity: float = Form(default=0.75),
    margin: int = Form(default=24),
):
    # ---- 1. 参数校验 ----
    display_name = safe_display_name(file.filename)
    validate_input_extension(display_name)
    wm_text = validate_watermark_text(text)
    wm_position = validate_watermark_position(position)
    wm_font_size = validate_watermark_font_size(font_size)
    wm_font_color = validate_watermark_font_color(font_color)
    wm_opacity = validate_watermark_opacity(opacity)
    wm_margin = validate_watermark_margin(margin)

    # ---- 2. 保存上传视频（分块写入） ----
    ensure_dir(UPLOAD_DIR)
    server_filename = uuid.uuid4().hex
    input_suffix = Path(display_name).suffix.lower()
    input_path = UPLOAD_DIR / (server_filename + input_suffix)

    # 水印文字临时文件（UTF-8，避免 FFmpeg 转义问题）
    textfile_path = UPLOAD_DIR / (server_filename + ".txt")

    try:
        size = await save_upload(file, input_path)
        if size <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded file is empty",
            )
    except HTTPException:
        cleanup_paths([input_path])
        raise

    # ---- 3. 写入水印文字到临时文件 ----
    try:
        textfile_path.write_text(wm_text, encoding="utf-8")
    except Exception:
        cleanup_paths([input_path])
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Watermark failed",
        )

    # ---- 4. ffprobe 基础校验 ----
    try:
        media_info = probe_video_basic(input_path)
    except HTTPException:
        cleanup_paths([input_path, textfile_path])
        raise

    # ---- 5. FFmpeg 水印处理 ----
    ensure_dir(OUTPUT_DIR)
    output_filename = server_filename + ".mp4"
    output_path = OUTPUT_DIR / output_filename

    try:
        run_watermark_ffmpeg(
            input_path, output_path, textfile_path,
            wm_font_size, wm_font_color, wm_opacity,
            wm_position, wm_margin,
            media_info["has_audio"],
        )
    except HTTPException:
        cleanup_paths([input_path, textfile_path, output_path])
        raise

    # ---- 6. 清理输入文件和临时文本文件 ----
    cleanup_paths([input_path, textfile_path])

    # ---- 7. 清理过期输出 ----
    cleanup_expired_outputs()

    # ---- 8. 构造响应 ----
    download_url = ""
    if PUBLIC_BASE_URL:
        download_url = f"{PUBLIC_BASE_URL}/files/{output_filename}"

    output_display = Path(display_name).stem + "_watermark.mp4"

    return api_success({
        "filename": output_display,
        "download_url": download_url,
        "expires_in": OUTPUT_TTL_SECONDS,
    })


@app.get("/files/{filename}", dependencies=[Depends(require_token)])
async def download_file(filename: str):
    # ---- 1. 安全校验 ----
    safe_name = validate_safe_filename(filename)

    # ---- 2. 查找文件 ----
    file_path = OUTPUT_DIR / safe_name
    resolved = file_path.resolve()
    allowed = OUTPUT_DIR.resolve()
    # 防止符号链接绕过
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
    if suffix == ".mp3":
        media_type = "audio/mpeg"
    elif suffix == ".mp4":
        media_type = "video/mp4"
    else:
        media_type = "audio/mp4"

    return FileResponse(
        file_path,
        media_type=media_type,
        filename=safe_name,
        headers={"Cache-Control": "no-store"},
    )
