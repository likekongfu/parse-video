"""Mini-program QR login sessions for the existing auth service."""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import time
import uuid

import httpx
import qrcode
from sqlalchemy import insert, or_, select, update

from parse_video_py.user_db import (
    SystemUser, _engine, init_user_database, qr_login_sessions, users,
)

QR_LOGIN_TTL = 180
WEB_SESSION_TTL_SECONDS = 24 * 60 * 60


def _key() -> bytes:
    value = os.getenv("WEB_SESSION_SIGNING_KEY", "").strip() or os.getenv(
        "OPENID_SIGNING_KEY", ""
    ).strip()
    if not value:
        raise RuntimeError("WEB_SESSION_SIGNING_KEY/OPENID_SIGNING_KEY 未配置")
    return value.encode()


def _hash(value: str) -> bytes:
    return hmac.new(_key(), value.encode(), hashlib.sha256).digest()


def _ticket_for_session(session_id: str) -> str:
    digest = hmac.new(
        _key(), f"qr-login-ticket:{session_id}".encode(), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _data_uri(image: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(image).decode()


def _mock_code(scene_token: str) -> str:
    payload = "mock-miniprogram://pages/web-login/index?scene=" + scene_token
    image = qrcode.make(payload)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return _data_uri(output.getvalue())


def _wechat_access_token(appid: str, secret: str) -> str:
    response = httpx.get(
        "https://api.weixin.qq.com/cgi-bin/token",
        params={"grant_type": "client_credential", "appid": appid, "secret": secret},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("access_token"):
        raise RuntimeError(data.get("errmsg") or "获取微信 access_token 失败")
    return str(data["access_token"])


def _real_code(scene_token: str, appid: str, secret: str) -> str:
    response = httpx.post(
        "https://api.weixin.qq.com/wxa/getwxacodeunlimit",
        params={"access_token": _wechat_access_token(appid, secret)},
        json={
            "scene": scene_token,
            "page": "pages/web-login/index",
            "check_path": False,
            "env_version": os.getenv("WX_MINIPROGRAM_ENV_VERSION", "release"),
        },
        timeout=15,
    )
    response.raise_for_status()
    if response.headers.get("content-type", "").startswith("application/json"):
        data = response.json()
        raise RuntimeError(data.get("errmsg") or "生成微信小程序码失败")
    return _data_uri(response.content)


def create_qr_login(appid: str, secret: str) -> dict[str, object]:
    init_user_database()
    scene_token = secrets.token_urlsafe(18)
    mock = os.getenv("QR_LOGIN_MOCK", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if mock:
        qr_code_url = _mock_code(scene_token)
    else:
        if not appid or not secret:
            raise RuntimeError("WX_APPID/WX_APPSECRET 未配置")
        qr_code_url = _real_code(scene_token, appid, secret)
    now = int(time.time())
    with _engine.begin() as conn:
        conn.execute(insert(qr_login_sessions).values(
            id=str(uuid.uuid4()), scene_token_hash=_hash(scene_token),
            login_ticket_hash=None, user_id=None, status="waiting",
            expires_at=now + QR_LOGIN_TTL, confirmed_at=None, consumed_at=None,
            created_at=now, updated_at=now,
        ))
    return {
        "qr_code_url": qr_code_url,
        "scene_token": scene_token,
        "expires_in": QR_LOGIN_TTL,
    }


def get_qr_status(scene_token: str) -> dict[str, str]:
    now = int(time.time())
    with _engine.begin() as conn:
        row = conn.execute(select(qr_login_sessions).where(
            qr_login_sessions.c.scene_token_hash == _hash(scene_token)
        )).mappings().first()
        if not row:
            raise ValueError("无效的 scene_token")
        if row["expires_at"] <= now:
            conn.execute(update(qr_login_sessions).where(
                qr_login_sessions.c.id == row["id"]
            ).values(status="expired", updated_at=now))
            return {"status": "expired"}
        if row["consumed_at"] is not None:
            return {"status": "expired"}
        if row["status"] != "confirmed":
            return {"status": str(row["status"])}
        ticket = _ticket_for_session(str(row["id"]))
        ticket_hash = _hash(ticket)
        if row["login_ticket_hash"] != ticket_hash:
            conn.execute(update(qr_login_sessions).where(
                qr_login_sessions.c.id == row["id"]
            ).values(login_ticket_hash=ticket_hash, updated_at=now))
        return {"status": "confirmed", "login_ticket": ticket}


def mark_qr_scanned(scene_token: str, user_id: str) -> None:
    """Bind the scan to one authenticated mini-program user, idempotently."""
    now = int(time.time())
    scene_hash = _hash(scene_token)
    with _engine.begin() as conn:
        result = conn.execute(update(qr_login_sessions).where(
            qr_login_sessions.c.scene_token_hash == scene_hash,
            qr_login_sessions.c.expires_at > now,
            qr_login_sessions.c.status == "waiting",
        ).values(user_id=user_id, status="scanned", updated_at=now))
        if result.rowcount == 1:
            return
        row = conn.execute(select(qr_login_sessions).where(
            qr_login_sessions.c.scene_token_hash == scene_hash
        )).mappings().first()
        if not row or row["expires_at"] <= now:
            raise ValueError("小程序码无效或已过期")
        if row["status"] == "scanned" and row["user_id"] == user_id:
            return
        if row["status"] == "scanned":
            raise ValueError("登录会话已由其他用户扫码")
        raise ValueError("登录会话已确认或取消")


def confirm_qr_login(scene_token: str, user_id: str) -> None:
    now = int(time.time())
    scene_hash = _hash(scene_token)
    with _engine.begin() as conn:
        result = conn.execute(update(qr_login_sessions).where(
            qr_login_sessions.c.scene_token_hash == scene_hash,
            qr_login_sessions.c.expires_at > now,
            or_(
                qr_login_sessions.c.status == "waiting",
                (
                    (qr_login_sessions.c.status == "scanned")
                    & (qr_login_sessions.c.user_id == user_id)
                ),
            ),
        ).values(
            user_id=user_id, status="confirmed", confirmed_at=now, updated_at=now,
        ))
        if result.rowcount != 1:
            row = conn.execute(select(qr_login_sessions).where(
                qr_login_sessions.c.scene_token_hash == scene_hash
            )).mappings().first()
            if row and row["status"] == "scanned" and row["user_id"] != user_id:
                raise ValueError("登录会话已由其他用户扫码，不能确认")
            if row and row["status"] == "cancelled":
                raise ValueError("登录会话已取消")
            raise ValueError("小程序码无效、已过期或已确认")


def cancel_qr_login(scene_token: str, user_id: str) -> None:
    """Cancel only the session scanned by the same mini-program user."""
    now = int(time.time())
    scene_hash = _hash(scene_token)
    with _engine.begin() as conn:
        result = conn.execute(update(qr_login_sessions).where(
            qr_login_sessions.c.scene_token_hash == scene_hash,
            qr_login_sessions.c.expires_at > now,
            qr_login_sessions.c.status == "scanned",
            qr_login_sessions.c.user_id == user_id,
        ).values(status="cancelled", updated_at=now))
        if result.rowcount == 1:
            return
        row = conn.execute(select(qr_login_sessions).where(
            qr_login_sessions.c.scene_token_hash == scene_hash
        )).mappings().first()
        if (
            row and row["expires_at"] > now and row["status"] == "cancelled"
            and row["user_id"] == user_id
        ):
            return
        if row and row["status"] == "scanned" and row["user_id"] != user_id:
            raise ValueError("不能取消其他用户的登录会话")
        raise ValueError("小程序码无效、已过期、已确认或已取消")


def exchange_login_ticket(login_ticket: str) -> SystemUser:
    now = int(time.time())
    with _engine.begin() as conn:
        row = conn.execute(select(qr_login_sessions).where(
            qr_login_sessions.c.login_ticket_hash == _hash(login_ticket)
        )).mappings().first()
        if not row or not row["user_id"] or row["expires_at"] <= now:
            raise ValueError("登录 ticket 无效或已过期")
        result = conn.execute(update(qr_login_sessions).where(
            qr_login_sessions.c.id == row["id"],
            qr_login_sessions.c.consumed_at.is_(None),
        ).values(consumed_at=now, updated_at=now))
        if result.rowcount != 1:
            raise ValueError("登录 ticket 已被使用")
        user_row = conn.execute(select(users).where(
            users.c.id == row["user_id"]
        )).mappings().one()
        return SystemUser(
            id=user_row["id"], internal_code=user_row["internal_code"],
            display_name=user_row["display_name"], avatar_url=user_row["avatar_url"],
        )


def create_web_session(user_id: str, ttl: int = WEB_SESSION_TTL_SECONDS) -> str:
    payload = {"uid": user_id, "exp": int(time.time()) + ttl}
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    signature = hmac.new(_key(), encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def verify_web_session(token: str) -> SystemUser:
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(_key(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("网页会话签名无效")
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        if int(payload["exp"]) <= int(time.time()):
            raise ValueError("网页会话已过期")
        user_id = str(payload["uid"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("网页会话无效") from exc
    init_user_database()
    with _engine.connect() as conn:
        row = conn.execute(select(users).where(users.c.id == user_id)).mappings().first()
    if not row:
        raise ValueError("网页用户不存在")
    return SystemUser(
        id=row["id"], internal_code=row["internal_code"],
        display_name=row["display_name"], avatar_url=row["avatar_url"],
    )
