"""
微信小程序登录服务。

提供 POST /auth/wechat-login 接口：
- 接收 wx.login() 返回的 code
- 调用微信官方 code2Session 获取 openid
- 使用 OPENID_SIGNING_KEY 签发 openidToken
- 不向前端返回原始 openid、session_key、AppSecret 或签名密钥

与媒体/文档服务共享同一个 OPENID_SIGNING_KEY，确保 token 可互通验证。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from parse_video_py.content_security import (
    OPENID_SIGNING_KEY,
    OPENID_TOKEN_TTL,
    WX_APPID,
    WX_APPSECRET,
    WxSecurityConfigError,
    WxSecurityServiceError,
    create_openid_token,
    verify_openid_token,
)
from parse_video_py.user_db import get_or_create_user, get_wechat_openid_for_user
from parse_video_py.qr_auth import (
    cancel_qr_login,
    confirm_qr_login,
    create_qr_login,
    create_web_session,
    exchange_login_ticket,
    get_qr_status,
    mark_qr_scanned,
    verify_web_session,
    WEB_SESSION_TTL_SECONDS,
)

logger = logging.getLogger("auth_web")

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

WX_API_TIMEOUT: float = float(os.getenv("WX_API_TIMEOUT", "10"))
WX_API_MAX_RETRIES: int = int(os.getenv("WX_API_MAX_RETRIES", "3"))
WX_API_RETRY_BACKOFF: float = float(os.getenv("WX_API_RETRY_BACKOFF", "0.5"))

_WX_API_BASE: str = "https://api.weixin.qq.com"
_CODE2SESSION_URL: str = f"{_WX_API_BASE}/sns/jscode2session"

DISABLE_DOCS: bool = os.getenv("DISABLE_DOCS", "").strip().lower() in {
    "1", "true", "yes", "on",
}

# ---------------------------------------------------------------------------
# APIRouter（供 media_convert_web 挂载）
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/auth")


# ---------------------------------------------------------------------------
# 响应构建
# ---------------------------------------------------------------------------

def api_response(code: int, msg: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "msg": msg}
    if data is not None:
        body["data"] = data
    return body


# ---------------------------------------------------------------------------
# code2Session 调用
# ---------------------------------------------------------------------------

def _call_code2session(code: str) -> dict[str, Any]:
    """调用微信官方 code2Session 接口获取 openid 和 session_key。

    GET https://api.weixin.qq.com/sns/jscode2session
        ?appid=APPID&secret=SECRET&js_code=CODE&grant_type=authorization_code

    安全约束：
    - AppSecret 仅出现在 URL 查询参数中，绝不写入日志或响应。
    - session_key 不在日志中输出。

    Args:
        code: wx.login() 返回的临时登录凭证。

    Returns:
        微信返回的原始数据 dict（包含 openid, session_key 等）。

    Raises:
        WxSecurityConfigError: AppID/AppSecret 未配置。
        WxSecurityServiceError: 微信接口调用失败。
    """
    if not WX_APPID or not WX_APPSECRET:
        raise WxSecurityConfigError(
            "WX_APPID 和 WX_APPSECRET 环境变量未配置"
        )

    last_error: Exception | None = None

    for attempt in range(1 + WX_API_MAX_RETRIES):
        try:
            with httpx.Client(timeout=WX_API_TIMEOUT) as client:
                resp = client.get(
                    _CODE2SESSION_URL,
                    params={
                        "appid": WX_APPID,
                        "secret": WX_APPSECRET,
                        "js_code": code,
                        "grant_type": "authorization_code",
                    },
                )
                resp.raise_for_status()

            data = resp.json()
            errcode = data.get("errcode", 0)

            if errcode == 0 and "openid" in data:
                logger.info("code2Session 成功 (openid 已获取，不输出原始值)")
                return data

            # ---- 无效 code（已使用/过期/错误）→ 不重试 ----
            if errcode in (40029, 40163, 41002):
                raise WxSecurityServiceError(
                    f"微信 code2Session 失败: errcode={errcode} "
                    f"errmsg={data.get('errmsg', '')}",
                    errcode=errcode,
                )

            # ---- 频率限制 / 系统错误 → 退避重试 ----
            if errcode in (45011, -1):
                if attempt < WX_API_MAX_RETRIES:
                    wait = WX_API_RETRY_BACKOFF * (2 ** attempt)
                    logger.warning(
                        "code2Session 频率限制/系统错误 (errcode=%s)，"
                        "%ss 后重试 (%s/%s)",
                        errcode, wait, attempt + 1, WX_API_MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise WxSecurityServiceError(
                    f"微信 code2Session 失败（已重试 {WX_API_MAX_RETRIES} 次）: "
                    f"errcode={errcode}",
                    errcode=errcode,
                )

            # ---- 其他业务错误 → 不重试 ----
            raise WxSecurityServiceError(
                f"微信 code2Session 返回错误: errcode={errcode} "
                f"errmsg={data.get('errmsg', '')}",
                errcode=errcode,
            )

        except httpx.TimeoutException as exc:
            last_error = exc
            if attempt < WX_API_MAX_RETRIES:
                wait = WX_API_RETRY_BACKOFF * (2 ** attempt)
                logger.warning(
                    "code2Session 超时，%ss 后重试 (%s/%s)",
                    wait, attempt + 1, WX_API_MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            raise WxSecurityServiceError(
                f"微信 code2Session 请求超时（已重试 {WX_API_MAX_RETRIES} 次）"
            ) from exc

        except httpx.HTTPStatusError as exc:
            last_error = exc
            if attempt < WX_API_MAX_RETRIES and exc.response.status_code >= 500:
                wait = WX_API_RETRY_BACKOFF * (2 ** attempt)
                logger.warning(
                    "code2Session HTTP %s，%ss 后重试 (%s/%s)",
                    exc.response.status_code, wait, attempt + 1, WX_API_MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            raise WxSecurityServiceError(
                f"微信 code2Session HTTP 错误: {exc.response.status_code}"
            ) from exc

        except httpx.HTTPError as exc:
            last_error = exc
            if attempt < WX_API_MAX_RETRIES:
                wait = WX_API_RETRY_BACKOFF * (2 ** attempt)
                logger.warning(
                    "code2Session 网络异常，%ss 后重试 (%s/%s)",
                    wait, attempt + 1, WX_API_MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            raise WxSecurityServiceError(
                f"微信 code2Session 网络异常（已重试 {WX_API_MAX_RETRIES} 次）: {exc}"
            ) from exc

    # 理论上不会到这里
    raise WxSecurityServiceError(
        f"微信 code2Session 失败（已重试 {WX_API_MAX_RETRIES} 次）"
    ) from last_error


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@router.post("/wechat-login")
async def wechat_login(request: Request):
    """微信小程序登录接口。

    接收 wx.login() 返回的临时 code，调用微信 code2Session 获取 openid，
    使用共享的 OPENID_SIGNING_KEY 签发 openidToken 返回给前端。

    请求体:
        {"code": "wx.login() 返回的 code"}

    成功响应:
        {"code": 0, "msg": "ok", "data": {"openidToken": "...", "expiresIn": 7200}}

    安全约束:
        - 不向前端返回原始 openid、session_key、unionid。
        - AppSecret / OPENID_SIGNING_KEY 不写入日志或响应。
        - code 无效时返回明确错误，不泄露内部细节。
    """
    # ---- 1. 解析请求体 ----
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请求体必须是有效的 JSON",
        )

    if not isinstance(body, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请求体必须是 JSON 对象",
        )

    code = (body.get("code") or "").strip()
    if not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="code 不能为空",
        )

    # ---- 2. 检查服务端配置 ----
    if not WX_APPID or not WX_APPSECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="微信登录服务未正确配置 (WX_APPID/WX_APPSECRET)",
        )

    if not OPENID_SIGNING_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="令牌签发服务未正确配置 (OPENID_SIGNING_KEY)",
        )

    # ---- 3. 调用微信 code2Session ----
    try:
        wx_data = _call_code2session(code)
    except WxSecurityConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )
    except WxSecurityServiceError as exc:
        # 区分 code 无效（客户端错误）与服务端异常
        if exc.errcode in (40029, 40163, 41002):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="微信登录失败: code 无效或已过期",
            )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="微信登录服务异常，请稍后重试",
        )

    openid = wx_data.get("openid", "")
    if not openid:
        logger.error("code2Session 返回数据中缺少 openid 字段")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="微信登录返回数据异常",
        )

    # ---- 4. 幂等绑定统一系统用户（不改变现有接口响应） ----
    try:
        get_or_create_user(openid, wx_data.get("unionid"))
    except Exception as exc:
        logger.exception("统一用户写入失败")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="用户服务暂不可用，请稍后重试",
        ) from exc

    # ---- 5. 签发 openidToken（不返回原始 openid） ----
    try:
        token = create_openid_token(openid)
    except WxSecurityConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )

    expires_in = OPENID_TOKEN_TTL

    logger.info(
        "微信登录成功，已签发 openidToken (expiresIn=%s)",
        expires_in,
    )

    return api_response(0, "ok", {
        "openidToken": token,
        "expiresIn": expires_in,
    })


@router.post("/qr/create")
def create_qr():
    """Create a three-minute mini-program code for web login."""
    try:
        return create_qr_login(WX_APPID, WX_APPSECRET)
    except Exception as exc:
        logger.exception("创建网页登录小程序码失败")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="小程序码登录暂不可用",
        ) from exc


@router.get("/qr/status/{scene_token}")
def qr_status(scene_token: str):
    try:
        return get_qr_status(scene_token)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/qr/confirm")
async def confirm_qr(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Confirm web login with the existing mini-program openidToken."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 openidToken")
    try:
        openid = verify_openid_token(authorization[7:].strip())
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="openidToken 无效或已过期") from exc
    try:
        body = await request.json()
        scene_token = str(body.get("scene_token") or "").strip()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象") from exc
    if not scene_token:
        raise HTTPException(status_code=400, detail="scene_token 不能为空")
    try:
        user = get_or_create_user(openid)
        confirm_qr_login(scene_token, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("确认网页登录失败")
        raise HTTPException(status_code=503, detail="用户服务暂不可用") from exc
    return api_response(0, "ok", {"status": "confirmed"})


async def _authenticated_qr_action(
    request: Request,
    authorization: str | None,
    action,
) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 openidToken")
    try:
        openid = verify_openid_token(authorization[7:].strip())
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="openidToken 无效或已过期") from exc
    try:
        body = await request.json()
        scene_token = str(body.get("scene_token") or "").strip()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象") from exc
    if not scene_token:
        raise HTTPException(status_code=400, detail="scene_token 不能为空")
    try:
        user = get_or_create_user(openid)
        action(scene_token, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("更新网页登录状态失败")
        raise HTTPException(status_code=503, detail="用户服务暂不可用") from exc
    return scene_token


@router.post("/qr/scan")
async def scan_qr(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Mark a scene as scanned and bind it to the authenticated mini-program user."""
    await _authenticated_qr_action(request, authorization, mark_qr_scanned)
    return api_response(0, "ok", {"status": "scanned"})


@router.post("/qr/cancel")
async def cancel_qr(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Cancel a scene previously scanned by the same mini-program user."""
    await _authenticated_qr_action(request, authorization, cancel_qr_login)
    return api_response(0, "ok", {"status": "cancelled"})


@router.post("/qr/exchange")
async def exchange_qr(request: Request, response: Response):
    try:
        body = await request.json()
        login_ticket = str(body.get("login_ticket") or "").strip()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象") from exc
    if not login_ticket:
        raise HTTPException(status_code=400, detail="login_ticket 不能为空")
    try:
        user = exchange_login_ticket(login_ticket)
        session_token = create_web_session(user.id)
        openid = get_wechat_openid_for_user(user.id)
        openid_token = create_openid_token(openid)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    response.set_cookie(
        key="web_session", value=session_token, max_age=WEB_SESSION_TTL_SECONDS,
        httponly=True,
        secure=os.getenv("WEB_COOKIE_SECURE", "false").lower() == "true",
        samesite="lax", path="/",
    )
    return {
        "status": "ok",
        "openidToken": openid_token,
        "expiresIn": OPENID_TOKEN_TTL,
    }


@router.get("/me")
def current_user(request: Request):
    token = request.cookies.get("web_session", "")
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        user = verify_web_session(token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {"user": {
        "id": user.id,
        "internal_code": user.internal_code,
        "display_name": user.display_name or "",
        "avatar_url": user.avatar_url,
    }}


@router.post("/token")
def refresh_openid_token(request: Request):
    """Issue a fresh short-lived openidToken for a valid web session."""
    session_token = request.cookies.get("web_session", "")
    if not session_token:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        user = verify_web_session(session_token)
        openid = get_wechat_openid_for_user(user.id)
        openid_token = create_openid_token(openid)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except WxSecurityConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "status": "ok",
        "openidToken": openid_token,
        "expiresIn": OPENID_TOKEN_TTL,
    }


@router.post("/logout", status_code=204)
def logout(response: Response):
    response.delete_cookie(
        "web_session", path="/", httponly=True,
        secure=os.getenv("WEB_COOKIE_SECURE", "false").lower() == "true",
        samesite="lax",
    )


# ---------------------------------------------------------------------------
# 向后兼容：独立 FastAPI 应用（用于测试或独立部署）
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Auth Service",
    docs_url=None if DISABLE_DOCS else "/docs",
    redoc_url=None if DISABLE_DOCS else "/redoc",
    openapi_url=None if DISABLE_DOCS else "/openapi.json",
)

app.include_router(router)


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.status_code, "msg": str(exc.detail)},
    )


@app.get("/health", include_in_schema=False)
def _health_check() -> dict[str, str]:
    return {"status": "ok", "service": "auth"}
