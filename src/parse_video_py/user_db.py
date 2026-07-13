"""Unified user persistence for mini-program and web authentication.

Uses the same MySQL environment variables as ``web.py``. Local development
falls back to the existing MATERIAL_DB_PATH SQLite database.
"""
from __future__ import annotations

import os
import secrets
import string
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from sqlalchemy import (
    BigInteger, Column, ForeignKey, Index, LargeBinary, MetaData, String, Table,
    UniqueConstraint, create_engine, insert, select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.mysql import VARBINARY

_metadata = MetaData()
_hash_binary_type = LargeBinary(32).with_variant(VARBINARY(32), "mysql")

users = Table(
    "users", _metadata,
    Column("id", String(36), primary_key=True),
    Column("internal_code", String(6), nullable=False, unique=True),
    Column("display_name", String(120), nullable=True),
    Column("avatar_url", String(1024), nullable=True),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
)

user_identities = Table(
    "user_identities", _metadata,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("provider", String(32), nullable=False),
    Column("provider_user_id", String(191), nullable=False),
    Column("unionid", String(191), nullable=True),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    UniqueConstraint("provider", "provider_user_id", name="uq_user_identity_provider_user"),
    Index("ix_user_identities_unionid", "unionid"),
    Index("ix_user_identities_user_id", "user_id"),
)

qr_login_sessions = Table(
    "qr_login_sessions", _metadata,
    Column("id", String(36), primary_key=True),
    Column("scene_token_hash", _hash_binary_type, nullable=False, unique=True),
    Column("login_ticket_hash", _hash_binary_type, nullable=True, unique=True),
    Column("user_id", String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    Column("status", String(20), nullable=False),
    Column("expires_at", BigInteger, nullable=False),
    Column("confirmed_at", BigInteger, nullable=True),
    Column("consumed_at", BigInteger, nullable=True),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    Index("ix_qr_login_sessions_status_expires", "status", "expires_at"),
)


@dataclass(frozen=True)
class SystemUser:
    id: str
    internal_code: str
    display_name: str | None = None
    avatar_url: str | None = None


def build_database_url() -> str:
    host = os.getenv("MYSQL_HOST", "").strip()
    user = os.getenv("MYSQL_USER", "").strip()
    database = os.getenv("MYSQL_DATABASE", "").strip()
    if host and user and database:
        password = quote_plus(os.getenv("MYSQL_PASSWORD", ""))
        return (
            f"mysql+pymysql://{quote_plus(user)}:{password}@{host}:"
            f"{int(os.getenv('MYSQL_PORT', '3306'))}/{quote_plus(database)}"
            f"?charset={os.getenv('MYSQL_CHARSET', 'utf8mb4')}"
        )
    path = Path(os.getenv("MATERIAL_DB_PATH", "data/materials.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path.as_posix()}"


_engine = create_engine(build_database_url(), pool_pre_ping=True, future=True)
_DISPLAY_CODE_ALPHABET = string.ascii_uppercase + string.digits


def init_user_database() -> None:
    _metadata.create_all(_engine)


def _new_internal_code() -> str:
    return "".join(secrets.choice(_DISPLAY_CODE_ALPHABET) for _ in range(6))


def _row_to_user(row) -> SystemUser:
    return SystemUser(
        id=row["id"], internal_code=row["internal_code"],
        display_name=row["display_name"], avatar_url=row["avatar_url"],
    )


def get_or_create_user(openid: str, unionid: str | None = None) -> SystemUser:
    """Resolve an OpenID to one stable system user, safely under concurrency."""
    openid = (openid or "").strip()
    unionid = (unionid or "").strip() or None
    if not openid:
        raise ValueError("openid cannot be empty")
    init_user_database()

    for _ in range(12):
        try:
            with _engine.begin() as conn:
                existing = conn.execute(
                    select(users).join(user_identities).where(
                        user_identities.c.provider == "wechat_miniprogram",
                        user_identities.c.provider_user_id == openid,
                    )
                ).mappings().first()
                if existing:
                    return _row_to_user(existing)

                linked_user_id = None
                if unionid:
                    linked_user_id = conn.execute(
                        select(user_identities.c.user_id).where(
                            user_identities.c.unionid == unionid
                        ).limit(1)
                    ).scalar_one_or_none()

                now = int(time.time())
                user_id = linked_user_id or str(uuid.uuid4())
                if not linked_user_id:
                    conn.execute(insert(users).values(
                        id=user_id, internal_code=_new_internal_code(),
                        created_at=now, updated_at=now,
                    ))
                conn.execute(insert(user_identities).values(
                    id=str(uuid.uuid4()), user_id=user_id,
                    provider="wechat_miniprogram", provider_user_id=openid,
                    unionid=unionid, created_at=now, updated_at=now,
                ))
                row = conn.execute(select(users).where(users.c.id == user_id)).mappings().one()
                return _row_to_user(row)
        except IntegrityError:
            # Another request may have inserted the same OpenID or internal code.
            # Retry and resolve the winner through the unique indexes.
            continue
    raise RuntimeError("unable to create a unique user after concurrent retries")
