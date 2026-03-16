"""tests/test_webhook_sender.py — Unit tests for tools/webhook_sender.py"""
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from tools.webhook_sender import (
    WebhookDeliveryError,
    WebhookTimeoutError,
    WebhookURLError,
    post_to_webhook,
)


# ── Settings fixture ───────────────────────────────────────────────────────

SETTINGS_YAML = """\
gcp:
  project_id: test-project
  region: us-central1
sheet:
  workbook_id: spreadsheet-123
models:
  LOCAL_MODEL: ollama/llama3.1
  FAST_MODEL: gemini-2.0-flash
  DEEP_MODEL: gemini-2.0-pro
  LOCAL_MODEL_FALLBACK: gemini-2.0-flash
  LOCAL_MODEL_TIMEOUT_SECONDS: 2
projects:
  default:
    sheet_id: spreadsheet-123
    drive_folder_id: folder-abc
"""

_HMAC_SECRET = "test-hmac-secret-abc"
_WEBHOOK_URL = "https://script.google.com/macros/s/AKfycb/exec"


@pytest.fixture(autouse=True)
def load_test_settings(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(SETTINGS_YAML)
    import config

    config._reset_for_testing()
    config.load_settings(cfg)
    yield
    config._reset_for_testing()


@pytest.fixture()
def mock_secrets():
    """Return fake WEBHOOK_URL and WEBHOOK_HMAC_SECRET for all tests."""

    def _get_secret(name, *_):
        return _WEBHOOK_URL if name == "WEBHOOK_URL" else _HMAC_SECRET

    with patch("tools.webhook_sender.get_secret", side_effect=_get_secret):
        yield


# ── HMAC signature ─────────────────────────────────────────────────────────


class TestHmacSignature:
    def test_x_aos_signature_header_matches_computed_hmac(self, mock_secrets):
        payload = {"agent_id": "beacon", "status": "ok"}
        captured: dict = {}

        def _fake_post(url, *, content, headers, timeout):
            captured["headers"] = headers
            captured["body"] = content
            resp = MagicMock()
            resp.is_success = True
            return resp

        with patch("tools.webhook_sender.httpx.post", side_effect=_fake_post):
            post_to_webhook(payload, "test-project")

        expected_sig = hmac.new(
            _HMAC_SECRET.encode(), captured["body"], hashlib.sha256
        ).hexdigest()
        assert captured["headers"]["X-AOS-Signature"] == expected_sig

    def test_x_aos_project_id_header_is_set(self, mock_secrets):
        captured: dict = {}

        def _fake_post(url, *, content, headers, timeout):
            captured["headers"] = headers
            resp = MagicMock()
            resp.is_success = True
            return resp

        with patch("tools.webhook_sender.httpx.post", side_effect=_fake_post):
            post_to_webhook({"k": "v"}, "my-proj")

        assert captured["headers"]["X-AOS-Project-ID"] == "my-proj"

    def test_code_sha256_added_before_signing_when_proposed_code_present(self, mock_secrets):
        code = "print('hello world')"
        payload = {"Proposed Code": code}
        captured: dict = {}

        def _fake_post(url, *, content, headers, timeout):
            captured["body"] = content
            resp = MagicMock()
            resp.is_success = True
            return resp

        with patch("tools.webhook_sender.httpx.post", side_effect=_fake_post):
            post_to_webhook(payload, "test-project")

        sent = json.loads(captured["body"])
        expected_hash = hashlib.sha256(code.encode()).hexdigest()
        assert sent["code_sha256"] == expected_hash

    def test_caller_payload_dict_is_not_mutated(self, mock_secrets):
        payload = {"Proposed Code": "x = 1"}
        original_keys = set(payload.keys())
        resp = MagicMock()
        resp.is_success = True

        with patch("tools.webhook_sender.httpx.post", return_value=resp):
            post_to_webhook(payload, "test-project")

        # code_sha256 must NOT have been added to the caller's dict
        assert set(payload.keys()) == original_keys


# ── URL validation ─────────────────────────────────────────────────────────


class TestURLValidation:
    def test_http_url_raises_webhook_url_error(self):
        def _get(name, *_):
            return "http://example.com/webhook" if name == "WEBHOOK_URL" else _HMAC_SECRET

        with patch("tools.webhook_sender.get_secret", side_effect=_get):
            with pytest.raises(WebhookURLError, match="HTTPS"):
                post_to_webhook({"k": "v"}, "test-project")

    def test_private_ip_raises_webhook_url_error(self):
        def _get(name, *_):
            return "https://192.168.1.100/webhook" if name == "WEBHOOK_URL" else _HMAC_SECRET

        with patch("tools.webhook_sender.get_secret", side_effect=_get):
            with pytest.raises(WebhookURLError, match="private"):
                post_to_webhook({"k": "v"}, "test-project")

    def test_loopback_ip_raises_webhook_url_error(self):
        def _get(name, *_):
            return "https://127.0.0.1/webhook" if name == "WEBHOOK_URL" else _HMAC_SECRET

        with patch("tools.webhook_sender.get_secret", side_effect=_get):
            with pytest.raises(WebhookURLError, match="private"):
                post_to_webhook({"k": "v"}, "test-project")


# ── Retry and error handling ───────────────────────────────────────────────


class TestRetryAndErrorHandling:
    def test_4xx_response_raises_delivery_error_immediately(self, mock_secrets):
        resp = MagicMock()
        resp.is_success = False
        resp.status_code = 400
        resp.text = "Bad Request"

        with patch("tools.webhook_sender.httpx.post", return_value=resp):
            with pytest.raises(WebhookDeliveryError):
                post_to_webhook({"k": "v"}, "test-project")

    def test_4xx_is_not_retried(self, mock_secrets):
        resp = MagicMock()
        resp.is_success = False
        resp.status_code = 403
        resp.text = "Forbidden"

        with patch("tools.webhook_sender.httpx.post", return_value=resp) as mock_post:
            with pytest.raises(WebhookDeliveryError):
                post_to_webhook({"k": "v"}, "test-project")

        assert mock_post.call_count == 1

    def test_5xx_response_retried_up_to_max(self, mock_secrets):
        resp = MagicMock()
        resp.is_success = False
        resp.status_code = 503
        resp.text = "Service Unavailable"

        with patch("tools.webhook_sender.httpx.post", return_value=resp) as mock_post:
            with patch("tools.webhook_sender.time.sleep"):
                with pytest.raises(WebhookDeliveryError):
                    post_to_webhook({"k": "v"}, "test-project")

        assert mock_post.call_count == 3

    def test_timeout_raises_webhook_timeout_error(self, mock_secrets):
        with patch(
            "tools.webhook_sender.httpx.post",
            side_effect=httpx.TimeoutException("timed out"),
        ):
            with patch("tools.webhook_sender.time.sleep"):
                with pytest.raises(WebhookTimeoutError):
                    post_to_webhook({"k": "v"}, "test-project")

    def test_successful_response_returns_none(self, mock_secrets):
        resp = MagicMock()
        resp.is_success = True

        with patch("tools.webhook_sender.httpx.post", return_value=resp):
            result = post_to_webhook({"k": "v"}, "test-project")

        assert result is None
