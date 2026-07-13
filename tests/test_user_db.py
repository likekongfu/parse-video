import concurrent.futures
import re

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

import parse_video_py.user_db as user_db


def test_qr_hash_columns_use_indexable_mysql_type():
    ddl = str(CreateTable(user_db.qr_login_sessions).compile(dialect=mysql.dialect()))
    assert "scene_token_hash VARBINARY(32)" in ddl
    assert "login_ticket_hash VARBINARY(32)" in ddl

    document_ddl = str(CreateTable(user_db.documents).compile(dialect=mysql.dialect()))
    assert "LONGTEXT" in document_ddl
    assert "uq_documents_user_content_hash" in document_ddl


@pytest.fixture()
def isolated_user_db(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{(tmp_path / 'users.db').as_posix()}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    monkeypatch.setattr(user_db, "_engine", engine)
    user_db.init_user_database()
    return engine


def test_get_or_create_user_is_stable(isolated_user_db):
    first = user_db.get_or_create_user("openid-1", "union-1")
    second = user_db.get_or_create_user("openid-1", "union-1")
    assert first.id == second.id
    assert first.internal_code == second.internal_code
    assert re.fullmatch(r"[A-Z0-9]{6}", first.internal_code)


def test_unionid_links_new_openid_to_existing_user(isolated_user_db):
    first = user_db.get_or_create_user("openid-app-a", "same-union")
    second = user_db.get_or_create_user("openid-app-b", "same-union")
    assert first.id == second.id


def test_same_openid_concurrent_calls_create_one_identity(isolated_user_db):
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: user_db.get_or_create_user("race-openid"), range(16)))
    assert len({item.id for item in results}) == 1
    with isolated_user_db.connect() as conn:
        identity_count = conn.execute(
            select(func.count()).select_from(user_db.user_identities).where(
                user_db.user_identities.c.provider == "wechat_miniprogram",
                user_db.user_identities.c.provider_user_id == "race-openid",
            )
        ).scalar_one()
    assert identity_count == 1
