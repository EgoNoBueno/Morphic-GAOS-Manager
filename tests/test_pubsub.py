"""tests/test_pubsub.py — Unit tests for tools/pubsub.py"""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from models import A2AMessage, MessageType
from tools.pubsub import (
    MessageDecodeError,
    MessageValidationError,
    PubSubAdminError,
    PubSubPublishError,
    TopicNotFoundError,
    _topic_path,
    decode_push_message,
    ensure_topic_exists,
    publish,
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


@pytest.fixture(autouse=True)
def load_test_settings(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(SETTINGS_YAML)
    import config

    config._reset_for_testing()
    config.load_settings(cfg)
    yield
    config._reset_for_testing()


# ── Sample A2AMessage ──────────────────────────────────────────────────────


@pytest.fixture()
def sample_message():
    return A2AMessage(
        project_id="default",
        source_agent="beacon",
        target_agent="pursuit",
        message_type=MessageType.TASK_HANDOFF,
        priority=2,
        payload={"lead_id": "lead-abc"},
    )


# ── _topic_path ────────────────────────────────────────────────────────────


class TestTopicPath:
    def test_replaces_slashes_with_dots(self):
        path = _topic_path("agent/beacon/events", "my-project")
        assert path == "projects/my-project/topics/agent.beacon.events"

    def test_dot_name_unchanged(self):
        path = _topic_path("agent.beacon.events", "my-project")
        assert path == "projects/my-project/topics/agent.beacon.events"


# ── publish ────────────────────────────────────────────────────────────────


class TestPublish:
    @patch("tools.pubsub.pubsub_v1.PublisherClient")
    def test_returns_message_id(self, mock_cls, sample_message):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_future = MagicMock()
        mock_future.result.return_value = "msg-id-123"
        mock_client.publish.return_value = mock_future

        result = publish("agent/beacon/events", sample_message)
        assert result == "msg-id-123"

    @patch("tools.pubsub.pubsub_v1.PublisherClient")
    def test_publishes_to_correct_topic(self, mock_cls, sample_message):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_future = MagicMock()
        mock_future.result.return_value = "msg-id"
        mock_client.publish.return_value = mock_future

        publish("agent/beacon/events", sample_message)
        call_args = mock_client.publish.call_args
        assert call_args[0][0] == "projects/test-project/topics/agent.beacon.events"

    @patch("tools.pubsub.pubsub_v1.PublisherClient")
    def test_raises_topic_not_found(self, mock_cls, sample_message):
        from google.api_core.exceptions import NotFound

        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.publish.side_effect = NotFound("topic not found")

        with pytest.raises(TopicNotFoundError, match="agent.beacon.events"):
            publish("agent/beacon/events", sample_message)

    @patch("tools.pubsub.pubsub_v1.PublisherClient")
    def test_raises_publish_error_on_unexpected(self, mock_cls, sample_message):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.publish.side_effect = RuntimeError("network down")

        with pytest.raises(PubSubPublishError, match="network down"):
            publish("agent/beacon/events", sample_message)

    @patch("tools.pubsub.pubsub_v1.PublisherClient")
    def test_payload_is_valid_json(self, mock_cls, sample_message):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_future = MagicMock()
        mock_future.result.return_value = "id"
        mock_client.publish.return_value = mock_future

        publish("agent/beacon/events", sample_message)
        data = mock_client.publish.call_args[1]["data"]
        parsed = json.loads(data.decode("utf-8"))
        assert parsed["source_agent"] == "beacon"
        assert parsed["project_id"] == "default"


# ── ensure_topic_exists ────────────────────────────────────────────────────


class TestEnsureTopicExists:
    @patch("tools.pubsub.pubsub_v1.PublisherClient")
    def test_creates_topic_if_absent(self, mock_cls):
        """get_topic raises NotFound → create_topic is called once."""
        from google.api_core.exceptions import NotFound

        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.get_topic.side_effect = NotFound("not found")
        ensure_topic_exists("agent/beacon/events")
        mock_client.create_topic.assert_called_once_with(
            request={"name": "projects/test-project/topics/agent.beacon.events"}
        )

    @patch("tools.pubsub.pubsub_v1.PublisherClient")
    def test_no_create_when_topic_exists(self, mock_cls):
        """get_topic succeeds → create_topic is never called (no API error charged)."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        ensure_topic_exists("agent/beacon/events")  # must not raise
        mock_client.create_topic.assert_not_called()

    @patch("tools.pubsub.pubsub_v1.PublisherClient")
    def test_idempotent_on_race_condition(self, mock_cls):
        """get_topic raises NotFound then create_topic races to AlreadyExists — must not raise."""
        from google.api_core.exceptions import AlreadyExists, NotFound

        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.get_topic.side_effect = NotFound("not found")
        mock_client.create_topic.side_effect = AlreadyExists("exists")
        ensure_topic_exists("agent/beacon/events")  # must not raise

    @patch("tools.pubsub.pubsub_v1.PublisherClient")
    def test_raises_admin_error_on_permission_failure(self, mock_cls):
        from google.api_core.exceptions import PermissionDenied

        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.get_topic.side_effect = PermissionDenied("denied")
        with pytest.raises(PubSubAdminError, match="agent.beacon.events"):
            ensure_topic_exists("agent/beacon/events")


# ── decode_push_message ────────────────────────────────────────────────────


class TestDecodePushMessage:
    def _make_envelope(self, payload: dict) -> dict:
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        return {
            "message": {"data": encoded, "messageId": "1234"},
            "subscription": "projects/test/subscriptions/sub",
        }

    def test_decodes_valid_envelope(self, sample_message):
        raw = json.loads(sample_message.model_dump_json())
        envelope = self._make_envelope(raw)
        result = decode_push_message(envelope)
        assert isinstance(result, A2AMessage)
        assert result.source_agent == "beacon"
        assert result.project_id == "default"

    def test_raises_decode_error_on_bad_base64(self):
        envelope = {
            "message": {"data": "!!!not-base64!!!", "messageId": "1"},
            "subscription": "sub",
        }
        with pytest.raises(MessageDecodeError):
            decode_push_message(envelope)

    def test_raises_decode_error_on_missing_message_key(self):
        with pytest.raises(MessageDecodeError):
            decode_push_message({"subscription": "sub"})

    def test_raises_validation_error_on_schema_mismatch(self):
        envelope = self._make_envelope({"not_a_message": True})
        with pytest.raises(MessageValidationError):
            decode_push_message(envelope)
