import io
import json
from unittest.mock import AsyncMock, patch

from docx import Document
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select

import parse_video_py.document_summary as summary_service
import parse_video_py.document_summary_web as summary_web
import parse_video_py.document_translation as translation_service
import parse_video_py.document_translation_web as translation_web
import parse_video_py.user_db as user_db


def _docx_bytes() -> bytes:
    document = Document()
    document.add_heading("1. Service Agreement", level=1)
    document.add_paragraph("The total amount is USD 1,200. Visit https://example.com.")
    document.add_paragraph("Delivery date: 2026-08-01.")
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _translated_batch(segments, **_kwargs):
    return "en", [
        {
            "segment_id": segment["segment_id"],
            "translated_text": f"译文：{segment['source_text']}",
        }
        for segment in segments
    ]


def _build_app(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{(tmp_path / 'translation.db').as_posix()}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    monkeypatch.setattr(user_db, "_engine", engine)
    monkeypatch.setattr(summary_service, "_engine", engine)
    monkeypatch.setattr(translation_service, "_engine", engine)
    monkeypatch.setattr(summary_web, "UPLOAD_DIR", tmp_path / "uploads")
    user_db.init_user_database()
    user = user_db.get_or_create_user("translation-user-openid")
    app = FastAPI()
    app.include_router(summary_web.router)
    app.include_router(translation_web.router)
    return TestClient(app), user, engine


def _upload_and_parse(client):
    uploaded = client.post(
        "/auth/documents/upload",
        files={
            "file": (
                "agreement.docx",
                _docx_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        cookies={"web_session": "session"},
    )
    assert uploaded.status_code == 200
    document_id = uploaded.json()["document_id"]
    parsed = client.post(
        f"/auth/documents/{document_id}/parse",
        cookies={"web_session": "session"},
    )
    assert parsed.status_code == 200
    return document_id


def test_translation_cache_modes_glossary_and_exports(monkeypatch, tmp_path):
    client, user, engine = _build_app(monkeypatch, tmp_path)
    with patch.object(summary_web, "verify_web_session", return_value=user):
        document_id = _upload_and_parse(client)
    mocked_ai = AsyncMock(side_effect=_translated_batch)
    payload = {
        "source_language": "auto",
        "target_language": "zh-CN",
        "mode": "bilingual",
        "style": "legal",
        "glossary": [{"source": "Service Agreement", "target": "服务协议"}],
    }
    with (
        patch.object(summary_web, "verify_web_session", return_value=user),
        patch.object(translation_web, "_current_user", return_value=user),
        patch.object(translation_service, "call_deepseek_translation", mocked_ai),
    ):
        first = client.post(
            f"/auth/documents/{document_id}/translate",
            json=payload,
            cookies={"web_session": "session"},
        )
        second = client.post(
            f"/auth/documents/{document_id}/translate",
            json=payload,
            cookies={"web_session": "session"},
        )
        translation_id = first.json()["translation_id"]
        txt = client.get(
            f"/auth/documents/{document_id}/translations/{translation_id}/export?format=txt",
            cookies={"web_session": "session"},
        )
        docx = client.get(
            f"/auth/documents/{document_id}/translations/{translation_id}/export?format=docx",
            cookies={"web_session": "session"},
        )

    assert first.status_code == 200
    assert first.json()["cached"] is False
    assert first.json()["detected_source_language"] == "en"
    assert [item["segment_id"] for item in first.json()["segments"]] == [
        "seg-0001",
        "seg-0002",
        "seg-0003",
    ]
    assert second.json()["cached"] is True
    assert mocked_ai.await_count == 1
    assert txt.status_code == 200
    assert "原文：" in txt.content.decode("utf-8-sig")
    assert docx.status_code == 200
    assert docx.content.startswith(b"PK")
    with engine.connect() as conn:
        count = conn.execute(
            select(func.count())
            .select_from(user_db.document_translations)
            .where(user_db.document_translations.c.user_id == user.id)
        ).scalar_one()
    assert count == 1


def test_changed_options_create_separate_cached_result(monkeypatch, tmp_path):
    client, user, _ = _build_app(monkeypatch, tmp_path)
    with patch.object(summary_web, "verify_web_session", return_value=user):
        document_id = _upload_and_parse(client)
    mocked_ai = AsyncMock(side_effect=_translated_batch)
    with (
        patch.object(translation_web, "_current_user", return_value=user),
        patch.object(translation_service, "call_deepseek_translation", mocked_ai),
    ):
        general = client.post(
            f"/auth/documents/{document_id}/translate",
            json={"target_language": "zh-CN", "style": "general"},
            cookies={"web_session": "session"},
        )
        technical = client.post(
            f"/auth/documents/{document_id}/translate",
            json={"target_language": "zh-CN", "style": "technical"},
            cookies={"web_session": "session"},
        )
    assert general.status_code == technical.status_code == 200
    assert general.json()["translation_id"] != technical.json()["translation_id"]
    assert mocked_ai.await_count == 2


def test_translation_requires_parsed_owned_document(monkeypatch, tmp_path):
    client, user, _ = _build_app(monkeypatch, tmp_path)
    with patch.object(summary_web, "verify_web_session", return_value=user):
        uploaded = client.post(
            "/auth/documents/upload",
            files={"file": ("agreement.docx", _docx_bytes())},
            cookies={"web_session": "session"},
        )
    with patch.object(translation_web, "_current_user", return_value=user):
        unparsed = client.post(
            f"/auth/documents/{uploaded.json()['document_id']}/translate",
            json={"target_language": "zh-CN"},
            cookies={"web_session": "session"},
        )
    assert unparsed.status_code == 502
    assert unparsed.json()["detail"] == "请先完成文档解析"

    other = user_db.get_or_create_user("other-translation-user")
    with patch.object(translation_web, "_current_user", return_value=other):
        forbidden = client.post(
            f"/auth/documents/{uploaded.json()['document_id']}/translate",
            json={"target_language": "zh-CN"},
            cookies={"web_session": "other"},
        )
    assert forbidden.status_code == 404


def test_segmentation_and_batching_never_split_sentences():
    segments = translation_service.segment_document(
        "Heading\nFirst sentence. Second sentence.\nThird paragraph."
    )
    assert len(segments) == 3
    assert segments[1]["source_text"] == "First sentence. Second sentence."
    batches = list(translation_service.iter_segment_batches(segments, max_chars=10))
    assert [item for batch in batches for item in batch] == segments


async def test_deepseek_translation_uses_segment_json_and_glossary(monkeypatch):
    captured = {}
    segments = translation_service.segment_document(
        "Service Agreement\nThe total amount is USD 1,200."
    )

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "detected_source_language": "en",
                                    "translations": [
                                        {
                                            "segment_id": item["segment_id"],
                                            "translated_text": f"译文 {index}",
                                        }
                                        for index, item in enumerate(segments, 1)
                                    ],
                                }
                            )
                        }
                    }
                ]
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, headers, json):
            captured.update({"url": url, "headers": headers, "body": json})
            return FakeResponse()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    with patch.object(
        translation_service.httpx, "AsyncClient", return_value=FakeClient()
    ):
        detected, translated = await translation_service.call_deepseek_translation(
            segments,
            source_language="auto",
            target_language="zh-CN",
            style="business",
            glossary=[{"source": "Service Agreement", "target": "服务协议"}],
            user_id="user-id",
        )

    assert detected == "en"
    assert [item["segment_id"] for item in translated] == ["seg-0001", "seg-0002"]
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    prompt = captured["body"]["messages"][1]["content"]
    assert "Service Agreement" in prompt
    assert "服务协议" in prompt
    assert "USD 1,200" in prompt
