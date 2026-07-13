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
    user_db.init_user_database()
    return engine


def test_qr_login_service_full_flow_and_one_time_ticket(isolated_qr_db):
    created = qr_auth.create_qr_login("", "")
    assert created["expires_in"] == 180
    assert str(created["qr_code_url"]).startswith("data:image/png;base64,")
    scene = str(created["scene_token"])
    assert qr_auth.get_qr_status(scene) == {"status": "waiting"}

    user = user_db.get_or_create_user("qr-openid")
    qr_auth.confirm_qr_login(scene, user.id)
    status_result = qr_auth.get_qr_status(scene)
    assert status_result["status"] == "confirmed"
    ticket = status_result["login_ticket"]
    exchanged = qr_auth.exchange_login_ticket(ticket)
    assert exchanged.id == user.id
    with pytest.raises(ValueError, match="已被使用"):
        qr_auth.exchange_login_ticket(ticket)

    web_session = qr_auth.create_web_session(user.id)
    assert qr_auth.verify_web_session(web_session).id == user.id


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
         patch.object(auth_web, "confirm_qr_login") as confirm:
        response = client.post(
            "/auth/qr/confirm", json={"scene_token": "scene"},
            headers={"Authorization": "Bearer existing-openid-token"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "confirmed"
        confirm.assert_called_once_with("scene", "user-id")

    with patch.object(auth_web, "exchange_login_ticket", return_value=user), \
         patch.object(auth_web, "create_web_session", return_value="signed-session"):
        response = client.post("/auth/qr/exchange", json={"login_ticket": "ticket"})
        assert response.status_code == 200
        assert response.cookies.get("web_session") == "signed-session"

    with patch.object(auth_web, "verify_web_session", return_value=user):
        response = client.get("/auth/me", cookies={"web_session": "signed-session"})
        assert response.status_code == 200
        assert response.json()["user"]["internal_code"] == "ABC123"
