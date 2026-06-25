from fastapi.testclient import TestClient

from parse_video_py.parser.base import ImgInfo, VideoInfo
from parse_video_py.web import app

client = TestClient(app)


def test_share_url_parse_returns_400_when_no_url_found():
    response = client.get("/video/share/url/parse", params={"url": "not a link"})

    assert response.status_code == 200
    assert response.json() == {"code": 400, "msg": "未检测到有效的分享链接"}


def test_share_url_parse_returns_400_for_empty_string():
    response = client.get("/video/share/url/parse", params={"url": ""})

    assert response.status_code == 200
    assert response.json() == {"code": 400, "msg": "未检测到有效的分享链接"}


def test_share_url_parse_returns_400_for_partial_url_without_scheme():
    response = client.get(
        "/video/share/url/parse", params={"url": "example.com/video/123"}
    )

    assert response.status_code == 200
    assert response.json() == {"code": 400, "msg": "未检测到有效的分享链接"}


def test_share_url_parse_returns_422_when_url_param_missing():
    response = client.get("/video/share/url/parse")

    assert response.status_code == 422


def test_health_check_returns_ok():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_share_url_parse_returns_material_id(monkeypatch):
    async def mock_parse_video_share_url(url):
        return VideoInfo(
            video_url="https://example.com/video.mp4",
            cover_url="https://example.com/cover.jpg",
            title="demo",
        )

    monkeypatch.setattr(
        "parse_video_py.web.parse_video_share_url", mock_parse_video_share_url
    )

    response = client.get(
        "/video/share/url/parse", params={"url": "https://v.douyin.com/demo"}
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["id"].startswith("mat_")
    assert data["material_id"] == data["id"]
    assert data["video_url"] == "https://example.com/video.mp4"


def test_material_download_streams_video(monkeypatch):
    async def mock_parse_video_share_url(url):
        return VideoInfo(
            video_url="https://example.com/video.mp4",
            cover_url="https://example.com/cover.jpg",
        )

    monkeypatch.setattr(
        "parse_video_py.web.parse_video_share_url", mock_parse_video_share_url
    )

    parse_response = client.get(
        "/video/share/url/parse", params={"url": "https://v.douyin.com/demo"}
    )
    material_id = parse_response.json()["data"]["id"]

    class MockResponse:
        status_code = 200
        headers = {"content-type": "video/mp4"}

        async def aiter_bytes(self):
            yield b"video-bytes"

        async def aclose(self):
            pass

    class MockClient:
        def build_request(self, method, url, headers=None):
            return {"method": method, "url": url, "headers": headers or {}}

        async def send(self, request, stream=False):
            assert request["url"] == "https://example.com/video.mp4"
            assert stream is True
            return MockResponse()

        async def aclose(self):
            pass

    monkeypatch.setattr("parse_video_py.web.create_async_client", lambda **_: MockClient())

    response = client.get("/material/download", params={"id": material_id})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("video/mp4")
    assert response.content == b"video-bytes"


def test_material_download_streams_image_by_index(monkeypatch):
    async def mock_parse_video_share_url(url):
        return VideoInfo(
            video_url="",
            cover_url="https://example.com/cover.jpg",
            images=[ImgInfo(url="https://example.com/image.jpg")],
        )

    monkeypatch.setattr(
        "parse_video_py.web.parse_video_share_url", mock_parse_video_share_url
    )

    parse_response = client.get(
        "/video/share/url/parse", params={"url": "https://v.douyin.com/demo"}
    )
    material_id = parse_response.json()["data"]["id"]

    class MockResponse:
        status_code = 200
        headers = {"content-type": "image/jpeg"}

        async def aiter_bytes(self):
            yield b"image-bytes"

        async def aclose(self):
            pass

    class MockClient:
        def build_request(self, method, url, headers=None):
            return {"method": method, "url": url, "headers": headers or {}}

        async def send(self, request, stream=False):
            assert request["url"] == "https://example.com/image.jpg"
            assert stream is True
            return MockResponse()

        async def aclose(self):
            pass

    monkeypatch.setattr("parse_video_py.web.create_async_client", lambda **_: MockClient())

    response = client.get(
        "/material/download", params={"id": material_id, "type": "image", "index": 0}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.content == b"image-bytes"
