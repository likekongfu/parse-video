"""Persistence service for authenticated file processing history."""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import insert, select

from parse_video_py import user_db


def create_processing_history(
    *,
    user_id: str,
    source_filename: str,
    source_file_size: int,
    category: str,
    tool_type: str,
    tool_label: str,
    status: str,
    output_filename: str | None = None,
    output_url: str | None = None,
    output_expires_at: int | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    user_db.init_user_database()
    now = int(time.time())
    record = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "source_filename": source_filename,
        "source_file_size": source_file_size,
        "category": category,
        "tool_type": tool_type,
        "tool_label": tool_label,
        "status": status,
        "output_filename": output_filename,
        "output_url": output_url,
        "output_expires_at": output_expires_at,
        "error_message": error_message,
        "created_at": now,
        "completed_at": now if status in {"completed", "failed"} else None,
    }
    with user_db._engine.begin() as conn:
        conn.execute(insert(user_db.file_processing_history).values(**record))
    return _public_record(record)


def list_processing_history(user_id: str, limit: int = 30) -> list[dict[str, Any]]:
    user_db.init_user_database()
    with user_db._engine.connect() as conn:
        rows = (
            conn.execute(
                select(user_db.file_processing_history)
                .where(user_db.file_processing_history.c.user_id == user_id)
                .order_by(user_db.file_processing_history.c.created_at.desc())
                .limit(limit)
            )
            .mappings()
            .all()
        )
    return [_public_record(dict(row)) for row in rows]


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "user_id"}
