"""
内容安全模块单元测试。

所有微信 API 调用均使用 mock，不访问真实线上接口。
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 在导入模块前设置必需的环境变量，避免模块级常量为空
# ---------------------------------------------------------------------------
os.environ.setdefault("WX_APPID", "wx_test_default")
os.environ.setdefault("WX_APPSECRET", "secret_test_default")
os.environ.setdefault("WX_API_MAX_RETRIES", "2")
os.environ.setdefault("WX_API_RETRY_BACKOFF", "0.01")

from parse_video_py.content_security import (  # noqa: E402
    MediaCheckCallbackResult,
    MediaCheckResult,
    TextCheckResult,
    WxSecurityConfigError,
    WxSecurityError,
    WxSecurityRejectedError,
    WxSecurityServiceError,
    _TokenCache,
    _wx_post_json,
    check_audio,
    check_image,
    check_text,
    extract_pdf_text,
    get_access_token,
    handle_callback_cleanup,
    invalidate_token,
    is_configured,
    label_description,
    parse_media_check_callback,
    security_error_response,
)
import parse_video_py.content_security as cs  # noqa: E402


# ======================================================================
# 通用 mock 辅助
# ======================================================================

def _make_json_response(data: dict) -> MagicMock:
    """构造一个模拟的 httpx Response。"""
    resp = MagicMock()
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


def _mock_wx_api(return_values: list[dict]) -> MagicMock:
    """返回一个 mock httpx Client，post() 依次返回给定 JSON 响应。

    用于 mock _build_http_client，绕过真实 HTTP 请求。
    """
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.side_effect = [_make_json_response(v) for v in return_values]
    return mock_client


# ======================================================================
# TokenCache 测试
# ======================================================================

class TestTokenCache:
    """access_token 缓存测试。"""

    def setup_method(self):
        # 每次测试前重置缓存
        invalidate_token()

    def test_first_call_fetches_token(self):
        """首次调用触发 token 获取。"""
        with patch.object(cs, "WX_APPID", "wx_test"):
            with patch.object(cs, "WX_APPSECRET", "secret_123"):
                mock_client = _mock_wx_api([
                    {"access_token": "fake_token_abc", "expires_in": 7200},
                ])
                with patch.object(cs, "_build_http_client", return_value=mock_client):
                    invalidate_token()
                    token = get_access_token()
                    assert token == "fake_token_abc"

    def test_cached_token_reused(self):
        """缓存有效时不重复请求。"""
        with patch.object(cs, "WX_APPID", "wx_test"):
            with patch.object(cs, "WX_APPSECRET", "secret_123"):
                mock_client = _mock_wx_api([
                    {"access_token": "token_xyz", "expires_in": 7200},
                ])
                with patch.object(cs, "_build_http_client", return_value=mock_client):
                    invalidate_token()
                    t1 = get_access_token()
                    t2 = get_access_token()
                    assert t1 == t2 == "token_xyz"
                    # 缓存命中，只调用一次 post
                    assert mock_client.post.call_count == 1

    def test_missing_appid_raises(self):
        """未配置 AppID 时抛出配置错误（直接调用 _refresh 绕过缓存）。"""
        cache = _TokenCache()
        with patch.object(cs, "WX_APPID", ""):
            with pytest.raises(WxSecurityConfigError, match="WX_APPID"):
                cache._refresh()

    def test_invalidate_forces_refresh(self):
        """invalidate 后强制重新获取 token。"""
        with patch.object(cs, "WX_APPID", "wx_test"):
            with patch.object(cs, "WX_APPSECRET", "secret_123"):
                mock_client = _mock_wx_api([
                    {"access_token": "token_1", "expires_in": 7200},
                    {"access_token": "token_2", "expires_in": 7200},
                ])
                with patch.object(cs, "_build_http_client", return_value=mock_client):
                    invalidate_token()
                    t1 = get_access_token()
                    assert t1 == "token_1"
                    invalidate_token()
                    t2 = get_access_token()
                    assert t2 == "token_2"
                    assert mock_client.post.call_count == 2

    def test_token_api_error(self):
        """微信返回错误时抛出服务异常。"""
        with patch.object(cs, "WX_APPID", "wx_test"):
            with patch.object(cs, "WX_APPSECRET", "secret_123"):
                mock_client = _mock_wx_api([
                    {"errcode": 40013, "errmsg": "invalid appid"},
                ])
                with patch.object(cs, "_build_http_client", return_value=mock_client):
                    invalidate_token()
                    with pytest.raises(WxSecurityServiceError, match="errcode=40013"):
                        get_access_token()


# ======================================================================
# check_text 测试
# ======================================================================

class TestCheckText:
    """文本审核测试。"""

    def setup_method(self):
        invalidate_token()

    def test_pass_result(self):
        """文本通过审核。"""
        mock_client = _mock_wx_api([
            {"access_token": "tok", "expires_in": 7200},
            {
                "errcode": 0,
                "result": {"suggest": "pass", "label": 100},
                "detail": [{"strategy": "content_model", "errcode": 0,
                             "suggest": "pass", "label": 100, "prob": 90}],
                "trace_id": "trace_123",
            },
        ])
        with patch.object(cs, "_build_http_client", return_value=mock_client):
            with patch.object(cs, "WX_CONTENT_SECURITY_STRICT", False):
                invalidate_token()
                result = check_text("合法内容测试", openid="test_openid")

        assert result.is_pass
        assert not result.is_rejected
        assert result.suggest == "pass"
        assert result.label == 100
        assert result.trace_id == "trace_123"

    def test_risky_result_not_strict(self):
        """非严格模式下违规不抛异常。"""
        mock_client = _mock_wx_api([
            {"access_token": "tok", "expires_in": 7200},
            {
                "errcode": 0,
                "result": {"suggest": "risky", "label": 20002},
                "detail": [],
                "trace_id": "trace_456",
            },
        ])
        with patch.object(cs, "_build_http_client", return_value=mock_client):
            with patch.object(cs, "WX_CONTENT_SECURITY_STRICT", False):
                invalidate_token()
                result = check_text("违规内容", openid="test_openid")

        assert result.is_rejected
        assert result.suggest == "risky"

    def test_risky_result_strict_mode_raises(self):
        """严格模式下违规抛出 WxSecurityRejectedError。"""
        mock_client = _mock_wx_api([
            {"access_token": "tok", "expires_in": 7200},
            {
                "errcode": 0,
                "result": {"suggest": "risky", "label": 20002},
                "detail": [],
                "trace_id": "trace_789",
            },
        ])
        with patch.object(cs, "_build_http_client", return_value=mock_client):
            with patch.object(cs, "WX_CONTENT_SECURITY_STRICT", True):
                invalidate_token()
                with pytest.raises(WxSecurityRejectedError, match="色情"):
                    check_text("违规内容", openid="test_openid")

    def test_empty_content_raises(self):
        """空文本抛出 ValueError。"""
        with pytest.raises(ValueError, match="不能为空"):
            check_text("", openid="test_openid")

    def test_content_too_long_raises(self):
        """超长文本抛出 ValueError。"""
        with pytest.raises(ValueError, match="2500"):
            check_text("x" * 2501, openid="test_openid")

    def test_empty_openid_raises(self):
        """空 openid 抛出 ValueError。"""
        with pytest.raises(ValueError, match="openid"):
            check_text("test", openid="")

    def test_invalid_scene_raises(self):
        """无效场景值抛出 ValueError。"""
        with pytest.raises(ValueError, match="scene"):
            check_text("test", openid="test_openid", scene=99)

    def test_review_result(self):
        """review 结果正确解析。"""
        mock_client = _mock_wx_api([
            {"access_token": "tok", "expires_in": 7200},
            {
                "errcode": 0,
                "result": {"suggest": "review", "label": 21000},
                "detail": [],
                "trace_id": "trace_review",
            },
        ])
        with patch.object(cs, "_build_http_client", return_value=mock_client):
            with patch.object(cs, "WX_CONTENT_SECURITY_STRICT", False):
                invalidate_token()
                result = check_text("疑似内容", openid="test_openid")

        assert result.needs_review
        assert not result.is_pass
        assert not result.is_rejected

    def test_api_error_raises_service_error(self):
        """审核 API 返回错误时抛出 WxSecurityServiceError。"""
        mock_client = _mock_wx_api([
            {"access_token": "tok", "expires_in": 7200},
            {"errcode": 61010, "errmsg": "code is expired"},
        ])
        with patch.object(cs, "_build_http_client", return_value=mock_client):
            invalidate_token()
            with pytest.raises(WxSecurityServiceError, match="61010"):
                check_text("test", openid="test_openid")


# ======================================================================
# check_image / check_audio 测试
# ======================================================================

class TestMediaCheckAsync:
    """多媒体异步审核测试。"""

    def setup_method(self):
        invalidate_token()

    def test_check_image_returns_trace_id(self):
        """图片审核提交返回 trace_id（不返回审核结论）。"""
        mock_client = _mock_wx_api([
            {"access_token": "token", "expires_in": 7200},
            {"errcode": 0, "trace_id": "img_trace_001"},
        ])
        with patch.object(cs, "_build_http_client", return_value=mock_client):
            invalidate_token()
            result = check_image(
                "https://example.com/img/test.png",
                openid="test_openid",
            )

        assert isinstance(result, MediaCheckResult)
        assert result.trace_id == "img_trace_001"

    def test_check_audio_returns_trace_id(self):
        """音频审核提交返回 trace_id。"""
        mock_client = _mock_wx_api([
            {"access_token": "token", "expires_in": 7200},
            {"errcode": 0, "trace_id": "audio_trace_002"},
        ])
        with patch.object(cs, "_build_http_client", return_value=mock_client):
            invalidate_token()
            result = check_audio(
                "https://example.com/audio/test.mp3",
                openid="test_openid",
            )

        assert isinstance(result, MediaCheckResult)
        assert result.trace_id == "audio_trace_002"

    def test_invalid_media_type_raises(self):
        """非法 media_type 抛出 ValueError。"""
        from parse_video_py.content_security import _submit_media_check
        with pytest.raises(ValueError, match="media_type"):
            _submit_media_check(
                "https://example.com/test.png",
                media_type=99,
                openid="test_openid",
            )


# ======================================================================
# 回调解析测试
# ======================================================================

class TestCallbackParsing:
    """回调解析测试。"""

    def test_parse_valid_callback(self):
        """解析合法的审核回调。"""
        body = json.dumps({
            "ToUserName": "gh_123",
            "FromUserName": "o_fake",
            "CreateTime": 1234567890,
            "MsgType": "event",
            "Event": "wxa_media_check",
            "appid": "wx_test",
            "trace_id": "cb_trace_001",
            "version": 2,
            "errcode": 0,
            "result": {"suggest": "risky", "label": 20002},
            "detail": [],
        })

        result = parse_media_check_callback(body)
        assert result is not None
        assert result.trace_id == "cb_trace_001"
        assert result.is_rejected
        assert result.label == 20002

    def test_parse_non_event_returns_none(self):
        """非事件消息返回 None。"""
        body = json.dumps({"MsgType": "text", "Content": "hello"})
        assert parse_media_check_callback(body) is None

    def test_parse_non_media_check_returns_none(self):
        """非审核事件返回 None。"""
        body = json.dumps({
            "MsgType": "event",
            "Event": "subscribe",
        })
        assert parse_media_check_callback(body) is None

    def test_parse_invalid_json_returns_none(self):
        """无效 JSON 返回 None。"""
        assert parse_media_check_callback("not json") is None

    def test_parse_bytes_body(self):
        """bytes 类型 body 正常解析。"""
        body = json.dumps({
            "ToUserName": "gh_123",
            "FromUserName": "o_fake",
            "CreateTime": 1234567890,
            "MsgType": "event",
            "Event": "wxa_media_check",
            "appid": "wx_001",
            "trace_id": "t_bytes",
            "errcode": 0,
            "result": {"suggest": "pass", "label": 100},
            "detail": [],
        }).encode("utf-8")

        result = parse_media_check_callback(body)
        assert result is not None
        assert result.trace_id == "t_bytes"
        assert result.is_pass


class TestCallbackCleanup:
    """回调清理逻辑测试。"""

    def test_risky_triggers_cleanup(self):
        """risky 结果触发文件清理。"""
        cleaned = []
        result = MediaCheckCallbackResult(
            trace_id="t1", appid="wx", suggest="risky", label=20002
        )
        handle_callback_cleanup(result, lambda: cleaned.append("deleted"))
        assert cleaned == ["deleted"]

    def test_pass_does_not_trigger_cleanup(self):
        """pass 结果不触发清理。"""
        cleaned = []
        result = MediaCheckCallbackResult(
            trace_id="t1", appid="wx", suggest="pass", label=100
        )
        handle_callback_cleanup(result, lambda: cleaned.append("deleted"))
        assert cleaned == []

    def test_cleanup_exception_does_not_raise(self):
        """清理异常不向上传播。"""
        def bad_cleaner():
            raise OSError("permission denied")

        result = MediaCheckCallbackResult(
            trace_id="t1", appid="wx", suggest="risky", label=20006
        )
        # 不抛异常
        handle_callback_cleanup(result, bad_cleaner)


# ======================================================================
# 辅助函数测试
# ======================================================================

class TestLabelDescription:
    """标签描述测试。"""

    def test_known_labels(self):
        assert label_description(100) == "正常"
        assert label_description(10001) == "广告"
        assert label_description(20001) == "时政"
        assert label_description(20002) == "色情"
        assert label_description(20003) == "辱骂"
        assert label_description(20006) == "违法犯罪"
        assert label_description(20008) == "欺诈"
        assert label_description(20012) == "低俗"
        assert label_description(20013) == "版权"
        assert label_description(21000) == "其他"

    def test_unknown_label(self):
        assert "99999" in label_description(99999)


class TestSecurityErrorResponse:
    """错误响应格式化测试。"""

    def test_rejected_error(self):
        exc = WxSecurityRejectedError("违规内容", label=20002)
        resp = security_error_response(exc)
        assert resp["success"] is False
        assert resp["code"] == "CONTENT_REJECTED"

    def test_service_error(self):
        exc = WxSecurityServiceError("服务异常", errcode=-1)
        resp = security_error_response(exc)
        assert resp["success"] is False
        assert resp["code"] == "SECURITY_SERVICE_ERROR"

    def test_config_error(self):
        exc = WxSecurityConfigError("未配置 AppID")
        resp = security_error_response(exc)
        assert resp["success"] is False
        assert resp["code"] == "SECURITY_CONFIG_ERROR"


class TestIsConfigured:
    """配置检查测试。"""

    def test_not_configured(self):
        with patch.object(cs, "WX_APPID", ""):
            with patch.object(cs, "WX_APPSECRET", ""):
                assert is_configured() is False

    def test_configured(self):
        with patch.object(cs, "WX_APPID", "wx_abc"):
            with patch.object(cs, "WX_APPSECRET", "sec_xyz"):
                assert is_configured() is True


# ======================================================================
# 文本提取测试
# ======================================================================

class TestExtractPdfText:
    """PDF 文本提取测试。"""

    def test_extract_text_from_pdf(self):
        """从 PDF 提取文本。"""
        try:
            import fitz
        except ImportError:
            pytest.skip("PyMuPDF 未安装")

        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Hello World PDF Text Extraction Test")

        pdf_path = Path(tempfile.mktemp(suffix=".pdf"))
        doc.save(str(pdf_path))
        doc.close()

        try:
            text = extract_pdf_text(pdf_path)
            assert "Hello World" in text
            assert "PDF Text Extraction" in text
        finally:
            pdf_path.unlink(missing_ok=True)

    def test_extract_text_max_chars(self):
        """文本提取受 max_chars 限制。"""
        try:
            import fitz
        except ImportError:
            pytest.skip("PyMuPDF 未安装")

        doc = fitz.open()
        page = doc.new_page()
        long_text = "ABC" * 1000
        page.insert_text((72, 72), long_text)

        pdf_path = Path(tempfile.mktemp(suffix=".pdf"))
        doc.save(str(pdf_path))
        doc.close()

        try:
            text = extract_pdf_text(pdf_path, max_chars=500)
            assert len(text) <= 500
        finally:
            pdf_path.unlink(missing_ok=True)


# ======================================================================
# _wx_post_json 重试测试
# ======================================================================

class TestWxPostJsonRetry:
    """底层 HTTP 重试逻辑测试。"""

    def setup_method(self):
        invalidate_token()

    def test_rate_limit_triggers_retry(self):
        """频率限制触发重试并最终成功。"""
        mock_client = _mock_wx_api([
            {"access_token": "token", "expires_in": 7200},
            {"errcode": 44991, "errmsg": "reach max api minute frequence"},
            {"errcode": 0, "trace_id": "retry_ok"},
        ])
        with patch.object(cs, "_build_http_client", return_value=mock_client):
            with patch.object(cs, "WX_API_MAX_RETRIES", 2):
                with patch.object(cs, "WX_API_RETRY_BACKOFF", 0.01):
                    invalidate_token()
                    data = _wx_post_json("https://test.url/api", {"test": True})
                    assert data["trace_id"] == "retry_ok"
                    # token(1) + 限流(1) + 成功(1) = 3 次 post
                    assert mock_client.post.call_count == 3

    def test_max_retries_exceeded_raises(self):
        """超过最大重试次数后抛出异常。"""
        mock_client = _mock_wx_api([
            {"access_token": "token", "expires_in": 7200},
            {"errcode": 44991, "errmsg": "rate limit"},
            {"errcode": 44991, "errmsg": "rate limit"},
            {"errcode": 44991, "errmsg": "rate limit"},
        ])
        with patch.object(cs, "_build_http_client", return_value=mock_client):
            with patch.object(cs, "WX_API_MAX_RETRIES", 2):
                with patch.object(cs, "WX_API_RETRY_BACKOFF", 0.01):
                    invalidate_token()
                    with pytest.raises(WxSecurityServiceError, match="频率超限"):
                        _wx_post_json("https://test.url/api", {"test": True})


# ======================================================================
# TextCheckResult / MediaCheckResult 数据类测试
# ======================================================================

class TestResultDataclasses:
    """结果数据类测试。"""

    def test_text_check_result_properties(self):
        r = TextCheckResult(suggest="pass", label=100, trace_id="t")
        assert r.is_pass
        assert not r.is_rejected
        assert not r.needs_review

    def test_text_check_result_risky(self):
        r = TextCheckResult(suggest="risky", label=20002, trace_id="t")
        assert not r.is_pass
        assert r.is_rejected
        assert not r.needs_review

    def test_text_check_result_review(self):
        r = TextCheckResult(suggest="review", label=21000, trace_id="t")
        assert not r.is_pass
        assert not r.is_rejected
        assert r.needs_review

    def test_media_check_callback_result(self):
        r = MediaCheckCallbackResult(
            trace_id="t", appid="wx", suggest="risky", label=20006
        )
        assert r.is_rejected
        assert not r.is_pass


# ======================================================================
# OpenID Token 签名 / 验证 测试
# ======================================================================

class TestOpenidToken:
    """openid 令牌签名与验证测试。"""

    def setup_method(self):
        os.environ["OPENID_SIGNING_KEY"] = "test_signing_key_32_bytes!!"

    def teardown_method(self):
        os.environ.pop("OPENID_SIGNING_KEY", None)

    def test_create_and_verify(self):
        """创建令牌并成功验证。"""
        from parse_video_py.content_security import (
            create_openid_token,
            verify_openid_token,
        )
        with patch.object(cs, "OPENID_SIGNING_KEY", "test_signing_key_32_bytes!!"):
            token = create_openid_token("o_test_user_123")
            openid = verify_openid_token(token)
            assert openid == "o_test_user_123"

    def test_verify_invalid_token_format(self):
        """无效格式抛出 ValueError。"""
        from parse_video_py.content_security import verify_openid_token
        with patch.object(cs, "OPENID_SIGNING_KEY", "test_key"):
            with pytest.raises(ValueError, match="格式无效"):
                verify_openid_token("not-valid-base64!!!")

    def test_verify_empty_token(self):
        """空令牌抛出 ValueError。"""
        from parse_video_py.content_security import verify_openid_token
        with pytest.raises(ValueError, match="不能为空"):
            verify_openid_token("")

    def test_verify_wrong_signature(self):
        """篡改签名后验证失败。"""
        from parse_video_py.content_security import (
            create_openid_token,
            verify_openid_token,
        )
        with patch.object(cs, "OPENID_SIGNING_KEY", "test_signing_key_32_bytes!!"):
            token = create_openid_token("o_test_user_123")
            # 篡改 token：修改 openid
            import base64
            import json
            decoded = json.loads(base64.urlsafe_b64decode(token.encode("ascii")))
            decoded["openid"] = "o_hacker"
            tampered = base64.urlsafe_b64encode(
                json.dumps(decoded).encode("utf-8")
            ).decode("ascii")
            with pytest.raises(ValueError, match="签名不匹配"):
                verify_openid_token(tampered)

    def test_verify_expired_token(self):
        """过期令牌抛出 ValueError。"""
        from parse_video_py.content_security import create_openid_token
        from parse_video_py.content_security import verify_openid_token
        with patch.object(cs, "OPENID_SIGNING_KEY", "test_signing_key_32_bytes!!"):
            # 创建 1 秒有效期的令牌
            token = create_openid_token("o_test_user_123", ttl=0)
            import time
            time.sleep(0.1)  # 确保过期
            with pytest.raises(ValueError, match="已过期"):
                verify_openid_token(token)

    def test_missing_signing_key_raises(self):
        """未配置 OPENID_SIGNING_KEY 时抛出配置错误。"""
        from parse_video_py.content_security import verify_openid_token
        import base64
        import json
        # 构造一个 token 但清除 signing key
        token_data = json.dumps(
            {"openid": "o_test", "exp": 9999999999, "sig": "fake"},
            separators=(",", ":"),
        )
        token = base64.urlsafe_b64encode(token_data.encode("utf-8")).decode("ascii")
        with patch.object(cs, "OPENID_SIGNING_KEY", ""):
            with pytest.raises(WxSecurityConfigError, match="OPENID_SIGNING_KEY"):
                verify_openid_token(token)


# ======================================================================
# 微信加解密测试
# ======================================================================

class TestWxCrypto:
    """微信回调加解密测试。"""

    def test_sha1_signature(self):
        """SHA1 签名计算正确。"""
        from parse_video_py.content_security import _wx_sha1_signature
        sig = _wx_sha1_signature("token123", "1234567890", "abc123")
        assert len(sig) == 40  # SHA1 hex digest
        # 相同输入应该产生相同签名
        sig2 = _wx_sha1_signature("token123", "1234567890", "abc123")
        assert sig == sig2

    def test_sha1_signature_order_independent(self):
        """签名与参数顺序无关（sorted）。"""
        from parse_video_py.content_security import _wx_sha1_signature
        sig1 = _wx_sha1_signature("a", "b", "c")
        sig2 = _wx_sha1_signature("c", "a", "b")
        assert sig1 == sig2

    def test_aes_decrypt_encrypt_roundtrip(self):
        """AES 加解密往返测试。"""
        from parse_video_py.content_security import _wx_aes_decrypt, _wx_aes_encrypt
        test_aes_key = "a" * 43  # 43 字符的 Base64 key
        test_msg = json.dumps({
            "ToUserName": "gh_test",
            "MsgType": "event",
            "Event": "wxa_media_check",
            "trace_id": "test_trace_001",
            "result": {"suggest": "pass", "label": 100},
        })
        with patch.object(cs, "WX_CALLBACK_AES_KEY", test_aes_key):
            with patch.object(cs, "WX_APPID", "wx_test"):
                encrypted = _wx_aes_encrypt(test_msg)
                decrypted, appid = _wx_aes_decrypt(encrypted)
                assert decrypted == test_msg
                assert appid == "wx_test"

    def test_aes_decrypt_invalid_base64(self):
        """无效 Base64 抛出服务异常。"""
        from parse_video_py.content_security import _wx_aes_decrypt
        with patch.object(cs, "WX_CALLBACK_AES_KEY", "a" * 43):
            with pytest.raises(WxSecurityServiceError, match="Base64"):
                _wx_aes_decrypt("!!!not-valid-base64!!!")

    def test_aes_decrypt_missing_key(self):
        """未配置 AES Key 抛出配置错误。"""
        from parse_video_py.content_security import _wx_aes_decrypt
        with patch.object(cs, "WX_CALLBACK_AES_KEY", ""):
            with pytest.raises(WxSecurityConfigError, match="WX_CALLBACK_AES_KEY"):
                _wx_aes_decrypt("dGVzdA==")

    def test_verify_signature_matches(self):
        """签名验证通过。"""
        from parse_video_py.content_security import verify_wx_callback_signature
        with patch.object(cs, "WX_CALLBACK_TOKEN", "my_token"):
            # 预计算正确签名
            from parse_video_py.content_security import _wx_sha1_signature
            sig = _wx_sha1_signature("my_token", "123", "abc", "encrypted_msg")
            assert verify_wx_callback_signature(sig, "123", "abc", "encrypted_msg")

    def test_verify_signature_mismatch(self):
        """签名不匹配返回 False。"""
        from parse_video_py.content_security import verify_wx_callback_signature
        with patch.object(cs, "WX_CALLBACK_TOKEN", "my_token"):
            assert not verify_wx_callback_signature(
                "wrong_signature_here", "123", "abc", "msg"
            )

    def test_verify_signature_no_token_configured(self):
        """未配置 token 时跳过验证（开发环境）。"""
        from parse_video_py.content_security import verify_wx_callback_signature
        with patch.object(cs, "WX_CALLBACK_TOKEN", ""):
            assert verify_wx_callback_signature("anything", "1", "2", "3") is True


# ======================================================================
# AuditTask 数据类测试
# ======================================================================

class TestAuditTask:
    """AuditTask 序列化测试。"""

    def test_to_dict_and_from_dict(self):
        """序列化往返一致。"""
        from parse_video_py.content_security import AuditTask
        task = AuditTask(
            job_id="job_001",
            trace_id="trace_abc",
            file_path="/tmp/output/test.gif",
            status="pending",
            created_at=1234567890.0,
            updated_at=1234567890.0,
            media_url="https://example.com/files/test.gif",
            openid_hash="abcdef1234567890",
            media_type="image",
        )
        d = task.to_dict()
        restored = AuditTask.from_dict(d)
        assert restored.job_id == task.job_id
        assert restored.trace_id == task.trace_id
        assert restored.file_path == task.file_path
        assert restored.status == task.status
        assert restored.media_url == task.media_url

    def test_from_dict_with_missing_optional_fields(self):
        """缺少可选字段的旧数据兼容。"""
        from parse_video_py.content_security import AuditTask
        task = AuditTask.from_dict({
            "job_id": "job_002",
            "trace_id": "trace_xyz",
            "file_path": "/tmp/output/test2.gif",
            "status": "approved",
            "created_at": 1234567890.0,
            "updated_at": 1234567899.0,
            # 缺少 media_url, openid_hash, media_type, result
        })
        assert task.job_id == "job_002"
        assert task.media_url == ""
        assert task.openid_hash == ""
        assert task.result is None


# ======================================================================
# AuditTaskStore 测试
# ======================================================================

class TestAuditTaskStore:
    """审核任务持久化存储测试。"""

    def setup_method(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp(prefix="taskstore_test_")
        self._store_path = Path(self._tmpdir) / "test_tasks.json"

    def teardown_method(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_store(self) -> "cs.AuditTaskStore":
        store = cs.AuditTaskStore(str(self._store_path))
        store._ensure_dir()
        return store

    def test_create_and_get_task(self):
        """创建任务后可查询。"""
        store = self._make_store()
        task = store.create_task(
            job_id="job_001",
            trace_id="trace_abc",
            file_path="/tmp/test.gif",
            openid="o_test",
            media_url="https://example.com/test.gif",
            media_type="image",
        )
        assert task.status == "pending"
        assert task.job_id == "job_001"

        retrieved = store.get_task("job_001")
        assert retrieved is not None
        assert retrieved.trace_id == "trace_abc"
        assert retrieved.openid_hash != "o_test"  # 哈希存储
        assert len(retrieved.openid_hash) == 16

    def test_get_task_not_found(self):
        """查询不存在的任务返回 None。"""
        store = self._make_store()
        assert store.get_task("nonexistent") is None

    def test_get_by_trace_id(self):
        """按 trace_id 查询任务。"""
        store = self._make_store()
        store.create_task(
            job_id="job_x", trace_id="trace_x", file_path="/tmp/x.gif"
        )
        task = store.get_task_by_trace_id("trace_x")
        assert task is not None
        assert task.job_id == "job_x"

    def test_update_by_trace_id(self):
        """回调更新任务状态。"""
        store = self._make_store()
        store.create_task(
            job_id="job_001", trace_id="trace_001", file_path="/tmp/test.gif"
        )
        updated = store.update_by_trace_id(
            "trace_001", "approved",
            result={"suggest": "pass", "label": 100},
        )
        assert updated is not None
        assert updated.status == "approved"
        assert updated.result["suggest"] == "pass"

        # 再次查询确认持久化
        task = store.get_task("job_001")
        assert task.status == "approved"

    def test_update_unknown_trace_id(self):
        """未知 trace_id 的更新返回 None。"""
        store = self._make_store()
        result = store.update_by_trace_id("unknown_trace", "approved")
        assert result is None

    def test_cleanup_expired_pending_tasks(self):
        """过期 pending 任务标记为 error。"""
        import time as _time
        store = self._make_store()
        # 创建一个"过去"的任务（5000 秒前，超过 3600 TTL 但不到 7200）
        task = store.create_task(
            job_id="old_job", trace_id="old_trace", file_path="/tmp/old.gif"
        )
        # 手动修改 created_at
        task.created_at = _time.time() - 5000
        store._tasks["old_job"] = task
        store._save()

        cleaned = store.cleanup_expired(ttl=3600)  # 1 小时 TTL
        assert cleaned >= 1  # 至少标记了 1 个

        retrieved = store.get_task("old_job")
        assert retrieved is not None
        assert retrieved.status == "error"

    def test_cleanup_very_old_tasks_removed(self):
        """超过 2 倍 TTL 的任务直接删除。"""
        import time as _time
        store = self._make_store()
        task = store.create_task(
            job_id="ancient_job", trace_id="ancient_trace", file_path="/tmp/old.gif"
        )
        task.created_at = _time.time() - 100000
        store._tasks["ancient_job"] = task
        store._save()

        cleaned = store.cleanup_expired(ttl=3600)
        assert cleaned >= 1
        assert store.get_task("ancient_job") is None

    def test_persistence_and_recovery(self):
        """任务持久化到文件并从文件恢复。"""
        store = self._make_store()
        store.create_task(
            job_id="persist_job", trace_id="persist_trace", file_path="/tmp/p.gif"
        )
        assert self._store_path.exists()

        # 模拟重启：创建新实例从同一文件加载
        store2 = cs.AuditTaskStore(str(self._store_path))
        count = store2._load()
        assert count >= 1
        task = store2.get_task("persist_job")
        assert task is not None
        assert task.trace_id == "persist_trace"

    def test_task_count(self):
        """任务计数正确。"""
        store = self._make_store()
        assert store.task_count == 0
        store.create_task(job_id="j1", trace_id="t1", file_path="/tmp/f1.gif")
        store.create_task(job_id="j2", trace_id="t2", file_path="/tmp/f2.gif")
        assert store.task_count == 2

    def test_load_corrupt_file(self):
        """损坏的持久化文件不会崩溃。"""
        self._store_path.write_text("not json at all", encoding="utf-8")
        store = cs.AuditTaskStore(str(self._store_path))
        count = store._load()
        assert count == 0


# ======================================================================
# 回调 URL 验证测试
# ======================================================================

class TestCallbackUrlVerification:
    """URL 验证处理测试。"""

    def test_verify_success_plaintext(self):
        """明文模式 URL 验证成功。"""
        from parse_video_py.content_security import handle_callback_url_verification
        with patch.object(cs, "WX_CALLBACK_TOKEN", "test_token"):
            with patch.object(cs, "WX_CALLBACK_AES_KEY", ""):
                from parse_video_py.content_security import _wx_sha1_signature
                echostr = "test_echo_plain"
                sig = _wx_sha1_signature("test_token", "111", "222", echostr)
                ok, result = handle_callback_url_verification(
                    sig, "111", "222", echostr
                )
                assert ok is True
                assert result == echostr

    def test_verify_fails_bad_signature(self):
        """签名不匹配验证失败。"""
        from parse_video_py.content_security import handle_callback_url_verification
        with patch.object(cs, "WX_CALLBACK_TOKEN", "test_token"):
            ok, result = handle_callback_url_verification(
                "bad_signature", "111", "222", "echo_val"
            )
            assert ok is False
            assert result is None

    def test_verify_with_aes_encryption(self):
        """加密模式 URL 验证成功。"""
        from parse_video_py.content_security import (
            _wx_aes_encrypt,
            _wx_sha1_signature,
            handle_callback_url_verification,
        )
        test_aes_key = "b" * 43
        echostr_plain = "hello_wechat_echo_123"
        with patch.object(cs, "WX_CALLBACK_TOKEN", "test_token"):
            with patch.object(cs, "WX_CALLBACK_AES_KEY", test_aes_key):
                echostr_encrypted = _wx_aes_encrypt(echostr_plain)
                sig = _wx_sha1_signature(
                    "test_token", "111", "222", echostr_encrypted
                )
                ok, result = handle_callback_url_verification(
                    sig, "111", "222", echostr_encrypted
                )
                assert ok is True
                assert result == echostr_plain


# ======================================================================
# 回调事件处理测试
# ======================================================================

class TestCallbackEvent:
    """回调事件处理测试。"""

    def setup_method(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp(prefix="cbevent_test_")
        self._store_path = Path(self._tmpdir) / "cb_tasks.json"
        # 初始化任务存储到临时路径
        with patch.object(cs, "WX_SECURITY_TASK_STORE", str(self._store_path)):
            global _task_store
            cs._task_store = None  # 重置单例

    def teardown_method(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        cs._task_store = None

    def test_plaintext_event_pass(self):
        """明文回调：审核通过。"""
        from parse_video_py.content_security import handle_callback_event, get_task_store
        with patch.object(cs, "WX_SECURITY_TASK_STORE", str(self._store_path)):
            cs._task_store = None
            store = get_task_store()
            store.create_task(
                job_id="cb_job_1", trace_id="cb_trace_1", file_path="/tmp/cb_test.gif"
            )

            body = json.dumps({
                "ToUserName": "gh_test",
                "FromUserName": "o_user",
                "CreateTime": 1234567890,
                "MsgType": "event",
                "Event": "wxa_media_check",
                "appid": "wx_test",
                "trace_id": "cb_trace_1",
                "version": 2,
                "errcode": 0,
                "result": {"suggest": "pass", "label": 100},
                "detail": [],
            })

            result = handle_callback_event(body)
            assert result is not None
            assert result.is_pass
            assert result.trace_id == "cb_trace_1"

            # 验证任务状态已更新
            task = store.get_task("cb_job_1")
            assert task.status == "approved"

    def test_plaintext_event_risky(self):
        """明文回调：审核不通过。"""
        from parse_video_py.content_security import handle_callback_event, get_task_store

        # 创建临时文件用于测试删除
        test_file = Path(self._tmpdir) / "risky_test.gif"
        test_file.write_text("fake gif content")

        with patch.object(cs, "WX_SECURITY_TASK_STORE", str(self._store_path)):
            cs._task_store = None
            store = get_task_store()
            store.create_task(
                job_id="cb_job_2",
                trace_id="cb_trace_2",
                file_path=str(test_file),
            )

            body = json.dumps({
                "ToUserName": "gh_test",
                "FromUserName": "o_user",
                "CreateTime": 1234567890,
                "MsgType": "event",
                "Event": "wxa_media_check",
                "appid": "wx_test",
                "trace_id": "cb_trace_2",
                "version": 2,
                "errcode": 0,
                "result": {"suggest": "risky", "label": 20002},
                "detail": [],
            })

            result = handle_callback_event(body)
            assert result is not None
            assert result.is_rejected

            # 验证任务状态已更新为 rejected
            task = store.get_task("cb_job_2")
            assert task.status == "rejected"

            # 验证文件已被删除
            assert not test_file.exists()

    def test_non_event_returns_none(self):
        """非事件消息返回 None。"""
        from parse_video_py.content_security import handle_callback_event
        body = json.dumps({"MsgType": "text", "Content": "hello"})
        assert handle_callback_event(body) is None

    def test_bytes_body(self):
        """bytes 格式 body 正常处理。"""
        from parse_video_py.content_security import handle_callback_event, get_task_store
        with patch.object(cs, "WX_SECURITY_TASK_STORE", str(self._store_path)):
            cs._task_store = None
            store = get_task_store()
            store.create_task(
                job_id="bytes_job", trace_id="bytes_trace", file_path="/tmp/bytes.gif"
            )

            body = json.dumps({
                "ToUserName": "gh_test",
                "MsgType": "event",
                "Event": "wxa_media_check",
                "trace_id": "bytes_trace",
                "result": {"suggest": "pass", "label": 100},
            }).encode("utf-8")

            result = handle_callback_event(body)
            assert result is not None
            assert result.is_pass

    def test_encrypted_event(self):
        """加密回调事件解密并处理。"""
        from parse_video_py.content_security import (
            _wx_aes_encrypt,
            _wx_sha1_signature,
            handle_callback_event,
            get_task_store,
        )
        test_aes_key = "c" * 43

        inner_event = json.dumps({
            "ToUserName": "gh_test",
            "FromUserName": "o_user",
            "CreateTime": 1234567890,
            "MsgType": "event",
            "Event": "wxa_media_check",
            "appid": "wx_test",
            "trace_id": "enc_trace_1",
            "version": 2,
            "errcode": 0,
            "result": {"suggest": "pass", "label": 100},
            "detail": [],
        })

        with patch.object(cs, "WX_CALLBACK_AES_KEY", test_aes_key):
            encrypted = _wx_aes_encrypt(inner_event)

        outer = json.dumps({
            "ToUserName": "gh_test",
            "Encrypt": encrypted,
        })

        with patch.object(cs, "WX_SECURITY_TASK_STORE", str(self._store_path)):
            with patch.object(cs, "WX_CALLBACK_AES_KEY", test_aes_key):
                cs._task_store = None
                store = get_task_store()
                store.create_task(
                    job_id="enc_job", trace_id="enc_trace_1", file_path="/tmp/enc.gif"
                )

                result = handle_callback_event(outer)
                assert result is not None
                assert result.trace_id == "enc_trace_1"
                assert result.is_pass


# ======================================================================
# FastAPI 端点集成测试（使用 TestClient + mock）
# ======================================================================

class TestMediaConvertWebSecurity:
    """media_convert_web 安全端点集成测试。"""

    @pytest.fixture(autouse=True)
    def _setup_app(self):
        """导入 FastAPI 应用并创建 TestClient。"""
        from fastapi.testclient import TestClient
        import parse_video_py.media_convert_web as mw

        # 保存原始值
        self._orig_sec_enabled = mw._SEC_ENABLED
        self._orig_sec_strict = mw._SEC_STRICT
        self._orig_api_token = mw.API_TOKEN
        self._orig_public_url = mw.PUBLIC_BASE_URL
        self._orig_output_dir = mw.OUTPUT_DIR
        self._orig_upload_dir = mw.UPLOAD_DIR

        # 使用临时目录
        import tempfile
        self._tmpdir = tempfile.mkdtemp(prefix="mw_test_")
        self._output_dir = Path(self._tmpdir) / "output"
        self._upload_dir = Path(self._tmpdir) / "upload"
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._upload_dir.mkdir(parents=True, exist_ok=True)

        mw.OUTPUT_DIR = self._output_dir
        mw.UPLOAD_DIR = self._upload_dir
        mw.API_TOKEN = ""  # 关闭鉴权
        mw.PUBLIC_BASE_URL = "https://test.example.com"

        self.client = TestClient(mw.app)

    def teardown_method(self):
        import shutil
        import parse_video_py.media_convert_web as mw
        mw._SEC_ENABLED = self._orig_sec_enabled
        mw._SEC_STRICT = self._orig_sec_strict
        mw.API_TOKEN = self._orig_api_token
        mw.PUBLIC_BASE_URL = self._orig_public_url
        mw.OUTPUT_DIR = self._orig_output_dir
        mw.UPLOAD_DIR = self._orig_upload_dir
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        cs._task_store = None

    def test_to_gif_rejects_missing_openid_token_when_sec_enabled(self):
        """安全开启时缺少 openid_token 返回 400。"""
        import parse_video_py.media_convert_web as mw
        from io import BytesIO
        with patch.object(mw, "_SEC_ENABLED", True):
            with patch.object(cs, "OPENID_SIGNING_KEY", "test_key"):
                dummy_file = BytesIO(b"fake mp4 content " * 100)
                response = self.client.post(
                    "/media/video/to-gif",
                    files={"file": ("test.mp4", dummy_file, "video/mp4")},
                    data={
                        "start_time": 0,
                        "duration": 3,
                        "width": 360,
                        "fps": 10,
                    },
                )
                assert response.status_code == 400
                data = response.json()
                msg = data.get("message", data.get("detail", ""))
                assert "openid_token" in str(msg)

    def test_to_gif_rejects_invalid_openid_token(self):
        """安全开启时无效 openid_token 返回 400。"""
        import parse_video_py.media_convert_web as mw
        from io import BytesIO
        with patch.object(mw, "_SEC_ENABLED", True):
            with patch.object(cs, "OPENID_SIGNING_KEY", "test_key"):
                dummy_file = BytesIO(b"fake mp4 content " * 100)
                response = self.client.post(
                    "/media/video/to-gif",
                    files={"file": ("test.mp4", dummy_file, "video/mp4")},
                    data={
                        "start_time": 0,
                        "duration": 3,
                        "width": 360,
                        "fps": 10,
                        "openid_token": "invalid_base64!!!",
                    },
                )
                assert response.status_code == 400
                data = response.json()
                msg = data.get("message", data.get("detail", ""))
                assert "无效" in str(msg) or "格式" in str(msg)

    def test_gif_status_not_found(self):
        """查询不存在的任务返回 404。"""
        response = self.client.get("/media/video/to-gif/status/nonexistent_job")
        assert response.status_code == 404

    def test_gif_status_pending(self):
        """pending 状态任务返回正确结构。"""
        from parse_video_py.content_security import get_task_store
        with patch.object(cs, "WX_SECURITY_TASK_STORE",
                          str(Path(self._tmpdir) / "status_test.json")):
            cs._task_store = None
            store = get_task_store()
            store.create_task(
                job_id="job_pending",
                trace_id="trace_p",
                file_path=str(self._output_dir / "pending.gif"),
            )

            response = self.client.get("/media/video/to-gif/status/job_pending")
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["status"] == "pending"
            assert data["downloadUrl"] == ""

    def test_gif_status_approved(self):
        """approved 状态返回 downloadUrl。"""
        from parse_video_py.content_security import get_task_store
        import parse_video_py.media_convert_web as mw

        test_file = self._output_dir / "approved.gif"
        test_file.write_text("fake gif")

        with patch.object(cs, "WX_SECURITY_TASK_STORE",
                          str(Path(self._tmpdir) / "approved_test.json")):
            cs._task_store = None
            store = get_task_store()
            store.create_task(
                job_id="job_approved",
                trace_id="trace_a",
                file_path=str(test_file),
            )
            store.update_by_trace_id("trace_a", "approved",
                                     result={"suggest": "pass", "label": 100})

            response = self.client.get("/media/video/to-gif/status/job_approved")
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["status"] == "approved"
            assert "downloadUrl" in data
            assert "approved.gif" in data.get("downloadUrl", "")

    def test_gif_status_rejected(self):
        """rejected 状态不返回 downloadUrl。"""
        from parse_video_py.content_security import get_task_store
        with patch.object(cs, "WX_SECURITY_TASK_STORE",
                          str(Path(self._tmpdir) / "rejected_test.json")):
            cs._task_store = None
            store = get_task_store()
            store.create_task(
                job_id="job_rejected",
                trace_id="trace_r",
                file_path=str(self._output_dir / "rejected.gif"),
            )
            store.update_by_trace_id(
                "trace_r", "rejected",
                result={"suggest": "risky", "label": 20002},
            )

            response = self.client.get("/media/video/to-gif/status/job_rejected")
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["status"] == "rejected"
            assert data["downloadUrl"] == ""

    def test_callback_get_verification(self):
        """GET 回调 URL 验证。"""
        from parse_video_py.content_security import _wx_sha1_signature
        with patch.object(cs, "WX_CALLBACK_TOKEN", "cb_token"):
            with patch.object(cs, "WX_CALLBACK_AES_KEY", ""):
                echostr = "echo_test_value"
                sig = _wx_sha1_signature("cb_token", "111", "222", echostr)
                response = self.client.get(
                    "/content-security/callback",
                    params={
                        "signature": sig,
                        "timestamp": "111",
                        "nonce": "222",
                        "echostr": echostr,
                    },
                )
                assert response.status_code == 200
                assert response.text == echostr

    def test_callback_get_missing_params(self):
        """GET 回调缺少参数返回 400。"""
        response = self.client.get("/content-security/callback")
        assert response.status_code == 400

    def test_callback_post_event(self):
        """POST 回调事件返回 success。"""
        from parse_video_py.content_security import get_task_store
        with patch.object(cs, "WX_SECURITY_TASK_STORE",
                          str(Path(self._tmpdir) / "cb_post_test.json")):
            cs._task_store = None
            store = get_task_store()
            store.create_task(
                job_id="post_job", trace_id="post_trace",
                file_path=str(self._output_dir / "post_test.gif"),
            )

            body = json.dumps({
                "ToUserName": "gh_test",
                "MsgType": "event",
                "Event": "wxa_media_check",
                "trace_id": "post_trace",
                "result": {"suggest": "pass", "label": 100},
            })

            response = self.client.post(
                "/content-security/callback",
                content=body,
            )
            assert response.status_code == 200
            assert response.text == "success"

            # 验证任务已更新
            task = store.get_task("post_job")
            assert task.status == "approved"

    def test_callback_post_empty_body(self):
        """POST 回调空 body 返回 400。"""
        response = self.client.post("/content-security/callback")
        assert response.status_code == 400


class TestDocumentConvertWebSecurity:
    """document_convert_web 安全端点集成测试。"""

    @pytest.fixture(autouse=True)
    def _setup_app(self):
        """导入 FastAPI 应用并创建 TestClient。"""
        from fastapi.testclient import TestClient
        import parse_video_py.document_convert_web as dw

        self._orig_sec_enabled = dw._SEC_ENABLED
        self._orig_api_token = dw.API_TOKEN
        self._orig_output_dir = dw.OUTPUT_DIR

        import tempfile
        self._tmpdir = tempfile.mkdtemp(prefix="dw_test_")
        self._output_dir = Path(self._tmpdir) / "output"
        self._output_dir.mkdir(parents=True, exist_ok=True)

        dw.OUTPUT_DIR = self._output_dir
        dw.API_TOKEN = ""

        self.client = TestClient(dw.app)

    def teardown_method(self):
        import shutil
        import parse_video_py.document_convert_web as dw
        dw._SEC_ENABLED = self._orig_sec_enabled
        dw.API_TOKEN = self._orig_api_token
        dw.OUTPUT_DIR = self._orig_output_dir
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_pdf_to_images_rejects_when_sec_enabled_no_openid(self):
        """安全开启但未提供 openid_token 返回 400。"""
        import parse_video_py.document_convert_web as dw
        from io import BytesIO
        with patch.object(dw, "_SEC_ENABLED", True):
            dummy_file = BytesIO(b"fake pdf content " * 100)
            response = self.client.post(
                "/document/pdf-to-images",
                files={"file": ("test.pdf", dummy_file, "application/pdf")},
                data={"pages": "1"},
            )
            assert response.status_code == 400
            detail = response.json().get("detail", "")
            if isinstance(detail, list):
                detail = str(detail)
            assert "openid_token" in str(detail)

    def test_pdf_to_images_rejects_invalid_openid_token(self):
        """安全开启时无效 openid_token 返回 400。"""
        import parse_video_py.document_convert_web as dw
        from io import BytesIO
        with patch.object(dw, "_SEC_ENABLED", True):
            with patch.object(cs, "OPENID_SIGNING_KEY", "test_key"):
                dummy_file = BytesIO(b"fake pdf content " * 100)
                response = self.client.post(
                    "/document/pdf-to-images",
                    files={"file": ("test.pdf", dummy_file, "application/pdf")},
                    data={
                        "pages": "1",
                        "openid_token": "invalid_token",
                    },
                )
                assert response.status_code == 400
                detail = response.json().get("detail", "")
                if isinstance(detail, list):
                    detail = str(detail)
                assert "无效" in str(detail)

    def test_pdf_to_images_fallback_raw_openid(self):
        """向后兼容：安全开启时仍接受原始 openid。"""
        import parse_video_py.document_convert_web as dw
        from io import BytesIO
        with patch.object(dw, "_SEC_ENABLED", True):
            dummy_file = BytesIO(b"fake pdf content " * 100)
            # 不会因为 openid_token 缺失而报错，因为提供了原始 openid
            # 会因文件不是真实 PDF 而在后续处理中失败，但参数校验阶段通过
            response = self.client.post(
                "/document/pdf-to-images",
                files={"file": ("test.pdf", dummy_file, "application/pdf")},
                data={"pages": "1", "openid": "o_raw_test"},
            )
            # 文件格式不合法导致 422，但不应该报 openid_token 相关错误
            detail = response.json().get("detail", "")
            if isinstance(detail, list):
                detail = str(detail)
            assert "openid_token" not in str(detail).lower()


# ======================================================================
# Auth Web 端点集成测试（使用 TestClient + mock）
# ======================================================================

class TestAuthWeb:
    """auth_web 微信登录端点集成测试。"""

    @pytest.fixture(autouse=True)
    def _setup_app(self):
        """导入 FastAPI 应用并创建 TestClient。"""
        from fastapi.testclient import TestClient
        import parse_video_py.auth_web as aw

        # 保存原始模块级常量
        self._orig_wx_appid = aw.WX_APPID
        self._orig_wx_appsecret = aw.WX_APPSECRET
        self._orig_signing_key = cs.OPENID_SIGNING_KEY
        self._orig_token_ttl = cs.OPENID_TOKEN_TTL

        self.client = TestClient(aw.app)
        self.aw = aw
        self._user_db_patcher = patch.object(aw, "get_or_create_user")
        self._user_db_patcher.start()

    def teardown_method(self):
        import parse_video_py.auth_web as aw
        aw.WX_APPID = self._orig_wx_appid
        aw.WX_APPSECRET = self._orig_wx_appsecret
        # 恢复 content_security 模块常量
        cs.OPENID_SIGNING_KEY = self._orig_signing_key
        cs.OPENID_TOKEN_TTL = self._orig_token_ttl
        self._user_db_patcher.stop()

    # ------------------------------------------------------------------
    # 成功场景
    # ------------------------------------------------------------------

    def test_login_success(self):
        """正常登录返回 openidToken 和 expiresIn。"""
        mock_wx_response = {
            "openid": "o_test_user_abc123",
            "session_key": "sk_test_sensitive_value",
            "unionid": "u_test_union",
            "errcode": 0,
            "errmsg": "ok",
        }

        with patch.object(cs, "OPENID_SIGNING_KEY", "test_signing_key_for_auth"), \
             patch.object(self.aw, "OPENID_SIGNING_KEY", "test_signing_key_for_auth"), \
             patch.object(cs, "OPENID_TOKEN_TTL", 7200), \
             patch.object(self.aw, "OPENID_TOKEN_TTL", 7200), \
             patch.object(
                 self.aw, "_call_code2session", return_value=mock_wx_response
             ), \
             patch.object(self.aw, "get_or_create_user") as get_user:
            response = self.client.post(
                "/auth/wechat-login",
                json={"code": "valid_wx_code_123"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert data["msg"] == "ok"
        assert "openidToken" in data["data"]
        assert data["data"]["expiresIn"] == 7200
        # 验证 token 不为空
        assert len(data["data"]["openidToken"]) > 0
        get_user.assert_called_once_with(
            "o_test_user_abc123", "u_test_union"
        )

    def test_login_does_not_leak_openid_or_session_key(self):
        """响应中不包含原始 openid、session_key 或签名密钥。"""
        mock_wx_response = {
            "openid": "o_secret_user_xyz",
            "session_key": "sk_top_secret_12345",
            "errcode": 0,
            "errmsg": "ok",
        }

        with patch.object(cs, "OPENID_SIGNING_KEY", "test_signing_key_for_auth"), \
             patch.object(self.aw, "OPENID_SIGNING_KEY", "test_signing_key_for_auth"), \
             patch.object(
                 self.aw, "_call_code2session", return_value=mock_wx_response
             ):
                response = self.client.post(
                    "/auth/wechat-login",
                    json={"code": "valid_code"},
                )

        assert response.status_code == 200
        data = response.json()
        # 检查响应中不含敏感字段
        response_str = str(data)
        assert "o_secret_user_xyz" not in response_str
        assert "sk_top_secret_12345" not in response_str
        assert "test_signing_key_for_auth" not in response_str
        assert "session_key" not in response_str.lower()

    # ------------------------------------------------------------------
    # 参数校验
    # ------------------------------------------------------------------

    def test_login_missing_code_field(self):
        """请求体缺少 code 字段返回 400。"""
        response = self.client.post(
            "/auth/wechat-login",
            json={"other_field": "value"},
        )
        assert response.status_code == 400
        data = response.json()
        assert "code" in str(data).lower()

    def test_login_empty_code(self):
        """code 为空字符串返回 400。"""
        response = self.client.post(
            "/auth/wechat-login",
            json={"code": ""},
        )
        assert response.status_code == 400
        data = response.json()
        assert "code" in str(data).lower()

    def test_login_code_whitespace_only(self):
        """code 仅包含空白字符返回 400。"""
        response = self.client.post(
            "/auth/wechat-login",
            json={"code": "   "},
        )
        assert response.status_code == 400

    def test_login_invalid_json_body(self):
        """非 JSON 请求体返回 400。"""
        response = self.client.post(
            "/auth/wechat-login",
            content="not json at all",
            headers={"Content-Type": "text/plain"},
        )
        assert response.status_code == 400

    def test_login_body_not_dict(self):
        """请求体不是 JSON 对象返回 400。"""
        response = self.client.post(
            "/auth/wechat-login",
            json=["array", "not", "object"],
        )
        assert response.status_code == 400

    # ------------------------------------------------------------------
    # 微信 API 错误
    # ------------------------------------------------------------------

    def test_login_wechat_invalid_code(self):
        """微信返回 code 无效 (40029) 时返回 400。"""
        from parse_video_py.content_security import WxSecurityServiceError

        with patch.object(cs, "OPENID_SIGNING_KEY", "test_key"), \
             patch.object(self.aw, "OPENID_SIGNING_KEY", "test_key"), \
             patch.object(
                 self.aw,
                 "_call_code2session",
                 side_effect=WxSecurityServiceError(
                     "code2Session 失败: errcode=40029 errmsg=invalid code",
                     errcode=40029,
                 ),
             ):
                response = self.client.post(
                    "/auth/wechat-login",
                    json={"code": "expired_code"},
                )

        assert response.status_code == 400
        data = response.json()
        assert "无效" in str(data) or "过期" in str(data)

    def test_login_wechat_code_used(self):
        """微信返回 code 已被使用 (40163) 时返回 400。"""
        from parse_video_py.content_security import WxSecurityServiceError

        with patch.object(cs, "OPENID_SIGNING_KEY", "test_key"), \
             patch.object(self.aw, "OPENID_SIGNING_KEY", "test_key"), \
             patch.object(
                 self.aw,
                 "_call_code2session",
                 side_effect=WxSecurityServiceError(
                     "code2Session 失败: errcode=40163 errmsg=code been used",
                     errcode=40163,
                 ),
             ):
                response = self.client.post(
                    "/auth/wechat-login",
                    json={"code": "used_code"},
                )

        assert response.status_code == 400
        data = response.json()
        assert "无效" in str(data) or "过期" in str(data)

    def test_login_wechat_system_error(self):
        """微信返回系统错误 (-1) 时返回 502。"""
        from parse_video_py.content_security import WxSecurityServiceError

        with patch.object(cs, "OPENID_SIGNING_KEY", "test_key"), \
             patch.object(self.aw, "OPENID_SIGNING_KEY", "test_key"), \
             patch.object(
                 self.aw,
                 "_call_code2session",
                 side_effect=WxSecurityServiceError(
                     "code2Session 系统错误: errcode=-1",
                     errcode=-1,
                 ),
             ):
                response = self.client.post(
                    "/auth/wechat-login",
                    json={"code": "valid_looking_code"},
                )

        assert response.status_code == 502

    def test_login_wechat_timeout(self):
        """微信接口超时时返回 502。"""
        from parse_video_py.content_security import WxSecurityServiceError

        with patch.object(cs, "OPENID_SIGNING_KEY", "test_key"), \
             patch.object(self.aw, "OPENID_SIGNING_KEY", "test_key"), \
             patch.object(
                 self.aw,
                 "_call_code2session",
                 side_effect=WxSecurityServiceError(
                     "code2Session 请求超时（已重试 3 次）"
                 ),
             ):
                response = self.client.post(
                    "/auth/wechat-login",
                    json={"code": "valid_looking_code"},
                )

        assert response.status_code == 502

    # ------------------------------------------------------------------
    # 配置错误
    # ------------------------------------------------------------------

    def test_login_missing_appid(self):
        """WX_APPID 未配置时返回 503。"""
        with patch.object(self.aw, "WX_APPID", ""):
            with patch.object(self.aw, "WX_APPSECRET", "secret"):
                with patch.object(cs, "OPENID_SIGNING_KEY", "test_key"):
                    response = self.client.post(
                        "/auth/wechat-login",
                        json={"code": "some_code"},
                    )

        assert response.status_code == 503
        data = response.json()
        assert "WX_APPID" in str(data) or "配置" in str(data)

    def test_login_missing_appsecret(self):
        """WX_APPSECRET 未配置时返回 503。"""
        with patch.object(self.aw, "WX_APPID", "wx_app"):
            with patch.object(self.aw, "WX_APPSECRET", ""):
                with patch.object(cs, "OPENID_SIGNING_KEY", "test_key"):
                    response = self.client.post(
                        "/auth/wechat-login",
                        json={"code": "some_code"},
                    )

        assert response.status_code == 503

    def test_login_missing_signing_key(self):
        """OPENID_SIGNING_KEY 未配置时返回 503。"""
        mock_wx_response = {
            "openid": "o_test_user",
            "session_key": "sk_test",
            "errcode": 0,
        }

        with patch.object(self.aw, "WX_APPID", "wx_app"):
            with patch.object(self.aw, "WX_APPSECRET", "secret"):
                with patch.object(cs, "OPENID_SIGNING_KEY", ""):
                    with patch.object(
                        self.aw, "_call_code2session", return_value=mock_wx_response
                    ):
                        response = self.client.post(
                            "/auth/wechat-login",
                            json={"code": "valid_code"},
                        )

        assert response.status_code == 503
        data = response.json()
        assert "OPENID_SIGNING_KEY" in str(data) or "签发" in str(data)

    # ------------------------------------------------------------------
    # 微信返回异常数据
    # ------------------------------------------------------------------

    def test_login_missing_openid_in_wx_response(self):
        """微信返回数据缺少 openid 字段时返回 502。"""
        # code2Session 正常响应但无 openid（极端情况）
        mock_wx_response = {
            "errcode": 0,
            "errmsg": "ok",
            # 缺少 openid
        }

        with patch.object(cs, "OPENID_SIGNING_KEY", "test_key"), \
             patch.object(self.aw, "OPENID_SIGNING_KEY", "test_key"), \
             patch.object(
                 self.aw, "_call_code2session", return_value=mock_wx_response
             ):
                response = self.client.post(
                    "/auth/wechat-login",
                    json={"code": "weird_code"},
                )

        assert response.status_code == 502

    # ------------------------------------------------------------------
    # 跨服务 token 互通验证
    # ------------------------------------------------------------------

    def test_issued_token_verifiable_by_content_security(self):
        """auth_web 签发的 token 可被 content_security.verify_openid_token 验证。

        这确保文档和媒体服务可以使用同一个 OPENID_SIGNING_KEY 验证 token。
        """
        from parse_video_py.content_security import verify_openid_token

        test_openid = "o_cross_service_test"
        mock_wx_response = {
            "openid": test_openid,
            "session_key": "sk_test",
            "errcode": 0,
        }

        with patch.object(cs, "OPENID_SIGNING_KEY", "shared_signing_key_abc123"), \
             patch.object(self.aw, "OPENID_SIGNING_KEY", "shared_signing_key_abc123"), \
             patch.object(cs, "OPENID_TOKEN_TTL", 7200), \
             patch.object(self.aw, "OPENID_TOKEN_TTL", 7200), \
             patch.object(
                 self.aw, "_call_code2session", return_value=mock_wx_response
             ):
                    response = self.client.post(
                        "/auth/wechat-login",
                        json={"code": "valid_code"},
                    )

        assert response.status_code == 200
        token = response.json()["data"]["openidToken"]

        # 使用 content_security 模块的 verify_openid_token 验证
        with patch.object(cs, "OPENID_SIGNING_KEY", "shared_signing_key_abc123"):
            verified_openid = verify_openid_token(token)
            assert verified_openid == test_openid

    def test_token_verification_fails_with_different_key(self):
        """不同签名密钥签发的 token 验证失败。"""
        from parse_video_py.content_security import verify_openid_token

        mock_wx_response = {
            "openid": "o_user_test",
            "session_key": "sk_test",
            "errcode": 0,
        }

        # 使用密钥 A 签发
        with patch.object(cs, "OPENID_SIGNING_KEY", "signing_key_A"), \
             patch.object(self.aw, "OPENID_SIGNING_KEY", "signing_key_A"), \
             patch.object(
                 self.aw, "_call_code2session", return_value=mock_wx_response
             ):
                response = self.client.post(
                    "/auth/wechat-login",
                    json={"code": "valid_code"},
                )

        assert response.status_code == 200
        token = response.json()["data"]["openidToken"]

        # 使用密钥 B 验证 → 应失败
        with patch.object(cs, "OPENID_SIGNING_KEY", "signing_key_B"):
            with pytest.raises(ValueError, match="签名不匹配"):
                verify_openid_token(token)

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------

    def test_health_check(self):
        """健康检查端点正常。"""
        response = self.client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["service"] == "auth"
