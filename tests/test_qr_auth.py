import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

os.environ.setdefault("WX_APPID", "wx_test_default")
os.environ.setdefault("WX_APPSECRET", "secret_test_default")

import parse_video_py.auth_web as auth_web
import parse_video_py.qr_auth as qr_auth
import parse_video_py.user_db as user_db


@pytest.fixture()
def isolated_qr_db(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{(tmp_path / 'qr.db').as_posix()}",
        connect_args={"check_same_thread": False}, future=True,
    )
    monkeypatch.setattr(user_db, "_engine", engine)
    monkeypatch.setattr(qr_auth, "_engine", engine)
    monkeypatch.setenv("QR_LOGIN_MOCK", "true")
    monkeypatch.setenv("WEB_SESSION_SIGNING_KEY", "test-web-session-key")
    monkeypatch.setenv("WEB_COOKIE_SECURE", "false")
    user_db.init_user_database()
    return engine


def test_qr_login_service_full_flow_and_one_time_ticket(isolated_qr_db):
    created = qr_auth.create_qr_login("", "")
    assert created["expires_in"] == 180
    assert str(created["qr_code_url"]).startswith("data:image/png;base64,")
    scene = str(created["scene_token"])
    assert qr_auth.get_qr_status(scene) == {"status": "waiting"}

    user = user_db.get_or_create_user("qr-openid")
    assert user_db.get_wechat_openid_for_user(user.id) == "qr-openid"
    qr_auth.confirm_qr_login(scene, user.id)
    status_result = qr_auth.get_qr_status(scene)
    assert status_result["status"] == "confirmed"
    ticket = status_result["login_ticket"]
    repeated_status = qr_auth.get_qr_status(scene)
    assert repeated_status == {"status": "confirmed", "login_ticket": ticket}
    exchanged = qr_auth.exchange_login_ticket(ticket)
    assert exchanged.id == user.id
    with pytest.raises(ValueError, match="已被使用"):
        qr_auth.exchange_login_ticket(ticket)

    web_session = qr_auth.create_web_session(user.id)
    assert qr_auth.verify_web_session(web_session).id == user.id


def test_expired_code_can_be_replaced_with_a_fresh_code(isolated_qr_db, monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(qr_auth.time, "time", lambda: now)
    expired = qr_auth.create_qr_login("", "")
    monkeypatch.setattr(qr_auth.time, "time", lambda: now + 181)
    assert qr_auth.get_qr_status(str(expired["scene_token"])) == {"status": "expired"}

    refreshed = qr_auth.create_qr_login("", "")
    assert refreshed["scene_token"] != expired["scene_token"]
    assert qr_auth.get_qr_status(str(refreshed["scene_token"])) == {"status": "waiting"}


def test_scan_is_idempotent_and_only_scanning_user_can_confirm(isolated_qr_db):
    scene = str(qr_auth.create_qr_login("", "")["scene_token"])
    owner = user_db.get_or_create_user("scan-owner-openid")
    another = user_db.get_or_create_user("another-openid")

    qr_auth.mark_qr_scanned(scene, owner.id)
    qr_auth.mark_qr_scanned(scene, owner.id)
    assert qr_auth.get_qr_status(scene) == {"status": "scanned"}
    with pytest.raises(ValueError, match="其他用户"):
        qr_auth.confirm_qr_login(scene, another.id)

    qr_auth.confirm_qr_login(scene, owner.id)
    with pytest.raises(ValueError, match="已确认"):
        qr_auth.confirm_qr_login(scene, owner.id)


def test_cancelled_scan_is_visible_to_web_and_cannot_be_confirmed(isolated_qr_db):
    scene = str(qr_auth.create_qr_login("", "")["scene_token"])
    owner = user_db.get_or_create_user("cancel-owner-openid")
    another = user_db.get_or_create_user("cancel-other-openid")

    qr_auth.mark_qr_scanned(scene, owner.id)
    with pytest.raises(ValueError, match="其他用户"):
        qr_auth.cancel_qr_login(scene, another.id)
    qr_auth.cancel_qr_login(scene, owner.id)
    qr_auth.cancel_qr_login(scene, owner.id)
    assert qr_auth.get_qr_status(scene) == {"status": "cancelled"}
    with pytest.raises(ValueError, match="已取消"):
        qr_auth.confirm_qr_login(scene, owner.id)


def test_qr_routes_keep_expected_contract():
    client = TestClient(auth_web.app)
    created = {"qr_code_url": "data:image/png;base64,abc", "scene_token": "scene", "expires_in": 180}
    with patch.object(auth_web, "create_qr_login", return_value=created):
        assert client.post("/auth/qr/create").json() == created
    with patch.object(auth_web, "get_qr_status", return_value={"status": "waiting"}):
        assert client.get("/auth/qr/status/scene").json() == {"status": "waiting"}

    user = SimpleNamespace(id="user-id", internal_code="ABC123", display_name=None, avatar_url=None)
    with patch.object(auth_web, "verify_openid_token", return_value="openid"), \
         patch.object(auth_web, "get_or_create_user", return_value=user), \
         patch.object(auth_web, "mark_qr_scanned") as scan, \
         patch.object(auth_web, "cancel_qr_login") as cancel, \
         patch.object(auth_web, "confirm_qr_login") as confirm:
        response = client.post(
            "/auth/qr/scan", json={"scene_token": "scene"},
            headers={"Authorization": "Bearer existing-openid-token"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "scanned"
        scan.assert_called_once_with("scene", "user-id")

        response = client.post(
            "/auth/qr/confirm", json={"scene_token": "scene"},
            headers={"Authorization": "Bearer existing-openid-token"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "confirmed"
        confirm.assert_called_once_with("scene", "user-id")

        response = client.post(
            "/auth/qr/cancel", json={"scene_token": "scene"},
            headers={"Authorization": "Bearer existing-openid-token"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "cancelled"
        cancel.assert_called_once_with("scene", "user-id")

    with patch.object(auth_web, "exchange_login_ticket", return_value=user), \
         patch.object(auth_web, "create_web_session", return_value="signed-session"), \
         patch.object(auth_web, "get_wechat_openid_for_user", return_value="openid"), \
         patch.object(auth_web, "create_openid_token", return_value="signed-openid-token"):
        response = client.post("/auth/qr/exchange", json={"login_ticket": "ticket"})
        assert response.status_code == 200
        assert response.cookies.get("web_session") == "signed-session"
        assert response.json() == {
            "status": "ok",
            "openidToken": "signed-openid-token",
            "expiresIn": auth_web.OPENID_TOKEN_TTL,
        }

    with patch.object(auth_web, "verify_web_session", return_value=user):
        first_refresh = client.get("/auth/me")
        second_refresh = client.get("/auth/me")
        assert first_refresh.status_code == 200
        assert second_refresh.status_code == 200
        assert second_refresh.json()["user"]["internal_code"] == "ABC123"

    response = client.post("/auth/logout")
    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert client.get("/auth/me").status_code == 401


def test_qr_actions_reject_missing_or_expired_miniprogram_token():
    client = TestClient(auth_web.app)
    assert client.post("/auth/qr/scan", json={"scene_token": "scene"}).status_code == 401
    with patch.object(auth_web, "verify_openid_token", side_effect=ValueError("expired")):
        response = client.post(
            "/auth/qr/confirm", json={"scene_token": "scene"},
            headers={"Authorization": "Bearer expired-token"},
        )
    assert response.status_code == 401
