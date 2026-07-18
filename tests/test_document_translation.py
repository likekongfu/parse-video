import io
import json
from unittest.mock import AsyncMock, patch

import pytest
import fitz
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


def _styled_docx_bytes() -> bytes:
    document = Document()
    document.add_heading("1. Service Agreement", level=1)
    paragraph = document.add_paragraph(
        "The total amount is USD 1,200. Visit https://example.com."
    )
    paragraph.style = "Intense Quote"
    table = document.add_table(rows=1, cols=2)
    table.style = "Table Grid"
    table.rows[0].cells[0].text = "Item"
    table.rows[0].cells[1].text = "Amount"
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _styled_pdf_bytes() -> bytes:
    document = fitz.open()
    page = document.new_page(width=420, height=300)
    page.draw_rect(fitz.Rect(24, 24, 396, 276), color=(0.1, 0.3, 0.8), width=2)
    page.insert_textbox(
        fitz.Rect(50, 55, 370, 90),
        "Service Agreement",
        fontsize=18,
        fontname="helv",
        align=fitz.TEXT_ALIGN_CENTER,
        color=(0.1, 0.2, 0.5),
    )
    page.insert_textbox(
        fitz.Rect(55, 120, 365, 175),
        "The total amount is USD 1,200.",
        fontsize=11,
        fontname="helv",
    )
    payload = document.tobytes(deflate=True)
    document.close()
    return payload


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
    return _upload_bytes_and_parse(client, _docx_bytes(), "agreement.docx")


def _upload_bytes_and_parse(client, content: bytes, filename: str):
    content_type = (
        "application/pdf"
        if filename.lower().endswith(".pdf")
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    uploaded = client.post(
        "/auth/documents/upload",
        files={
            "file": (
                filename,
                content,
                content_type,
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


def test_docx_translation_export_preserves_original_layout(monkeypatch, tmp_path):
    client, user, _ = _build_app(monkeypatch, tmp_path)
    with patch.object(summary_web, "verify_web_session", return_value=user):
        document_id = _upload_bytes_and_parse(
            client, _styled_docx_bytes(), "styled-agreement.docx"
        )
    mocked_ai = AsyncMock(side_effect=_translated_batch)
    with (
        patch.object(summary_web, "verify_web_session", return_value=user),
        patch.object(translation_web, "_current_user", return_value=user),
        patch.object(translation_service, "call_deepseek_translation", mocked_ai),
    ):
        translated = client.post(
            f"/auth/documents/{document_id}/translate",
            json={"target_language": "zh-CN", "mode": "translation"},
            cookies={"web_session": "session"},
        )
        translation_id = translated.json()["translation_id"]
        exported = client.get(
            f"/auth/documents/{document_id}/translations/{translation_id}/export?format=docx",
            cookies={"web_session": "session"},
        )

    assert exported.status_code == 200
    output = Document(io.BytesIO(exported.content))
    assert len(output.tables) == 1
    assert output.paragraphs[0].style.name == "Heading 1"
    assert output.paragraphs[0].text == "译文：1. Service Agreement"
    assert output.paragraphs[1].style.name == "Intense Quote"
    assert output.paragraphs[1].text.startswith("译文：The total amount")
    assert output.tables[0].style.name == "Table Grid"
    assert output.tables[0].rows[0].cells[0].text == "译文：Item"
    assert output.tables[0].rows[0].cells[1].text == "Amount"


def test_pdf_translation_export_preserves_original_page_layout(monkeypatch, tmp_path):
    client, user, _ = _build_app(monkeypatch, tmp_path)
    source_bytes = _styled_pdf_bytes()
    with patch.object(summary_web, "verify_web_session", return_value=user):
        document_id = _upload_bytes_and_parse(client, source_bytes, "agreement.pdf")

    async def translated_pdf_batch(segments, **_kwargs):
        translations = ["服务协议", "总金额为 1,200 美元"]
        assert len(segments) == len(translations)
        return "en", [
            {
                "segment_id": segment["segment_id"],
                "translated_text": translated_text,
            }
            for segment, translated_text in zip(segments, translations, strict=False)
        ]

    with (
        patch.object(summary_web, "verify_web_session", return_value=user),
        patch.object(translation_web, "_current_user", return_value=user),
        patch.object(
            translation_service,
            "call_deepseek_translation",
            side_effect=translated_pdf_batch,
        ),
    ):
        translated = client.post(
            f"/auth/documents/{document_id}/translate",
            json={"target_language": "zh-CN", "mode": "translation"},
            cookies={"web_session": "session"},
        )
        assert translated.status_code == 200
        assert [item["segment_id"] for item in translated.json()["segments"]] == [
            "p0001-b0001",
            "p0001-b0002",
        ]
        translation_id = translated.json()["translation_id"]
        exported = client.get(
            f"/auth/documents/{document_id}/translations/{translation_id}/export?format=pdf",
            cookies={"web_session": "session"},
        )

    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/pdf")
    with fitz.open(stream=source_bytes, filetype="pdf") as source_document:
        source_page = source_document[0]
        source_size = (source_page.rect.width, source_page.rect.height)
        source_drawing_count = len(source_page.get_drawings())
    with fitz.open(stream=exported.content, filetype="pdf") as output_document:
        assert output_document.page_count == 1
        output_page = output_document[0]
        assert (output_page.rect.width, output_page.rect.height) == source_size
        assert len(output_page.get_drawings()) >= source_drawing_count
        output_text = output_page.get_text("text")
        assert "Service Agreement" not in output_text
        assert "The total amount is USD 1,200." not in output_text
        assert "服务协议" in output_text
        assert "总金额为 1,200 美元" in output_text


def test_original_layout_pdf_export_rejects_bilingual_mode(monkeypatch, tmp_path):
    client, user, _ = _build_app(monkeypatch, tmp_path)
    with patch.object(summary_web, "verify_web_session", return_value=user):
        document_id = _upload_bytes_and_parse(
            client, _styled_pdf_bytes(), "agreement.pdf"
        )
    mocked_ai = AsyncMock(side_effect=_translated_batch)
    with (
        patch.object(translation_web, "_current_user", return_value=user),
        patch.object(translation_service, "call_deepseek_translation", mocked_ai),
    ):
        translated = client.post(
            f"/auth/documents/{document_id}/translate",
            json={"target_language": "zh-CN", "mode": "bilingual"},
            cookies={"web_session": "session"},
        )
        translation_id = translated.json()["translation_id"]
        exported = client.get(
            f"/auth/documents/{document_id}/translations/{translation_id}/export?format=pdf",
            cookies={"web_session": "session"},
        )

    assert exported.status_code == 502
    assert exported.json()["detail"] == "原版式 PDF 暂只支持纯译文模式"


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


def test_segmentation_and_batching_uses_configured_segment_count():
    segments = translation_service.segment_document(
        "Heading\nFirst sentence. Second sentence.\nThird paragraph."
    )
    assert len(segments) == 3
    assert segments[1]["source_text"] == "First sentence. Second sentence."
    batches = list(translation_service.iter_segment_batches(segments, batch_size=2))
    assert [len(batch) for batch in batches] == [2, 1]
    assert [item for batch in batches for item in batch] == segments

    many_segments = translation_service.segment_document(
        "\n".join(f"Paragraph {index}." for index in range(45))
    )
    default_batches = list(translation_service.iter_segment_batches(many_segments))
    assert [len(batch) for batch in default_batches] == [20, 20, 5]


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

    assert detected is None
    assert [item["segment_id"] for item in translated] == ["seg-0001", "seg-0002"]
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    prompt = captured["body"]["messages"][1]["content"]
    assert "Service Agreement" in prompt
    assert "服务协议" in prompt
    assert "USD 1,200" in prompt
    assert "detected_source_language" not in prompt


async def test_incomplete_translation_retries_current_batch():
    segments = translation_service.segment_document(
        "Heading\nFirst paragraph.\nSecond paragraph."
    )
    requested_ids = []

    async def incomplete_once(requested, **_kwargs):
        requested_ids.append([item["segment_id"] for item in requested])
        if len(requested_ids) == 1:
            payload = {
                "translations": [
                    {
                        "segment_id": requested[0]["segment_id"],
                        "translated_text": "标题",
                    },
                    {
                        "segment_id": requested[1]["segment_id"],
                        "translated_text": "第一段",
                    },
                ],
            }
            return translation_service._normalize_ai_result(payload, requested)
        return None, [
            {
                "segment_id": item["segment_id"],
                "translated_text": f"译文 {item['segment_id']}",
            }
            for item in requested
        ]

    with patch.object(
        translation_service,
        "call_deepseek_translation",
        side_effect=incomplete_once,
    ):
        detected, translated = await translation_service.translate_batch_with_recovery(
            segments,
            source_language="auto",
            target_language="zh-CN",
            style="general",
            glossary=[],
            user_id="user-id",
        )

    assert detected is None
    assert requested_ids == [
        ["seg-0001", "seg-0002", "seg-0003"],
        ["seg-0001", "seg-0002", "seg-0003"],
    ]
    assert [item["translated_text"] for item in translated] == [
        "译文 seg-0001",
        "译文 seg-0002",
        "译文 seg-0003",
    ]


async def test_invalid_translation_response_splits_batch_and_retries():
    segments = translation_service.segment_document("Heading\nFirst paragraph.")
    requested_sizes = []

    async def invalid_batch(requested, **_kwargs):
        requested_sizes.append(len(requested))
        if len(requested) > 1:
            raise translation_service.InvalidTranslationResponseError(
                "AI 返回的翻译格式无效"
            )
        return "en", [
            {
                "segment_id": requested[0]["segment_id"],
                "translated_text": f"译文 {requested[0]['segment_id']}",
            }
        ]

    with patch.object(
        translation_service,
        "call_deepseek_translation",
        side_effect=invalid_batch,
    ):
        _, translated = await translation_service.translate_batch_with_recovery(
            segments,
            source_language="auto",
            target_language="zh-CN",
            style="general",
            glossary=[],
            user_id="user-id",
        )

    assert requested_sizes == [2, 2, 2, 1, 1]
    assert [item["segment_id"] for item in translated] == ["seg-0001", "seg-0002"]


async def test_single_incomplete_segment_has_bounded_retries():
    segments = translation_service.segment_document("Only one paragraph.")
    mocked_ai = AsyncMock(
        side_effect=translation_service.IncompleteTranslationResponseError([], "en")
    )
    with (
        patch.object(translation_service, "call_deepseek_translation", mocked_ai),
        pytest.raises(
            translation_service.DocumentTranslationError,
            match="seg-0001",
        ),
    ):
        await translation_service.translate_batch_with_recovery(
            segments,
            source_language="auto",
            target_language="zh-CN",
            style="general",
            glossary=[],
            user_id="user-id",
        )

    assert mocked_ai.await_count == translation_service.TRANSLATION_BATCH_RETRIES + 1


async def test_length_finish_reason_is_retried_then_split_in_original_order():
    segments = translation_service.segment_document("Heading\nFirst.\nSecond.")
    requested_ids = []

    async def truncated_batch(requested, **_kwargs):
        requested_ids.append([item["segment_id"] for item in requested])
        if len(requested) > 1:
            raise translation_service.TruncatedTranslationResponseError(
                "AI 翻译输出被截断", finish_reason="length"
            )
        return None, [
            {
                "segment_id": requested[0]["segment_id"],
                "translated_text": f"译文 {requested[0]['segment_id']}",
            }
        ]

    with patch.object(
        translation_service,
        "call_deepseek_translation",
        side_effect=truncated_batch,
    ):
        _, translated = await translation_service.translate_batch_with_recovery(
            segments,
            source_language="auto",
            target_language="zh-CN",
            style="general",
            glossary=[],
            user_id="user-id",
            request_id="request-id",
            batch_index=1,
        )

    assert requested_ids[:3] == [["seg-0001", "seg-0002", "seg-0003"]] * 3
    assert [item["segment_id"] for item in translated] == [
        "seg-0001",
        "seg-0002",
        "seg-0003",
    ]
