"""
微信小程序内容安全统一模块。

基于微信官方文档 (2025/2026) 实现：
- 文本内容安全识别: POST /wxa/msg_sec_check  (v2, 同步)
- 多媒体内容安全识别: POST /wxa/media_check_async (v2, 异步)
- 稳定版 access_token: POST /cgi-bin/stable_token

参考文档:
- https://developers.weixin.qq.com/miniprogram/dev/server/API/sec-center/
- https://developers.weixin.qq.com/miniprogram/dev/OpenApiDoc/mp-access-token/

--- 重要：异步接口与人工配置 ---

media_check_async 是异步接口：
1. 调用后立即返回 trace_id，不返回审核结果。
2. 审核结果在 30 分钟内由微信服务器 **主动推送** 到开发者配置的消息接收服务器。
3. 推送事件类型为 wxa_media_check，需在服务端实现回调接口接收。

需要人工在微信公众平台完成以下配置：
- 【开发】→【开发管理】→【开发设置】→【消息推送】
- 配置服务器地址 (URL)、令牌 (Token)、消息加解密密钥 (EncodingAESKey)
- 选择消息加解密方式（明文 / 兼容 / 安全）
- 确保服务器地址可被微信服务器公网访问

本模块 **不伪造同步结果**。调用 media_check_async 后仅返回 trace_id，
严格模式下建议暂存文件、等待回调确认后再决定是否清理。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

logger = logging.getLogger("content_security")

# ---------------------------------------------------------------------------
# 配置（全部通过环境变量注入）
# ---------------------------------------------------------------------------

WX_APPID: str = os.getenv("WX_APPID", "").strip()
WX_APPSECRET: str = os.getenv("WX_APPSECRET", "").strip()

WX_CONTENT_SECURITY_ENABLED: bool = os.getenv(
    "WX_CONTENT_SECURITY_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on"}

WX_CONTENT_SECURITY_STRICT: bool = os.getenv(
    "WX_CONTENT_SECURITY_STRICT", "false"
).strip().lower() in {"1", "true", "yes", "on"}

WX_API_TIMEOUT: float = float(os.getenv("WX_API_TIMEOUT", "10"))
WX_API_MAX_RETRIES: int = int(os.getenv("WX_API_MAX_RETRIES", "3"))
WX_API_RETRY_BACKOFF: float = float(os.getenv("WX_API_RETRY_BACKOFF", "0.5"))

# 文本审核场景: 1=资料 2=评论 3=论坛 4=社交日志
WX_MSG_SEC_CHECK_SCENE: int = int(os.getenv("WX_MSG_SEC_CHECK_SCENE", "2"))
# 多媒体审核场景
WX_MEDIA_CHECK_SCENE: int = int(os.getenv("WX_MEDIA_CHECK_SCENE", "2"))

# access_token 提前过期安全缓冲（秒）
_TOKEN_EXPIRY_BUFFER: int = 120

# 微信 API 基地址
_WX_API_BASE: str = "https://api.weixin.qq.com"


# ---------------------------------------------------------------------------
# 异常类型
# ---------------------------------------------------------------------------

class WxSecurityError(Exception):
    """内容安全异常基类。"""

    def __init__(self, message: str, code: str = "SECURITY_ERROR") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class WxSecurityRejectedError(WxSecurityError):
    """审核不通过（内容违规）。"""

    def __init__(
        self,
        message: str,
        label: int = 0,
        suggest: str = "risky",
    ) -> None:
        super().__init__(message, code="CONTENT_REJECTED")
        self.label = label
        self.suggest = suggest


class WxSecurityServiceError(WxSecurityError):
    """审核服务异常（网络、超时、配置错误等）。"""

    def __init__(self, message: str, errcode: int | None = None) -> None:
        super().__init__(message, code="SECURITY_SERVICE_ERROR")
        self.errcode = errcode


class WxSecurityConfigError(WxSecurityError):
    """配置错误（缺少 AppID / AppSecret 等）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="SECURITY_CONFIG_ERROR")


# ---------------------------------------------------------------------------
# 审核结果
# ---------------------------------------------------------------------------

@dataclass
class TextCheckResult:
    """文本审核结果。"""

    suggest: str  # pass / risky / review
    label: int  # 100=正常, 10001=广告, 20001=时政, 20002=色情, ...
    trace_id: str
    detail: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_pass(self) -> bool:
        return self.suggest == "pass"

    @property
    def is_rejected(self) -> bool:
        return self.suggest == "risky"

    @property
    def needs_review(self) -> bool:
        return self.suggest == "review"


@dataclass
class MediaCheckResult:
    """多媒体审核提交结果（异步接口仅返回 trace_id）。"""

    trace_id: str
    raw: dict[str, Any] = field(default_factory=dict)


# 回调结果与提交时结构一致，但有 result 字段
@dataclass
class MediaCheckCallbackResult:
    """多媒体审核回调结果（微信服务器推送）。"""

    trace_id: str
    appid: str
    suggest: str  # pass / risky / review
    label: int
    detail: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_pass(self) -> bool:
        return self.suggest == "pass"

    @property
    def is_rejected(self) -> bool:
        return self.suggest == "risky"

    @property
    def needs_review(self) -> bool:
        return self.suggest == "review"


# ---------------------------------------------------------------------------
# 标签中文描述
# ---------------------------------------------------------------------------

_LABEL_MAP: dict[int, str] = {
    100: "正常",
    10001: "广告",
    20001: "时政",
    20002: "色情",
    20003: "辱骂",
    20006: "违法犯罪",
    20008: "欺诈",
    20012: "低俗",
    20013: "版权",
    21000: "其他",
}


def label_description(label: int) -> str:
    """获取标签的中文描述。"""
    return _LABEL_MAP.get(label, f"未知标签({label})")


# ---------------------------------------------------------------------------
# access_token 管理与并发安全缓存
# ---------------------------------------------------------------------------

class _TokenCache:
    """线程安全的 access_token 缓存。

    使用稳定版接口 (POST /cgi-bin/stable_token)，与 /cgi-bin/token 互相隔离。
    """

    def __init__(self) -> None:
        self._token: str = ""
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def get(self) -> str:
        """获取当前有效 token；过期时自动刷新。"""
        now = time.time()
        if self._token and now < (self._expires_at - _TOKEN_EXPIRY_BUFFER):
            return self._token

        with self._lock:
            # 双重检查：锁内再次判断，避免重复刷新
            now = time.time()
            if self._token and now < (self._expires_at - _TOKEN_EXPIRY_BUFFER):
                return self._token
            self._refresh()
            return self._token

    def _refresh(self) -> None:
        """调用稳定版接口获取新 token。AppSecret 不写入日志。"""
        if not WX_APPID or not WX_APPSECRET:
            raise WxSecurityConfigError(
                "WX_APPID 和 WX_APPSECRET 环境变量未配置"
            )

        url = f"{_WX_API_BASE}/cgi-bin/stable_token"
        payload = {
            "grant_type": "client_credential",
            "appid": WX_APPID,
            "secret": WX_APPSECRET,
            "force_refresh": False,
        }

        try:
            with _build_http_client() as client:
                response = client.post(url, json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise WxSecurityServiceError(
                f"获取 access_token 网络异常: {exc}"
            ) from exc

        data = response.json()
        if "access_token" in data:
            self._token = data["access_token"]
            expires_in = data.get("expires_in", 7200)
            self._expires_at = time.time() + expires_in
            logger.info("access_token 刷新成功, expires_in=%s", expires_in)
        else:
            errcode = data.get("errcode", -1)
            errmsg = data.get("errmsg", "unknown")
            # 禁止打印 access_token 或 secret
            raise WxSecurityServiceError(
                f"获取 access_token 失败: errcode={errcode} errmsg={errmsg}",
                errcode=errcode,
            )

    def invalidate(self) -> None:
        """强制使当前 token 失效（用于 40001 错误后重试）。"""
        with self._lock:
            self._token = ""
            self._expires_at = 0.0


# 模块级单例
_token_cache = _TokenCache()


def get_access_token() -> str:
    """获取有效的 access_token。"""
    return _token_cache.get()


def invalidate_token() -> None:
    """使缓存的 access_token 失效。"""
    _token_cache.invalidate()


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------

def _build_http_client() -> httpx.Client:
    return httpx.Client(timeout=WX_API_TIMEOUT)


# ---------------------------------------------------------------------------
# 文本内容安全识别 (msg_sec_check v2)
# ---------------------------------------------------------------------------

_MSG_SEC_CHECK_URL = f"{_WX_API_BASE}/wxa/msg_sec_check"


def check_text(
    content: str,
    openid: str,
    scene: int | None = None,
    title: str | None = None,
    nickname: str | None = None,
) -> TextCheckResult:
    """同步检查文本内容是否合规。

    Args:
        content: 待检测文本，上限 2500 字，UTF-8 编码。
        openid: 用户 openid（需近两小时内访问过小程序）。
        scene: 场景：1=资料 2=评论 3=论坛 4=社交日志。
        title: 可选，文本标题。
        nickname: 可选，用户昵称。

    Returns:
        TextCheckResult。

    Raises:
        WxSecurityRejectedError: 严格模式下内容违规。
        WxSecurityServiceError: 审核服务异常。
        WxSecurityConfigError: 配置错误。
        ValueError: 参数不合法。
    """
    if not content or not content.strip():
        raise ValueError("待检测文本不能为空")

    if len(content) > 2500:
        raise ValueError(f"文本长度 {len(content)} 超过 2500 字上限")

    if not openid or not openid.strip():
        raise ValueError("openid 不能为空")

    _scene = scene if scene is not None else WX_MSG_SEC_CHECK_SCENE
    if _scene not in (1, 2, 3, 4):
        raise ValueError(f"scene 必须为 1/2/3/4，当前值: {_scene}")

    payload: dict[str, Any] = {
        "content": content,
        "version": 2,
        "scene": _scene,
        "openid": openid,
    }
    if title:
        payload["title"] = title
    if nickname:
        payload["nickname"] = nickname

    data = _wx_post_json(
        _MSG_SEC_CHECK_URL,
        payload,
        openid=openid,
    )

    result_data = data.get("result", {})
    suggest = result_data.get("suggest", "pass")
    label = result_data.get("label", 100)
    detail = data.get("detail", [])
    trace_id = data.get("trace_id", "")

    result = TextCheckResult(
        suggest=suggest,
        label=label,
        trace_id=trace_id,
        detail=detail,
        raw=data,
    )

    logger.info(
        "文本审核完成: suggest=%s label=%s(%s) trace_id=%s",
        suggest,
        label,
        label_description(label),
        trace_id,
    )

    # 严格模式：审核不通过直接拒绝
    if WX_CONTENT_SECURITY_STRICT and not result.is_pass:
        raise WxSecurityRejectedError(
            f"文本内容安全审核不通过：{label_description(label)}",
            label=label,
            suggest=suggest,
        )

    return result


# ---------------------------------------------------------------------------
# 多媒体内容安全识别 (media_check_async v2)
# ---------------------------------------------------------------------------

_MEDIA_CHECK_ASYNC_URL = f"{_WX_API_BASE}/wxa/media_check_async"


def check_image(
    media_url: str,
    openid: str,
    scene: int | None = None,
) -> MediaCheckResult:
    """异步提交图片审核。

    本接口为**异步**，调用后立即返回 trace_id。审核结果由微信服务器
    在 30 分钟内通过事件推送（wxa_media_check）发到配置的回调地址。
    不会伪造同步结果。

    Args:
        media_url: 图片的公网可访问 URL。支持 jpg/jpeg/png/bmp/gif。
        openid: 用户 openid（需近两小时内访问过小程序）。
        scene: 场景：1=资料 2=评论 3=论坛 4=社交日志。

    Returns:
        MediaCheckResult（仅含 trace_id，不含审核结论）。
    """
    return _submit_media_check(
        media_url=media_url,
        media_type=2,  # 图片
        openid=openid,
        scene=scene,
    )


def check_audio(
    media_url: str,
    openid: str,
    scene: int | None = None,
) -> MediaCheckResult:
    """异步提交音频审核。

    本接口为**异步**，调用后立即返回 trace_id。审核结果由微信服务器
    在 30 分钟内通过事件推送（wxa_media_check）发到配置的回调地址。

    Args:
        media_url: 音频的公网可访问 URL。支持 mp3/aac/ac3/wma/flac/vorbis/opus/wav。
        openid: 用户 openid。
        scene: 场景。

    Returns:
        MediaCheckResult（仅含 trace_id）。
    """
    return _submit_media_check(
        media_url=media_url,
        media_type=1,  # 音频
        openid=openid,
        scene=scene,
    )


def _submit_media_check(
    media_url: str,
    media_type: int,
    openid: str,
    scene: int | None = None,
) -> MediaCheckResult:
    """提交多媒体审核的通用实现。"""
    if not media_url or not media_url.strip():
        raise ValueError("media_url 不能为空")

    if not openid or not openid.strip():
        raise ValueError("openid 不能为空")

    if media_type not in (1, 2):
        raise ValueError(f"media_type 必须为 1(音频) 或 2(图片)，当前值: {media_type}")

    _scene = scene if scene is not None else WX_MEDIA_CHECK_SCENE
    if _scene not in (1, 2, 3, 4):
        raise ValueError(f"scene 必须为 1/2/3/4，当前值: {_scene}")

    payload = {
        "media_url": media_url,
        "media_type": media_type,
        "version": 2,
        "scene": _scene,
        "openid": openid,
    }

    data = _wx_post_json(
        _MEDIA_CHECK_ASYNC_URL,
        payload,
        openid=openid,
    )

    trace_id = data.get("trace_id", "")

    logger.info(
        "多媒体审核已提交: media_type=%s trace_id=%s (结果将异步推送)",
        "音频" if media_type == 1 else "图片",
        trace_id,
    )

    return MediaCheckResult(trace_id=trace_id, raw=data)


# ---------------------------------------------------------------------------
# 微信 API 底层调用
# ---------------------------------------------------------------------------

def _wx_post_json(
    url: str,
    payload: dict[str, Any],
    openid: str = "",
) -> dict[str, Any]:
    """向微信 API 发起 POST JSON 请求，含有限重试和 token 刷新。

    安全规则：
    - AppSecret 绝不写入请求体、日志或异常消息。
    - access_token 仅写入 URL 查询参数，不打印。
    """
    last_error: Exception | None = None

    for attempt in range(1 + WX_API_MAX_RETRIES):
        try:
            token = get_access_token()
            full_url = f"{url}?access_token={token}"

            with _build_http_client() as client:
                resp = client.post(full_url, json=payload)
                resp.raise_for_status()

            data = resp.json()
            errcode = data.get("errcode", 0)

            if errcode == 0:
                return data

            # token 过期或无效 → 刷新后重试一次
            if errcode in (40001, 42001):
                if attempt == 0:
                    logger.warning("access_token 无效，刷新后重试")
                    invalidate_token()
                    continue
                raise WxSecurityServiceError(
                    f"微信 API 返回鉴权错误: errcode={errcode} errmsg={data.get('errmsg', '')}",
                    errcode=errcode,
                )

            # 频率限制 → 退避后重试
            if errcode in (44991, 45009):
                if attempt < WX_API_MAX_RETRIES:
                    wait = WX_API_RETRY_BACKOFF * (2 ** attempt)
                    logger.warning(
                        "微信 API 频率限制 (errcode=%s)，%ss 后重试 (%s/%s)",
                        errcode,
                        wait,
                        attempt + 1,
                        WX_API_MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise WxSecurityServiceError(
                    f"微信 API 调用频率超限: errcode={errcode}",
                    errcode=errcode,
                )

            # 其他业务错误 → 不重试
            raise WxSecurityServiceError(
                f"微信 API 返回错误: errcode={errcode} errmsg={data.get('errmsg', '')}",
                errcode=errcode,
            )

        except httpx.TimeoutException as exc:
            last_error = exc
            if attempt < WX_API_MAX_RETRIES:
                wait = WX_API_RETRY_BACKOFF * (2 ** attempt)
                logger.warning(
                    "微信 API 超时，%ss 后重试 (%s/%s)",
                    wait,
                    attempt + 1,
                    WX_API_MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            raise WxSecurityServiceError(
                f"微信 API 请求超时（已重试 {WX_API_MAX_RETRIES} 次）"
            ) from exc

        except httpx.HTTPStatusError as exc:
            last_error = exc
            if attempt < WX_API_MAX_RETRIES and exc.response.status_code >= 500:
                wait = WX_API_RETRY_BACKOFF * (2 ** attempt)
                logger.warning(
                    "微信 API HTTP %s，%ss 后重试 (%s/%s)",
                    exc.response.status_code,
                    wait,
                    attempt + 1,
                    WX_API_MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            raise WxSecurityServiceError(
                f"微信 API HTTP 错误: {exc.response.status_code}"
            ) from exc

        except httpx.HTTPError as exc:
            last_error = exc
            if attempt < WX_API_MAX_RETRIES:
                wait = WX_API_RETRY_BACKOFF * (2 ** attempt)
                logger.warning(
                    "微信 API 网络异常，%ss 后重试 (%s/%s)",
                    wait,
                    attempt + 1,
                    WX_API_MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            raise WxSecurityServiceError(
                f"微信 API 网络异常（已重试 {WX_API_MAX_RETRIES} 次）: {exc}"
            ) from exc

    # 理论上不会到这里
    raise WxSecurityServiceError(
        f"微信 API 调用失败（已重试 {WX_API_MAX_RETRIES} 次）"
    ) from last_error


# ---------------------------------------------------------------------------
# 微信回调处理
# ---------------------------------------------------------------------------

def parse_media_check_callback(
    raw_body: str | bytes,
) -> MediaCheckCallbackResult | None:
    """解析微信服务器推送的 media_check_async 审核结果。

    推送格式（JSON）:
    {
        "ToUserName": "...",
        "FromUserName": "...",
        "CreateTime": 1234567890,
        "MsgType": "event",
        "Event": "wxa_media_check",
        "appid": "...",
        "trace_id": "...",
        "version": 2,
        "errcode": 0,
        "result": {"suggest": "pass", "label": 100},
        "detail": [...]
    }

    Returns:
        MediaCheckCallbackResult 或 None（非审核事件）。
    """
    if isinstance(raw_body, bytes):
        raw_body = raw_body.decode("utf-8", errors="replace")

    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.warning("微信回调 JSON 解析失败")
        return None

    if data.get("MsgType") != "event":
        return None
    if data.get("Event") != "wxa_media_check":
        return None

    errcode = data.get("errcode", 0)
    result_data = data.get("result", {})
    suggest = result_data.get("suggest", "pass")
    label = result_data.get("label", 100)

    logger.info(
        "收到微信审核回调: trace_id=%s suggest=%s label=%s(%s)",
        data.get("trace_id", ""),
        suggest,
        label,
        label_description(label),
    )

    return MediaCheckCallbackResult(
        trace_id=data.get("trace_id", ""),
        appid=data.get("appid", ""),
        suggest=suggest,
        label=label,
        detail=data.get("detail", []),
        raw=data,
    )


def handle_callback_cleanup(
    result: MediaCheckCallbackResult,
    file_cleaner: Callable[[], None],
) -> None:
    """根据审核回调结果执行清理。

    当 suggest 为 risky 时，调用 file_cleaner 删除文件。

    Args:
        result: 解析后的回调结果。
        file_cleaner: 文件清理回调函数。
    """
    if result.is_rejected:
        logger.warning(
            "审核不通过，清理文件: trace_id=%s label=%s",
            result.trace_id,
            label_description(result.label),
        )
        try:
            file_cleaner()
        except Exception:
            logger.exception("审核失败后文件清理异常")


# ---------------------------------------------------------------------------
# 内容提取辅助函数
# ---------------------------------------------------------------------------

def extract_pdf_text(
    input_path: Path,
    max_chars: int = 2500,
) -> str:
    """从 PDF 文件中提取文本内容（用于文本审核）。

    Args:
        input_path: PDF 文件路径。
        max_chars: 最大提取字符数（默认 2500，与 msg_sec_check 上限一致）。

    Returns:
        提取的文本内容。
    """
    try:
        import fitz
    except ImportError:
        raise WxSecurityServiceError("PyMuPDF 未安装，无法提取 PDF 文本")

    try:
        doc = fitz.open(str(input_path))
    except Exception as exc:
        raise WxSecurityServiceError(f"无法打开 PDF 文件: {exc}") from exc

    try:
        text_parts: list[str] = []
        total = 0
        for page in doc:
            page_text = page.get_text()
            if page_text:
                remaining = max_chars - total
                if remaining <= 0:
                    break
                text_parts.append(page_text[:remaining])
                total += len(page_text)
        return "".join(text_parts)[:max_chars].strip()
    finally:
        doc.close()


def extract_video_keyframes(
    input_path: Path,
    output_dir: Path,
    ffmpeg_bin: str = "ffmpeg",
    max_frames: int = 3,
    timeout: int = 60,
) -> list[Path]:
    """从视频中抽取有限数量关键帧（用于图片审核）。

    使用 FFmpeg 的 select 滤镜抽取 I 帧。

    Args:
        input_path: 视频文件路径。
        output_dir: 输出目录。
        ffmpeg_bin: FFmpeg 可执行文件路径。
        max_frames: 最大抽取帧数（默认 3）。
        timeout: FFmpeg 执行超时秒数。

    Returns:
        抽取的关键帧文件路径列表。
    """
    import subprocess

    output_dir.mkdir(parents=True, exist_ok=True)
    output_pattern = str(output_dir / "keyframe_%03d.jpg")

    command = [
        ffmpeg_bin,
        "-y",
        "-skip_frame",
        "nokey",
        "-i",
        str(input_path),
        "-vsync",
        "vfr",
        "-vf",
        f"select='eq(pict_type,I)',scale='iw:ih'",
        "-frames:v",
        str(max_frames),
        "-q:v",
        "2",
        output_pattern,
    ]

    try:
        subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise WxSecurityServiceError("视频关键帧抽取超时") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        raise WxSecurityServiceError(
            f"视频关键帧抽取失败: {stderr[-200:]}"
        ) from exc
    except FileNotFoundError as exc:
        raise WxSecurityServiceError(
            f"FFmpeg 未安装或路径无效: {ffmpeg_bin}"
        ) from exc

    frames = sorted(output_dir.glob("keyframe_*.jpg"))
    logger.info("抽取到 %s 个关键帧", len(frames))
    return frames


def extract_video_audio_clip(
    input_path: Path,
    output_path: Path,
    ffmpeg_bin: str = "ffmpeg",
    duration: int = 30,
    timeout: int = 60,
) -> Path:
    """从视频中提取短音频片段（用于音频审核）。

    从视频开头截取指定时长的音频，转换为 mp3 格式。

    Args:
        input_path: 视频文件路径。
        output_path: 输出音频文件路径。
        ffmpeg_bin: FFmpeg 可执行文件路径。
        duration: 提取时长秒数（默认 30）。
        timeout: FFmpeg 执行超时秒数。

    Returns:
        输出文件路径。
    """
    import subprocess

    command = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(input_path),
        "-t",
        str(duration),
        "-vn",
        "-acodec",
        "libmp3lame",
        "-q:a",
        "5",
        str(output_path),
    ]

    try:
        subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise WxSecurityServiceError("音频片段提取超时") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        raise WxSecurityServiceError(
            f"音频片段提取失败: {stderr[-200:]}"
        ) from exc
    except FileNotFoundError as exc:
        raise WxSecurityServiceError(
            f"FFmpeg 未安装或路径无效: {ffmpeg_bin}"
        ) from exc

    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise WxSecurityServiceError("音频片段提取失败：输出文件为空")

    logger.info("音频片段提取完成: %s", output_path)
    return output_path


# ---------------------------------------------------------------------------
# 统一错误码与响应消息
# ---------------------------------------------------------------------------

# 安全审核相关错误码（供 web 层使用）
SECURITY_ERROR_CODES: dict[str, tuple[int, str, str]] = {
    "CONTENT_REJECTED": (400, "CONTENT_REJECTED", "内容安全审核不通过，请修改后重试"),
    "SECURITY_SERVICE_ERROR": (500, "SECURITY_SERVICE_ERROR", "内容安全服务异常，请稍后重试"),
    "SECURITY_CONFIG_ERROR": (500, "SECURITY_CONFIG_ERROR", "内容安全服务未正确配置"),
}


def security_error_response(exc: WxSecurityError) -> dict[str, Any]:
    """将内容安全异常转换为统一错误响应。

    >>> try:
    ...     raise WxSecurityRejectedError("违规", label=20002)
    ... except WxSecurityError as e:
    ...     resp = security_error_response(e)
    ...     assert resp["success"] == False
    """
    return {
        "success": False,
        "code": exc.code,
        "message": exc.message,
    }


# ---------------------------------------------------------------------------
# 模块自检
# ---------------------------------------------------------------------------

def is_configured() -> bool:
    """检查模块是否已正确配置。"""
    return bool(WX_APPID and WX_APPSECRET)


# ---------------------------------------------------------------------------
# OpenID Token 签名 / 验证
# ---------------------------------------------------------------------------
#
# openid 必须从可信登录会话取得或通过签名验证，不能直接信任任意表单值。
# 前端或登录服务使用共享的 OPENID_SIGNING_KEY 对 openid 签名后传给本服务。

OPENID_SIGNING_KEY: str = os.getenv("OPENID_SIGNING_KEY", "").strip()
OPENID_TOKEN_TTL: int = int(os.getenv("OPENID_TOKEN_TTL", "3600"))


def create_openid_token(openid: str, ttl: int | None = None) -> str:
    """创建一个包含 openid 的签名令牌。

    格式: base64(json({"openid":..., "exp":..., "sig":...}))

    Args:
        openid: 用户 openid。
        ttl: 有效期秒数，默认 OPENID_TOKEN_TTL (3600)。

    Returns:
        Base64 编码的令牌字符串。
    """
    import base64
    import hashlib
    import hmac
    import json

    if not OPENID_SIGNING_KEY:
        raise WxSecurityConfigError("OPENID_SIGNING_KEY 环境变量未配置，无法签发 openid 令牌")

    _ttl = ttl if ttl is not None else OPENID_TOKEN_TTL
    exp = int(time.time()) + _ttl

    payload = json.dumps({"openid": openid, "exp": exp}, separators=(",", ":"))
    sig = hmac.new(
        OPENID_SIGNING_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    token_data = json.dumps(
        {"openid": openid, "exp": exp, "sig": sig},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(token_data.encode("utf-8")).decode("ascii")


def verify_openid_token(token: str) -> str:
    """验证 openid 签名令牌并提取 openid。

    Args:
        token: create_openid_token() 生成的令牌。

    Returns:
        验证通过的 openid。

    Raises:
        ValueError: 令牌无效、过期或签名不匹配。
        WxSecurityConfigError: OPENID_SIGNING_KEY 未配置。
    """
    import base64
    import hashlib
    import hmac
    import json

    if not token or not token.strip():
        raise ValueError("openid_token 不能为空")

    if not OPENID_SIGNING_KEY:
        raise WxSecurityConfigError("OPENID_SIGNING_KEY 环境变量未配置，无法验证 openid 令牌")

    try:
        token_data = json.loads(
            base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        )
    except (base64.binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"openid_token 格式无效: {exc}") from exc

    openid = token_data.get("openid", "")
    exp = token_data.get("exp", 0)
    sig = token_data.get("sig", "")

    if not openid or not exp or not sig:
        raise ValueError("openid_token 缺少必要字段 (openid/exp/sig)")

    # 验证签名（重新计算 payload 的 HMAC）
    payload = json.dumps({"openid": openid, "exp": exp}, separators=(",", ":"))
    expected_sig = hmac.new(
        OPENID_SIGNING_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(sig, expected_sig):
        raise ValueError("openid_token 签名不匹配")

    if time.time() > exp:
        raise ValueError("openid_token 已过期")

    return openid


# ---------------------------------------------------------------------------
# 微信回调加解密
# ---------------------------------------------------------------------------
#
# 基于微信官方文档实现：
# - URL 验证 (GET echostr): SHA1 签名校验 + AES 解密回传
# - 事件推送 (POST): msg_signature 校验 + AES 解密 JSON 消息体
# - 加解密算法: AES-256-CBC, PKCS#7 padding
# - EncodingAESKey: 43 字符，Base64 解码后为 32 字节密钥

WX_CALLBACK_TOKEN: str = os.getenv("WX_CALLBACK_TOKEN", "").strip()
WX_CALLBACK_AES_KEY: str = os.getenv("WX_CALLBACK_AES_KEY", "").strip()


def _wx_sha1_signature(*parts: str) -> str:
    """计算微信签名: SHA1(sort([token, timestamp, nonce, ...]))."""
    import hashlib

    sorted_parts = sorted(parts)
    raw = "".join(sorted_parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _wx_aes_decrypt(encrypted_base64: str) -> tuple[str, str]:
    """AES-256-CBC 解密微信加密消息。

    解密后格式: random(16) + msg_len(4, network byte order) + msg + appid

    Args:
        encrypted_base64: Base64 编码的密文。

    Returns:
        (明文消息, appid) 元组。

    Raises:
        WxSecurityServiceError: 解密失败。
    """
    import base64
    import struct

    if not WX_CALLBACK_AES_KEY:
        raise WxSecurityConfigError("WX_CALLBACK_AES_KEY 未配置，无法解密微信回调消息")

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise WxSecurityServiceError(
            "cryptography 库未安装，无法解密微信回调消息"
        ) from exc

    # Base64 解码 AES 密钥（43 字符 + "=" → 32 字节）
    aes_key = base64.b64decode(WX_CALLBACK_AES_KEY + "=")
    if len(aes_key) != 32:
        raise WxSecurityServiceError(
            f"WX_CALLBACK_AES_KEY 解码后长度应为 32 字节，实际: {len(aes_key)}"
        )

    # Base64 解码密文
    try:
        ciphertext = base64.b64decode(encrypted_base64)
    except (base64.binascii.Error, ValueError) as exc:
        raise WxSecurityServiceError(f"微信回调密文 Base64 解码失败: {exc}") from exc

    # AES-256-CBC 解密，IV = key[:16]
    iv = aes_key[:16]
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()

    # 去除 PKCS#7 填充
    pad_len = plaintext[-1]
    if pad_len < 1 or pad_len > 32:
        raise WxSecurityServiceError(
            f"微信回调解密后 PKCS#7 填充长度异常: {pad_len}"
        )
    plaintext = plaintext[:-pad_len]

    # 解析: random(16) + msg_len(4) + msg + appid
    if len(plaintext) < 20:
        raise WxSecurityServiceError("微信回调解密后数据过短")

    # msg_len 是大端 4 字节无符号整数
    msg_len = struct.unpack(">I", plaintext[16:20])[0]
    msg = plaintext[20 : 20 + msg_len].decode("utf-8", errors="replace")
    appid = plaintext[20 + msg_len :].decode("utf-8", errors="replace")

    logger.info("微信回调解密成功: appid=%s msg_len=%s", appid, msg_len)
    return msg, appid


def _wx_aes_encrypt(plaintext: str) -> str:
    """AES-256-CBC 加密消息（用于 URL 验证回传 echostr）。

    Args:
        plaintext: 明文字符串。

    Returns:
        Base64 编码的密文。
    """
    import base64
    import os as _os
    import struct

    if not WX_CALLBACK_AES_KEY:
        raise WxSecurityConfigError("WX_CALLBACK_AES_KEY 未配置")

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise WxSecurityServiceError(
            "cryptography 库未安装"
        ) from exc

    aes_key = base64.b64decode(WX_CALLBACK_AES_KEY + "=")
    iv = aes_key[:16]

    msg_bytes = plaintext.encode("utf-8")
    # 组装: random(16) + msg_len(4) + msg + appid
    random_bytes = _os.urandom(16)  # noqa: S311 — 仅用于 IV 混淆，非安全随机
    msg_len_bytes = struct.pack(">I", len(msg_bytes))
    appid_bytes = WX_APPID.encode("utf-8")
    raw = random_bytes + msg_len_bytes + msg_bytes + appid_bytes

    # PKCS#7 填充
    block_size = 32
    pad_len = block_size - (len(raw) % block_size)
    raw += bytes([pad_len] * pad_len)

    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(raw) + encryptor.finalize()

    return base64.b64encode(ciphertext).decode("ascii")


def verify_wx_callback_signature(
    signature: str,
    timestamp: str,
    nonce: str,
    encrypted_msg: str = "",
) -> bool:
    """验证微信回调请求签名。

    对于 GET (URL 验证): signature == SHA1([token, timestamp, nonce, echostr])
    对于 POST (事件推送): msg_signature == SHA1([token, timestamp, nonce, Encrypt])

    Args:
        signature: URL 参数中的 signature（GET）或 msg_signature（POST）。
        timestamp: URL 参数 timestamp。
        nonce: URL 参数 nonce。
        encrypted_msg: GET 时为 echostr 值，POST 时为 body 中的 Encrypt 字段。

    Returns:
        签名是否有效。
    """
    if not WX_CALLBACK_TOKEN:
        logger.warning("WX_CALLBACK_TOKEN 未配置，跳过签名验证")
        return True  # 未配置时跳过验证（开发环境）

    parts = [WX_CALLBACK_TOKEN, timestamp, nonce]
    if encrypted_msg:
        parts.append(encrypted_msg)

    expected = _wx_sha1_signature(*parts)
    return signature == expected


# ---------------------------------------------------------------------------
# 审核任务持久化存储
# ---------------------------------------------------------------------------
#
# 用于追踪 media_check_async 提交后的异步审核状态。
# 数据存储在 JSON 文件中，支持容器重启后状态恢复。

WX_SECURITY_TASK_STORE: str = os.getenv(
    "WX_SECURITY_TASK_STORE", "data/security-tasks.json"
)
WX_SECURITY_TASK_TTL: int = int(os.getenv("WX_SECURITY_TASK_TTL", "7200"))
# 回调超时：超过此时间未收到回调的任务标记为 error
WX_SECURITY_CALLBACK_TIMEOUT: int = int(
    os.getenv("WX_SECURITY_CALLBACK_TIMEOUT", "1800")
)


@dataclass
class AuditTask:
    """异步审核任务记录。"""

    job_id: str
    trace_id: str
    file_path: str  # 输出文件路径（用于审核不通过时删除）
    status: str  # pending / approved / rejected / error / expired
    created_at: float
    updated_at: float
    result: dict[str, Any] | None = None
    media_url: str = ""
    openid_hash: str = ""  # openid 的 SHA256 哈希，保护隐私
    media_type: str = ""  # "image" / "audio"

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "trace_id": self.trace_id,
            "file_path": self.file_path,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": self.result,
            "media_url": self.media_url,
            "openid_hash": self.openid_hash,
            "media_type": self.media_type,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AuditTask":
        return cls(
            job_id=d["job_id"],
            trace_id=d["trace_id"],
            file_path=d["file_path"],
            status=d["status"],
            created_at=d["created_at"],
            updated_at=d["updated_at"],
            result=d.get("result"),
            media_url=d.get("media_url", ""),
            openid_hash=d.get("openid_hash", ""),
            media_type=d.get("media_type", ""),
        )


class AuditTaskStore:
    """持久化审核任务存储。

    线程安全，数据存储在 JSON 文件中。
    """

    def __init__(self, store_path: str) -> None:
        self._store_path = Path(store_path)
        self._lock = threading.Lock()
        self._tasks: dict[str, AuditTask] = {}
        self._trace_index: dict[str, str] = {}  # trace_id → job_id

    def _ensure_dir(self) -> None:
        self._store_path.parent.mkdir(parents=True, exist_ok=True)

    def _save(self) -> None:
        """持久化到 JSON 文件。"""
        self._ensure_dir()
        data = {
            "version": 1,
            "updated": time.time(),
            "tasks": {k: v.to_dict() for k, v in self._tasks.items()},
        }
        tmp_path = self._store_path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self._store_path)  # 原子替换

    def _load(self) -> int:
        """从 JSON 文件加载任务。返回加载数量。"""
        if not self._store_path.exists():
            return 0
        try:
            data = json.loads(
                self._store_path.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("审核任务持久化文件读取失败: %s", exc)
            return 0

        tasks_data = data.get("tasks", {})
        for job_id, task_dict in tasks_data.items():
            try:
                task = AuditTask.from_dict(task_dict)
                self._tasks[job_id] = task
                if task.trace_id:
                    self._trace_index[task.trace_id] = job_id
            except (KeyError, TypeError) as exc:
                logger.warning("跳过无效任务记录 %s: %s", job_id, exc)

        logger.info(
            "从持久化文件恢复 %s 个审核任务 (文件: %s)",
            len(self._tasks),
            self._store_path,
        )
        return len(self._tasks)

    def create_task(
        self,
        job_id: str,
        trace_id: str,
        file_path: str,
        openid: str = "",
        media_url: str = "",
        media_type: str = "",
    ) -> AuditTask:
        """创建新的审核任务。"""
        import hashlib

        openid_hash = ""
        if openid:
            openid_hash = hashlib.sha256(
                openid.encode("utf-8")
            ).hexdigest()[:16]

        now = time.time()
        task = AuditTask(
            job_id=job_id,
            trace_id=trace_id,
            file_path=file_path,
            status="pending",
            created_at=now,
            updated_at=now,
            media_url=media_url,
            openid_hash=openid_hash,
            media_type=media_type,
        )

        with self._lock:
            self._tasks[job_id] = task
            if trace_id:
                self._trace_index[trace_id] = job_id
            self._save()

        logger.info(
            "审核任务已创建: job_id=%s trace_id=%s media_type=%s",
            job_id,
            trace_id,
            media_type,
        )
        return task

    def get_task(self, job_id: str) -> AuditTask | None:
        """按 job_id 查询任务。"""
        with self._lock:
            return self._tasks.get(job_id)

    def get_task_by_trace_id(self, trace_id: str) -> AuditTask | None:
        """按 trace_id 查询任务。"""
        with self._lock:
            job_id = self._trace_index.get(trace_id)
            if job_id:
                return self._tasks.get(job_id)
            return None

    def update_by_trace_id(
        self,
        trace_id: str,
        status: str,
        result: dict[str, Any] | None = None,
    ) -> AuditTask | None:
        """根据回调 trace_id 更新任务状态。

        审核通过 → approved；审核不通过 → rejected。
        """
        with self._lock:
            job_id = self._trace_index.get(trace_id)
            if not job_id:
                logger.warning(
                    "收到未知 trace_id 的回调: %s (可能任务已过期)",
                    trace_id,
                )
                return None
            task = self._tasks.get(job_id)
            if not task:
                return None

            task.status = status
            task.updated_at = time.time()
            if result is not None:
                task.result = result
            self._save()

        logger.info(
            "审核任务状态更新: job_id=%s trace_id=%s status=%s",
            job_id,
            trace_id,
            status,
        )
        return task

    def cleanup_expired(self, ttl: int | None = None) -> int:
        """清理过期任务记录。

        过期任务（超过 TTL 且状态仍为 pending）标记为 expired。
        超过 2 倍 TTL 的记录直接删除。

        Args:
            ttl: 任务存活秒数，默认 WX_SECURITY_TASK_TTL。

        Returns:
            清理/删除数量。
        """
        _ttl = ttl if ttl is not None else WX_SECURITY_TASK_TTL
        now = time.time()
        removed = 0
        expired = 0

        with self._lock:
            stale_job_ids: list[str] = []
            for job_id, task in list(self._tasks.items()):
                age = now - task.created_at
                if age > _ttl * 2:
                    # 完全删除过期很久的任务
                    stale_job_ids.append(job_id)
                    removed += 1
                elif age > _ttl and task.status == "pending":
                    # 超时未收到回调 → 标记 error
                    task.status = "error"
                    task.updated_at = now
                    task.result = {
                        "error": "CALLBACK_TIMEOUT",
                        "message": f"审核回调超时（已等待 {int(age)} 秒）",
                    }
                    expired += 1

            for job_id in stale_job_ids:
                task = self._tasks.pop(job_id, None)
                if task and task.trace_id:
                    self._trace_index.pop(task.trace_id, None)

            if removed > 0 or expired > 0:
                self._save()

        if removed > 0:
            logger.info("已删除 %s 个过期审核任务记录", removed)
        if expired > 0:
            logger.warning("已将 %s 个超时任务标记为 error", expired)

        return removed + expired

    @property
    def task_count(self) -> int:
        """当前任务总数。"""
        with self._lock:
            return len(self._tasks)


# 模块级任务存储单例
_task_store: AuditTaskStore | None = None


def get_task_store() -> AuditTaskStore:
    """获取模块级任务存储实例（懒加载 + 容器重启恢复）。"""
    global _task_store
    if _task_store is None:
        _task_store = AuditTaskStore(WX_SECURITY_TASK_STORE)
        _task_store._load()
    return _task_store


# ---------------------------------------------------------------------------
# 微信回调处理（供 Web 层路由使用）
# ---------------------------------------------------------------------------


def handle_callback_url_verification(
    signature: str,
    timestamp: str,
    nonce: str,
    echostr: str,
) -> tuple[bool, str | None]:
    """处理微信 GET 回调：URL 验证。

    微信公众平台在配置消息推送地址时会发送 GET 请求验证 URL 有效性。

    Args:
        signature: 微信签名字符串。
        timestamp: 时间戳。
        nonce: 随机数。
        echostr: 加密的随机字符串（需解密后回传）。

    Returns:
        (验证是否通过, 解密后的 echostr 或 None)。
    """
    # 1. 验证签名
    if not verify_wx_callback_signature(signature, timestamp, nonce, echostr):
        logger.warning("URL 验证签名不匹配")
        return False, None

    # 2. 解密 echostr
    if WX_CALLBACK_AES_KEY:
        try:
            plaintext, _ = _wx_aes_decrypt(echostr)
            return True, plaintext
        except WxSecurityError as exc:
            logger.error("URL 验证 echostr 解密失败: %s", exc)
            return False, None

    # 明文模式（未配置 AES Key）：直接返回 echostr
    return True, echostr


def handle_callback_event(
    body: str | bytes,
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
) -> MediaCheckCallbackResult | None:
    """处理微信 POST 回调：事件推送。

    处理两种格式：
    1. 加密 JSON: {"ToUserName":"...", "Encrypt":"<base64>"}
    2. 明文 JSON: {"ToUserName":"...", "MsgType":"event", ...}

    Args:
        body: POST 请求体。
        msg_signature: URL 参数 msg_signature。
        timestamp: URL 参数 timestamp。
        nonce: URL 参数 nonce。

    Returns:
        MediaCheckCallbackResult 或 None（非审核事件）。
    """
    if isinstance(body, bytes):
        raw_body_str = body.decode("utf-8", errors="replace")
    else:
        raw_body_str = body

    # 尝试解析 JSON
    try:
        outer = json.loads(raw_body_str)
    except json.JSONDecodeError:
        logger.warning("微信回调 POST body 非 JSON")
        return None

    # ---- 检查是否为加密消息 ----
    encrypt_field = outer.get("Encrypt", "")
    if encrypt_field:
        # ---- 加密模式：先验证 msg_signature ----
        if msg_signature and timestamp and nonce:
            if not verify_wx_callback_signature(
                msg_signature, timestamp, nonce, encrypt_field
            ):
                logger.warning("微信回调 POST msg_signature 验证失败")
                return None
        elif msg_signature:
            logger.warning(
                "收到加密回调但缺少 timestamp/nonce，跳过签名验证"
            )

        # 解密
        try:
            plaintext, decrypted_appid = _wx_aes_decrypt(encrypt_field)
        except WxSecurityError as exc:
            logger.error("微信回调消息解密失败: %s", exc)
            return None

        # 验证 appid
        if WX_APPID and decrypted_appid != WX_APPID:
            logger.warning(
                "解密后 appid 不匹配: 期望=%s 实际=%s",
                WX_APPID,
                decrypted_appid,
            )
            # 不阻断，appid 不匹配也可能是多小程序共享回调

        # 解析内层 JSON
        try:
            inner = json.loads(plaintext)
        except json.JSONDecodeError:
            logger.warning("微信回调解密后内容非 JSON")
            return None
    else:
        # ---- 明文模式 ----
        inner = outer

    # ---- 仅处理 wxa_media_check 事件 ----
    if inner.get("MsgType") != "event":
        return None
    if inner.get("Event") != "wxa_media_check":
        return None

    result_data = inner.get("result", {})
    suggest = result_data.get("suggest", "pass")
    label = result_data.get("label", 100)

    callback_result = MediaCheckCallbackResult(
        trace_id=inner.get("trace_id", ""),
        appid=inner.get("appid", ""),
        suggest=suggest,
        label=label,
        detail=inner.get("detail", []),
        raw=inner,
    )

    logger.info(
        "收到微信审核回调: trace_id=%s suggest=%s label=%s(%s)",
        callback_result.trace_id,
        suggest,
        label,
        label_description(label),
    )

    # ---- 更新任务存储 ----
    task_store = get_task_store()
    new_status = "approved" if callback_result.is_pass else "rejected"
    task_store.update_by_trace_id(
        callback_result.trace_id,
        new_status,
        result={
            "suggest": suggest,
            "label": label,
            "label_desc": label_description(label),
            "detail": callback_result.detail,
        },
    )

    # ---- 审核不通过时清理文件 ----
    if callback_result.is_rejected:
        task = task_store.get_task_by_trace_id(callback_result.trace_id)
        if task and task.file_path:
            file_path = Path(task.file_path)
            if file_path.exists():
                try:
                    file_path.unlink()
                    logger.warning(
                        "审核不通过，已删除文件: %s (trace_id=%s)",
                        file_path,
                        callback_result.trace_id,
                    )
                except OSError:
                    logger.exception("审核不通过后文件删除失败: %s", file_path)

    return callback_result


def _hash_openid(openid: str) -> str:
    """对 openid 做不可逆哈希（用于任务记录中的隐私保护）。"""
    import hashlib
    return hashlib.sha256(openid.encode("utf-8")).hexdigest()[:16]
