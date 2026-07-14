import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

# 内容安全审核
from parse_video_py.content_security import (
    WxSecurityError,
    WxSecurityServiceError,
    check_text,
    check_image,
    check_audio,
    extract_video_keyframes,
    get_task_store,
    verify_openid_token,
    handle_callback_url_verification,
    handle_callback_event,
)
from parse_video_py.content_security import WX_CONTENT_SECURITY_ENABLED as _SEC_ENABLED
from parse_video_py.content_security import WX_CONTENT_SECURITY_STRICT as _SEC_STRICT

# 微信小程序登录
from parse_video_py.auth_web import router as auth_router
from parse_video_py.document_summary_web import router as document_summary_router
from parse_video_py.document_translation_web import router as document_translation_router


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

# ---- GIF 转换配置 ----
ALLOWED_GIF_WIDTHS: set[int] = {360, 480}
ALLOWED_GIF_FPS: set[int] = {5, 10, 15}
MAX_GIF_DURATION_SECONDS: int = int(
    os.getenv("MEDIA_CONVERTER_MAX_GIF_DURATION_SECONDS", "10")
)
GIF_CONVERT_TIMEOUT_SECONDS: int = int(
    os.getenv("MEDIA_CONVERTER_GIF_TIMEOUT_SECONDS", "120")
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

# 挂载微信小程序登录路由（公网地址: /parse/media-converter/auth/wechat-login）
app.include_router(auth_router)
app.include_router(document_summary_router)
app.include_router(document_translation_router)


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
    ("Only .mp3 and .m4a", "INVALID_FORMAT", "Only .mp3, .m4a, .mp4 and .gif files are allowed"),
    ("Invalid start time", "INVALID_START_TIME", "Start time must be >= 0"),
    ("Invalid GIF duration", "INVALID_GIF_DURATION", "GIF duration must be 1-10 seconds"),
    ("Invalid GIF width", "INVALID_GIF_WIDTH", "Width must be 360 or 480"),
    ("Invalid GIF fps", "INVALID_GIF_FPS", "FPS must be 5, 10, or 15"),
    ("Clip range exceeds video duration", "DURATION_EXCEEDED", "Clip range exceeds video duration"),
    ("GIF conversion failed", "PROCESS_FAILED", "GIF conversion failed"),
    ("GIF conversion produced no output", "PROCESS_FAILED", "GIF conversion produced no output"),
    ("GIF conversion timed out", "PROCESS_TIMEOUT", "GIF conversion timed out"),
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
    if suffix not in {".mp3", ".m4a", ".mp4", ".gif", ".jpg", ".jpeg", ".png"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only media converter output files are allowed",
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


def probe_video_for_gif(
    input_path: Path, start_time: float, clip_duration: float
) -> dict[str, Any]:
    """
    使用 ffprobe 校验视频用于 GIF 转换：
    有效媒体、包含视频流、时长不超过上限、
    截取范围不超出视频总时长。
    不要求音频流。
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

    if start_time + clip_duration > duration_sec:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Clip range exceeds video duration",
        )

    return {
        "duration": duration_sec,
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
# FFmpeg GIF 转换
# ---------------------------------------------------------------------------

def _build_gif_command(
    input_path: Path,
    output_path: Path,
    start_time: float,
    duration: float,
    width: int,
    fps: int,
) -> list[str]:
    """构建视频转 GIF 的 FFmpeg 参数数组。

    使用 palettegen/paletteuse 两阶段滤镜生成高质量 GIF。
    """
    vf = (
        f"fps={fps},"
        f"scale={width}:-1:flags=lanczos,"
        f"split[s0][s1];"
        f"[s0]palettegen=max_colors=256:stats_mode=diff[p];"
        f"[s1][p]paletteuse=dither=bayer:bayer_scale=5"
    )
    return [
        FFMPEG_BIN,
        "-y",
        "-ss", str(start_time),
        "-t", str(duration),
        "-i", str(input_path),
        "-vf", vf,
        str(output_path),
    ]


def run_gif_ffmpeg(
    input_path: Path,
    output_path: Path,
    start_time: float,
    duration: float,
    width: int,
    fps: int,
) -> Path:
    """执行视频转 GIF。"""
    command = _build_gif_command(
        input_path, output_path, start_time, duration, width, fps
    )
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=GIF_CONVERT_TIMEOUT_SECONDS,
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
            detail="GIF conversion timed out",
        )

    if result.returncode != 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GIF conversion failed",
        )

    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GIF conversion produced no output",
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
    # 恢复并清理过期审核任务
    try:
        task_store = get_task_store()
        cleaned = task_store.cleanup_expired()
        if cleaned > 0:
            log = __import__("logging").getLogger(__name__)
            log.info("启动时清理 %s 个过期审核任务", cleaned)
    except Exception:
        pass


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
    openid_token: str = Form(default=""),
):
    # ---- 1. 基础校验 ----
    display_name = safe_display_name(file.filename)
    validate_input_extension(display_name)
    fmt = validate_output_format(output_format)

    # ---- 2. 内容安全开启时验证 openid_token ----
    verified_openid = ""
    if _SEC_ENABLED:
        if not openid_token or not openid_token.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="内容安全已开启，openid_token 不能为空",
            )

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

        if not PUBLIC_BASE_URL:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="内容安全已开启但 PUBLIC_BASE_URL 未配置，无法提交异步审核",
            )

    # ---- 3. 保存上传视频 ----
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
        cleanup_paths([input_path])
        raise

    # ---- 4. ffprobe 深度校验 ----
    try:
        probe_and_validate(input_path)
    except HTTPException:
        cleanup_paths([input_path])
        raise

    # ---- 5. 生成用户最终音频 ----
    ensure_dir(OUTPUT_DIR)
    output_filename = server_filename + "." + fmt
    output_path = OUTPUT_DIR / output_filename

    # MP3 直接审核最终文件；M4A 额外生成临时 MP3 用于审核。
    review_filename = output_filename
    review_path = output_path

    try:
        run_ffmpeg(input_path, output_path, fmt)

        if _SEC_ENABLED and fmt == "m4a":
            review_filename = server_filename + "_review.mp3"
            review_path = OUTPUT_DIR / review_filename
            run_ffmpeg(input_path, review_path, "mp3")
    except HTTPException:
        cleanup_paths([input_path, output_path, review_path])
        raise

    # ---- 6. 清理原始上传视频 ----
    cleanup_paths([input_path])

    output_display = Path(display_name).with_suffix("." + fmt).name

    # ---- 7. 提交微信音频内容安全审核 ----
    job_id = server_filename
    trace_id = ""
    is_pending = False

    if _SEC_ENABLED and verified_openid:
        review_url = f"{PUBLIC_BASE_URL}/files/{review_filename}"

        try:
            result = check_audio(
                media_url=review_url,
                openid=verified_openid,
            )
            trace_id = result.trace_id

            if not trace_id:
                raise WxSecurityServiceError(
                    "微信音频内容安全接口未返回 trace_id"
                )

            task_store = get_task_store()
            task_store.create_task(
                job_id=job_id,
                trace_id=trace_id,
                file_path=str(output_path),
                openid=verified_openid,
                media_url=review_url,
                media_type="audio",
            )
            is_pending = True

        except (WxSecurityError, ValueError) as exc:
            log = __import__("logging").getLogger(__name__)
            log.exception(
                "[audio-audit] 内容安全提交失败 "
                "job_id=%s errcode=%s code=%s message=%s",
                job_id,
                getattr(exc, "errcode", None),
                getattr(exc, "code", None),
                getattr(exc, "message", str(exc)),
            )

            # 临时审核 MP3 已无用途。
            if review_path != output_path:
                cleanup_paths([review_path])

            if _SEC_STRICT:
                cleanup_paths([output_path])
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="内容安全服务异常，请稍后重试",
                )

            # 非严格模式：保持 GIF 当前行为，审核提交失败时直接放行。
            is_pending = False

    # ---- 8. 清理过期输出 ----
    cleanup_expired_outputs()

    # ---- 9. 审核提交成功：返回 pending ----
    if is_pending:
        return api_success({
            "jobId": job_id,
            "status": "pending",
            "filename": output_display,
            "traceId": trace_id,
            "message": "音频已生成，内容安全审核中，请稍后查询",
        })

    # ---- 10. 未开启审核或非严格模式放行 ----
    download_url = ""
    if PUBLIC_BASE_URL:
        download_url = f"{PUBLIC_BASE_URL}/files/{output_filename}"

    return api_success({
        "filename": output_display,
        "download_url": download_url,
        "expires_in": OUTPUT_TTL_SECONDS,
    })


@app.get(
    "/media/video/extract-audio/status/{job_id}",
    dependencies=[Depends(require_token)],
)
async def extract_audio_status(job_id: str):
    """查询音频提取任务的内容安全审核状态。"""

    task_store = get_task_store()
    task = task_store.get_task(job_id)

    if task is None or task.media_type != "audio":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在或已过期",
        )

    final_path = Path(task.file_path)
    file_name = final_path.name

    if task.status == "approved":
        if not final_path.exists() or not final_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found or expired",
            )

        download_url = ""
        if PUBLIC_BASE_URL:
            download_url = f"{PUBLIC_BASE_URL}/files/{file_name}"

        return api_success({
            "jobId": job_id,
            "status": "approved",
            "filename": file_name,
            "downloadUrl": download_url,
            "expiresIn": OUTPUT_TTL_SECONDS,
            "traceId": task.trace_id,
        })

    if task.status == "rejected":
        return api_success({
            "jobId": job_id,
            "status": "rejected",
            "filename": file_name,
            "downloadUrl": "",
            "message": "内容安全审核不通过，输出文件已删除",
            "traceId": task.trace_id,
            "result": task.result,
        })

    if task.status in {"error", "expired"}:
        message = "审核失败或任务已过期"
        if task.result:
            message = task.result.get("message", message)

        return api_success({
            "jobId": job_id,
            "status": "error",
            "filename": file_name,
            "downloadUrl": "",
            "message": message,
            "traceId": task.trace_id,
        })

    return api_success({
        "jobId": job_id,
        "status": "pending",
        "filename": file_name,
        "downloadUrl": "",
        "message": "内容安全审核中，请稍后查询",
        "traceId": task.trace_id,
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
    openid_token: str = Form(default=""),
):
    """给视频添加文字水印，并对最终视频的代表性关键帧进行内容安全审核。"""

    # ---- 1. 参数校验 ----
    display_name = safe_display_name(file.filename)
    validate_input_extension(display_name)
    wm_text = validate_watermark_text(text)
    wm_position = validate_watermark_position(position)
    wm_font_size = validate_watermark_font_size(font_size)
    wm_font_color = validate_watermark_font_color(font_color)
    wm_opacity = validate_watermark_opacity(opacity)
    wm_margin = validate_watermark_margin(margin)

    # ---- 2. 内容安全开启时验证 openid_token ----
    verified_openid = ""

    if _SEC_ENABLED:
        if not openid_token or not openid_token.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="内容安全已开启，openid_token 不能为空",
            )

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

        if not PUBLIC_BASE_URL:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="内容安全已开启但 PUBLIC_BASE_URL 未配置",
            )

        # 水印文字使用同步文本内容安全审核。
        # 在保存和处理视频前完成，违规文字可直接拒绝，避免浪费磁盘和转码资源。
        try:
            text_check_result = check_text(
                content=wm_text,
                openid=verified_openid,
                scene=2,
                title="视频水印文字",
            )

            # 非严格模式下 check_text 不会主动抛出违规异常，
            # 因此仍需显式判断结果，确保 risky/review 不会继续生成视频。
            if not text_check_result.is_pass:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="水印文字内容安全审核不通过，请修改后重试",
                )

        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"水印文字审核参数无效: {exc}",
            )
        except WxSecurityError as exc:
            # 严格模式下，内容违规会由 check_text 直接抛出 CONTENT_REJECTED。
            if getattr(exc, "code", "") == "CONTENT_REJECTED":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="水印文字内容安全审核不通过，请修改后重试",
                )

            log = __import__("logging").getLogger(__name__)
            log.exception(
                "[watermark-text-audit] 文本审核服务异常 "
                "errcode=%s code=%s message=%s",
                getattr(exc, "errcode", None),
                getattr(exc, "code", None),
                getattr(exc, "message", str(exc)),
            )

            if _SEC_STRICT:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="水印文字内容安全服务异常，请稍后重试",
                )

            # 非严格模式下仅在审核服务不可用时兼容放行；
            # 明确返回 risky/review 的文字仍会在上方被拒绝。

    # ---- 3. 保存上传视频 ----
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

    # ---- 4. 写入水印文字临时文件 ----
    try:
        textfile_path.write_text(wm_text, encoding="utf-8")
    except Exception:
        cleanup_paths([input_path, textfile_path])
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Watermark failed",
        )

    # ---- 5. ffprobe 基础校验 ----
    try:
        media_info = probe_video_basic(input_path)
    except HTTPException:
        cleanup_paths([input_path, textfile_path])
        raise

    # ---- 6. 生成带水印 MP4 ----
    ensure_dir(OUTPUT_DIR)
    output_filename = server_filename + ".mp4"
    output_path = OUTPUT_DIR / output_filename

    try:
        run_watermark_ffmpeg(
            input_path,
            output_path,
            textfile_path,
            wm_font_size,
            wm_font_color,
            wm_opacity,
            wm_position,
            wm_margin,
            media_info["has_audio"],
        )
    except HTTPException:
        cleanup_paths([input_path, textfile_path, output_path])
        raise

    # 原始视频和文字文件不再需要
    cleanup_paths([input_path, textfile_path])

    output_display = Path(display_name).stem + "_watermark.mp4"
    job_id = server_filename
    trace_id = ""
    is_pending = False

    # 临时审核图片：从最终带水印视频抽取最多 3 个关键帧，
    # 选择中间一张作为代表帧提交微信图片审核。
    review_filename = server_filename + "_review.jpg"
    review_path = OUTPUT_DIR / review_filename
    review_frames_dir = UPLOAD_DIR / (server_filename + "_review_frames")

    # ---- 7. 提交代表帧图片审核 ----
    if _SEC_ENABLED and verified_openid:
        try:
            frames = extract_video_keyframes(
                input_path=output_path,
                output_dir=review_frames_dir,
                ffmpeg_bin=FFMPEG_BIN,
                max_frames=3,
                timeout=60,
            )

            if not frames:
                raise WxSecurityServiceError(
                    "未能从水印视频中抽取审核关键帧"
                )

            selected_frame = frames[len(frames) // 2]
            shutil.copyfile(selected_frame, review_path)
            cleanup_paths([review_frames_dir])

            if not review_path.exists() or review_path.stat().st_size <= 0:
                raise WxSecurityServiceError(
                    "水印视频审核图片生成失败"
                )

            review_url = f"{PUBLIC_BASE_URL}/files/{review_filename}"

            result = check_image(
                media_url=review_url,
                openid=verified_openid,
            )

            trace_id = result.trace_id
            if not trace_id:
                raise WxSecurityServiceError(
                    "微信图片内容安全接口未返回 trace_id"
                )

            task_store = get_task_store()
            task_store.create_task(
                job_id=job_id,
                trace_id=trace_id,
                # 审核拒绝时，现有回调会删除最终 MP4。
                file_path=str(output_path),
                openid=verified_openid,
                media_url=review_url,
                media_type="watermark_video",
            )

            is_pending = True

        except Exception as exc:
            cleanup_paths([review_frames_dir, review_path])

            log = __import__("logging").getLogger(__name__)
            log.exception(
                "[watermark-audit] 内容安全提交失败 "
                "job_id=%s errcode=%s code=%s message=%s",
                job_id,
                getattr(exc, "errcode", None),
                getattr(exc, "code", None),
                getattr(exc, "message", str(exc)),
            )

            if _SEC_STRICT:
                cleanup_paths([output_path])
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="内容安全服务异常，请稍后重试",
                )

            # 非严格模式沿用现有兼容行为：审核提交失败时直接返回结果。
            is_pending = False

        finally:
            cleanup_paths([review_frames_dir])

    # ---- 8. 清理过期输出 ----
    cleanup_expired_outputs()

    # 审核提交成功：返回 pending，不返回 MP4 下载地址。
    if is_pending:
        return api_success({
            "jobId": job_id,
            "status": "pending",
            "filename": output_display,
            "traceId": trace_id,
            "message": "水印视频已生成，内容安全审核中，请稍后查询",
        })

    # 内容安全关闭，或非严格模式下审核提交失败：保持原行为。
    download_url = ""
    if PUBLIC_BASE_URL:
        download_url = f"{PUBLIC_BASE_URL}/files/{output_filename}"

    return api_success({
        "filename": output_display,
        "download_url": download_url,
        "expires_in": OUTPUT_TTL_SECONDS,
    })


@app.get(
    "/media/video/add-text-watermark/status/{job_id}",
    dependencies=[Depends(require_token)],
)
async def add_text_watermark_status(job_id: str):
    """查询视频加水印任务的代表帧内容安全审核状态。"""

    task_store = get_task_store()
    task = task_store.get_task(job_id)

    if task is None or task.media_type != "watermark_video":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在或已过期",
        )

    final_path = Path(task.file_path)
    file_name = final_path.name
    review_path = OUTPUT_DIR / f"{job_id}_review.jpg"

    if task.status == "approved":
        cleanup_paths([review_path])

        if not final_path.exists() or not final_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found or expired",
            )

        download_url = ""
        if PUBLIC_BASE_URL:
            download_url = f"{PUBLIC_BASE_URL}/files/{file_name}"

        return api_success({
            "jobId": job_id,
            "status": "approved",
            "filename": file_name,
            "downloadUrl": download_url,
            "expiresIn": OUTPUT_TTL_SECONDS,
            "traceId": task.trace_id,
        })

    if task.status == "rejected":
        cleanup_paths([review_path])

        return api_success({
            "jobId": job_id,
            "status": "rejected",
            "filename": file_name,
            "downloadUrl": "",
            "message": "内容安全审核不通过，水印视频已删除",
            "traceId": task.trace_id,
            "result": task.result,
        })

    if task.status in {"error", "expired"}:
        cleanup_paths([review_path])

        message = "审核失败或任务已过期"
        if task.result:
            message = task.result.get("message", message)

        return api_success({
            "jobId": job_id,
            "status": "error",
            "filename": file_name,
            "downloadUrl": "",
            "message": message,
            "traceId": task.trace_id,
        })

    return api_success({
        "jobId": job_id,
        "status": "pending",
        "filename": file_name,
        "downloadUrl": "",
        "message": "内容安全审核中，请稍后查询",
        "traceId": task.trace_id,
    })


@app.post("/media/video/to-gif", dependencies=[Depends(require_token)])
async def video_to_gif(
    file: UploadFile = File(...),
    start_time: float = Form(...),
    duration: float = Form(...),
    width: int = Form(...),
    fps: int = Form(...),
    openid_token: str = Form(default=""),
):
    # ---- 1. 参数校验 ----
    display_name = safe_display_name(file.filename)
    validate_input_extension(display_name)

    if start_time < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid start time",
        )
    if duration <= 0 or duration > MAX_GIF_DURATION_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid GIF duration",
        )
    if width not in ALLOWED_GIF_WIDTHS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid GIF width",
        )
    if fps not in ALLOWED_GIF_FPS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid GIF fps",
        )

    # ---- 2. 验证 openid ----
    verified_openid = ""
    if _SEC_ENABLED:
        if not openid_token or not openid_token.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="内容安全已开启，openid_token 不能为空",
            )
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

        # 提交异步审核需要公网 URL
        if not PUBLIC_BASE_URL:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="内容安全已开启但 PUBLIC_BASE_URL 未配置，无法提交异步审核",
            )

    # ---- 3. 保存上传文件（分块写入） ----
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
        cleanup_paths([input_path])
        raise

    # ---- 4. ffprobe 校验视频和截取范围 ----
    try:
        media_info = probe_video_for_gif(input_path, start_time, duration)
    except HTTPException:
        cleanup_paths([input_path])
        raise

    # ---- 5. FFmpeg 生成 GIF ----
    ensure_dir(OUTPUT_DIR)
    output_filename = server_filename + ".gif"
    output_path = OUTPUT_DIR / output_filename

    try:
        run_gif_ffmpeg(input_path, output_path, start_time, duration, width, fps)
    except HTTPException:
        cleanup_paths([input_path, output_path])
        raise

    # ---- 6. 清理输入视频 ----
    cleanup_paths([input_path])

    # ---- 7. 内容安全：提交异步图片审核（不立即公开 downloadUrl） ----
    job_id = server_filename  # 复用 UUID 作为 job_id
    trace_id = ""
    is_pending = False

    if _SEC_ENABLED and verified_openid:
        gif_url = f"{PUBLIC_BASE_URL}/files/{output_filename}"
        try:
            result = check_image(gif_url, openid=verified_openid)
            trace_id = result.trace_id
            is_pending = True

            # 创建持久化审核任务
            task_store = get_task_store()
            task_store.create_task(
                job_id=job_id,
                trace_id=trace_id,
                file_path=str(output_path),
                openid=verified_openid,
                media_url=gif_url,
                media_type="image",
            )
        except WxSecurityServiceError:
            if _SEC_STRICT:
                # 严格模式：审核提交失败 → 删除输出文件并拒绝
                cleanup_paths([output_path])
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="内容安全服务异常，请稍后重试",
                )
            # 非严格模式：审核提交失败不阻断，记录日志
            is_pending = False

    # ---- 8. 清理过期输出 ----
    cleanup_expired_outputs()

    # ---- 9. 构造响应 ----
    output_display = Path(display_name).stem + ".gif"

    if is_pending:
        # 异步审核中：不返回 downloadUrl
        return api_success({
            "jobId": job_id,
            "status": "pending",
            "filename": output_display,
            "traceId": trace_id,
            "message": "GIF 已生成，内容安全审核中，请通过状态接口查询结果",
        })
    else:
        # 未开启安全审核：直接返回 downloadUrl（保持向后兼容）
        download_url = ""
        if PUBLIC_BASE_URL:
            download_url = f"{PUBLIC_BASE_URL}/files/{output_filename}"
        return api_success({
            "filename": output_display,
            "download_url": download_url,
            "expires_in": OUTPUT_TTL_SECONDS,
        })


@app.get(
    "/media/video/to-gif/status/{job_id}",
    dependencies=[Depends(require_token)],
)
async def gif_status(job_id: str):
    """查询 GIF 转换任务的安全审核状态。

    审核通过后才返回 downloadUrl。
    """
    task_store = get_task_store()
    task = task_store.get_task(job_id)

    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在或已过期",
        )

    # 提取文件名
    file_name = Path(task.file_path).name

    if task.status == "approved":
        download_url = ""
        if PUBLIC_BASE_URL:
            download_url = f"{PUBLIC_BASE_URL}/files/{file_name}"
        return api_success({
            "jobId": job_id,
            "status": "approved",
            "filename": file_name,
            "downloadUrl": download_url,
            "expiresIn": OUTPUT_TTL_SECONDS,
            "traceId": task.trace_id,
        })
    elif task.status == "rejected":
        return api_success({
            "jobId": job_id,
            "status": "rejected",
            "filename": file_name,
            "downloadUrl": "",
            "message": "内容安全审核不通过，输出文件已删除",
            "traceId": task.trace_id,
            "result": task.result,
        })
    elif task.status == "error":
        return api_success({
            "jobId": job_id,
            "status": "error",
            "filename": file_name,
            "downloadUrl": "",
            "message": (
                task.result.get("message", "审核超时")
                if task.result
                else "审核超时"
            ),
            "traceId": task.trace_id,
        })
    else:
        # pending
        return api_success({
            "jobId": job_id,
            "status": "pending",
            "filename": file_name,
            "downloadUrl": "",
            "message": "内容安全审核中，请稍后查询",
            "traceId": task.trace_id,
        })


# ---------------------------------------------------------------------------
# 微信内容安全回调端点
# ---------------------------------------------------------------------------

@app.get("/content-security/callback", include_in_schema=False)
async def wx_callback_verify(
    signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    echostr: str = "",
):
    """微信公众平台 URL 验证（GET）。

    微信在配置消息推送地址时会发送 GET 请求验证 URL 归属。
    需要验证签名并返回解密后的 echostr。
    """
    if not signature or not timestamp or not nonce or not echostr:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="缺少必要参数: signature/timestamp/nonce/echostr",
        )

    ok, plaintext = handle_callback_url_verification(
        signature=signature,
        timestamp=timestamp,
        nonce=nonce,
        echostr=echostr,
    )

    if not ok or plaintext is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="签名验证失败",
        )

    # 返回纯文本 echostr（不包装 JSON）
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content=plaintext)


@app.post("/content-security/callback", include_in_schema=False)
async def wx_callback_event(request: Request):
    """微信审核结果异步推送（POST）。

    微信在审核完成后主动推送 wxa_media_check 事件到此端点。
    """
    body = await request.body()
    if not body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请求体为空",
        )

    params = request.query_params
    msg_signature = params.get("msg_signature", "")
    timestamp = params.get("timestamp", "")
    nonce = params.get("nonce", "")

    result = handle_callback_event(
        body=body,
        msg_signature=msg_signature,
        timestamp=timestamp,
        nonce=nonce,
    )

    # 审核完成后清理各功能生成的临时审核文件。
    if result and result.trace_id:
        task_store = get_task_store()
        task = task_store.get_task_by_trace_id(result.trace_id)

        if task and task.media_type == "audio":
            final_path = Path(task.file_path)
            if final_path.suffix.lower() == ".m4a":
                review_path = OUTPUT_DIR / f"{task.job_id}_review.mp3"
                cleanup_paths([review_path])

        if task and task.media_type == "watermark_video":
            review_path = OUTPUT_DIR / f"{task.job_id}_review.jpg"
            cleanup_paths([review_path])

    # 无论解析结果如何，都返回 success，避免微信重复推送。
    return PlainTextResponse(content="success")


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
    elif suffix == ".gif":
        media_type = "image/gif"
    elif suffix in {".jpg", ".jpeg"}:
        media_type = "image/jpeg"
    elif suffix == ".png":
        media_type = "image/png"
    else:
        media_type = "audio/mp4"

    return FileResponse(
        file_path,
        media_type=media_type,
        filename=safe_name,
        headers={"Cache-Control": "no-store"},
    )
