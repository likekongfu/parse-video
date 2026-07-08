from io import BytesIO
from pathlib import Path
import shutil
import tempfile

import pytest
from fastapi.testclient import TestClient

import parse_video_py.document_convert_web as dw


@pytest.fixture()
def document_client(monkeypatch):
    original_output_dir = dw.OUTPUT_DIR
    original_public_base_url = dw.PUBLIC_BASE_URL
    original_api_token = dw.API_TOKEN

    tmpdir = tempfile.mkdtemp(prefix="doc_pdf_tools_")
    output_dir = Path(tmpdir) / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(dw, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(dw, "PUBLIC_BASE_URL", "https://example.test/document")
    monkeypatch.setattr(dw, "API_TOKEN", "")

    client = TestClient(dw.app)
    try:
        yield client
    finally:
        monkeypatch.setattr(dw, "OUTPUT_DIR", original_output_dir)
        monkeypatch.setattr(dw, "PUBLIC_BASE_URL", original_public_base_url)
        monkeypatch.setattr(dw, "API_TOKEN", original_api_token)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_pdf_compress_reuses_output_download_flow(document_client, monkeypatch):
    def fake_run_pdf_compress(input_path, output_path, level):
        assert input_path.exists()
        assert level == "heavy"
        output_path.write_bytes(b"%PDF-1.7 compressed")
        return output_path

    monkeypatch.setattr(dw, "run_pdf_compress", fake_run_pdf_compress)

    response = document_client.post(
        "/document/pdf-compress",
        files={
            "file": ("demo.pdf", BytesIO(b"%PDF-1.7 original bytes"), "application/pdf")
        },
        data={"level": "heavy"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["fileName"] == "demo_compressed_heavy.pdf"
    assert data["level"] == "heavy"
    assert data["compressedSize"] == len(b"%PDF-1.7 compressed")
    assert data["downloadUrl"].startswith("https://example.test/document/files/")

    download_name = data["downloadUrl"].rsplit("/", 1)[-1]
    download = document_client.get(f"/files/{download_name}")
    assert download.status_code == 200
    assert download.content == b"%PDF-1.7 compressed"


def test_pdf_compress_rejects_unknown_level(document_client):
    response = document_client.post(
        "/document/pdf-compress",
        files={"file": ("demo.pdf", BytesIO(b"%PDF-1.7"), "application/pdf")},
        data={"level": "tiny"},
    )

    assert response.status_code == 400
    assert "Unsupported compression level" in response.json()["detail"]


def test_pdf_encrypt_reuses_output_download_flow(document_client, monkeypatch):
    calls = {}

    def fake_run_pdf_encrypt(
        input_path,
        output_path,
        password,
        allow_print=True,
        allow_copy=False,
        allow_modify=False,
    ):
        calls.update(
            {
                "exists": input_path.exists(),
                "password": password,
                "allow_print": allow_print,
                "allow_copy": allow_copy,
                "allow_modify": allow_modify,
            }
        )
        output_path.write_bytes(b"%PDF-1.7 encrypted")
        return output_path

    monkeypatch.setattr(dw, "run_pdf_encrypt", fake_run_pdf_encrypt)

    response = document_client.post(
        "/document/pdf-encrypt",
        files={
            "file": ("secret.pdf", BytesIO(b"%PDF-1.7 original"), "application/pdf")
        },
        data={
            "password": "secret123",
            "allow_print": "false",
            "allow_copy": "true",
            "allow_modify": "false",
        },
    )

    assert response.status_code == 200
    assert calls == {
        "exists": True,
        "password": "secret123",
        "allow_print": False,
        "allow_copy": True,
        "allow_modify": False,
    }
    body = response.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["fileName"] == "secret_encrypted.pdf"
    assert data["allowPrint"] is False
    assert data["allowCopy"] is True
    assert data["allowModify"] is False


def test_pdf_encrypt_rejects_short_password(document_client):
    response = document_client.post(
        "/document/pdf-encrypt",
        files={"file": ("secret.pdf", BytesIO(b"%PDF-1.7"), "application/pdf")},
        data={"password": "123"},
    )

    assert response.status_code == 400
    assert "Password must be at least" in response.json()["detail"]
