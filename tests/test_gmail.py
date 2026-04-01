"""
tests/test_gmail.py — Unit tests for tools/gmail.py.

All GCP/Gmail calls are mocked at the SDK boundary
(``googleapiclient.discovery.build`` and ``tools.secrets.get_secret``).
No live API calls are made.

Tests (9):
  test_fetch_new_messages_happy
  test_fetch_new_messages_empty
  test_fetch_new_messages_api_error
  test_get_thread_context_happy
  test_mark_as_read_happy
  test_send_email_happy
  test_send_email_with_thread_id
  test_send_email_api_error
  test_get_gmail_service_secret_missing
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ID = "test-project"
_CRED_JSON = json.dumps(
    {
        "client_id": "client-123",
        "client_secret": "secret-abc",
        "refresh_token": "token-xyz",
    }
)

# ── Fixture: mock credentials loaded from Secret Manager ─────────────────────


@pytest.fixture()
def mock_secret():
    """Patch get_secret to return a valid GMAIL_OAUTH_CREDENTIALS JSON blob."""
    with patch("tools.gmail.get_secret", return_value=_CRED_JSON):
        yield


@pytest.fixture()
def mock_creds(mock_secret):
    """Patch google.oauth2.credentials.Credentials and googleapiclient.discovery.build."""
    with (
        patch("tools.gmail.get_gmail_service") as mock_svc_fn,
    ):
        yield mock_svc_fn


# ── test_fetch_new_messages_happy ─────────────────────────────────────────────


def test_fetch_new_messages_happy(mock_secret):
    """Two messageAdded records → two parsed message dicts with correct fields."""
    import base64

    from tools.gmail import fetch_new_messages

    # Build a minimal Gmail API mock chain
    fake_service = MagicMock()
    fake_history = {
        "historyId": "9999",
        "history": [
            {"messagesAdded": [{"message": {"id": "msg-1"}}]},
            {"messagesAdded": [{"message": {"id": "msg-2"}}]},
        ],
    }
    fake_service.users().history().list(
        userId="me", startHistoryId="1000", historyTypes=["messageAdded"]
    ).execute.return_value = fake_history

    def _fake_msg_get(userId, id, format):  # noqa: A002
        plain_b64 = base64.urlsafe_b64encode(b"Hello from " + id.encode()).decode()
        msg = MagicMock()
        msg.execute.return_value = {
            "id": id,
            "threadId": f"thread-{id}",
            "internalDate": "1700000000000",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": f"{id}@example.com"},
                    {"name": "Subject", "value": f"Subject for {id}"},
                    {"name": "Message-ID", "value": f"<{id}@mail>"},
                ],
                "body": {"data": plain_b64},
                "parts": [],
            },
        }
        return msg

    fake_service.users().messages().get.side_effect = _fake_msg_get

    with patch("tools.gmail.get_gmail_service", return_value=fake_service):
        messages, new_id, skipped_ids = fetch_new_messages(PROJECT_ID, "1000")

    assert len(messages) == 2
    assert new_id == "9999"
    assert skipped_ids == []
    assert messages[0]["message_id"] == "msg-1"
    assert messages[1]["message_id"] == "msg-2"
    assert "from_addr" in messages[0]
    assert "subject" in messages[0]
    assert "body" in messages[0]
    assert "received_at" in messages[0]


# ── test_fetch_new_messages_empty ─────────────────────────────────────────────


def test_fetch_new_messages_empty(mock_secret):
    """No messagesAdded in history → empty list and same history_id returned."""
    from tools.gmail import fetch_new_messages

    fake_service = MagicMock()
    fake_service.users().history().list().execute.return_value = {
        "historyId": "1234",
        "history": [],
    }

    with patch("tools.gmail.get_gmail_service", return_value=fake_service):
        messages, new_id, skipped_ids = fetch_new_messages(PROJECT_ID, "1000")

    assert messages == []
    assert new_id == "1234"
    assert skipped_ids == []


# ── test_fetch_new_messages_api_error ─────────────────────────────────────────


def test_fetch_new_messages_api_error(mock_secret):
    """Gmail history.list raises HttpError → GmailAPIError raised."""
    from googleapiclient.errors import HttpError

    from tools.gmail import GmailAPIError, fetch_new_messages

    fake_resp = MagicMock()
    fake_resp.status = 403
    fake_service = MagicMock()
    fake_service.users().history().list().execute.side_effect = HttpError(
        resp=fake_resp, content=b"Forbidden"
    )

    with patch("tools.gmail.get_gmail_service", return_value=fake_service):
        with pytest.raises(GmailAPIError):
            fetch_new_messages(PROJECT_ID, "1000")


# ── test_fetch_new_messages_404_skipped ───────────────────────────────────────


def test_fetch_new_messages_404_skipped(mock_secret):
    """Permanently-404 message is skipped; history watermark still advances."""
    import base64

    from googleapiclient.errors import HttpError

    from tools.gmail import fetch_new_messages

    fake_service = MagicMock()
    fake_history = {
        "historyId": "5555",
        "history": [
            {"messagesAdded": [{"message": {"id": "dead-msg"}}]},
            {"messagesAdded": [{"message": {"id": "live-msg"}}]},
        ],
    }
    fake_service.users().history().list(
        userId="me", startHistoryId="1000", historyTypes=["messageAdded"]
    ).execute.return_value = fake_history

    fake_404_resp = MagicMock()
    fake_404_resp.status = 404

    plain_b64 = base64.urlsafe_b64encode(b"Hello").decode()

    def _fake_msg_get(userId, id, format):  # noqa: A002
        if id == "dead-msg":
            mock = MagicMock()
            mock.execute.side_effect = HttpError(resp=fake_404_resp, content=b"Not Found")
            return mock
        mock = MagicMock()
        mock.execute.return_value = {
            "id": id,
            "threadId": "thread-1",
            "internalDate": "1700000000000",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "sender@example.com"},
                    {"name": "Subject", "value": "Live"},
                    {"name": "Message-ID", "value": "<live@mail>"},
                ],
                "body": {"data": plain_b64},
                "parts": [],
            },
        }
        return mock

    fake_service.users().messages().get.side_effect = _fake_msg_get

    with patch("tools.gmail.get_gmail_service", return_value=fake_service):
        messages, new_id, skipped_ids = fetch_new_messages(PROJECT_ID, "1000")

    # Dead message skipped; live message returned; watermark advanced
    assert len(messages) == 1
    assert messages[0]["message_id"] == "live-msg"
    assert new_id == "5555"
    assert skipped_ids == ["dead-msg"]


# ── test_get_thread_context_happy ─────────────────────────────────────────────


def test_get_thread_context_happy(mock_secret):
    """Thread with 5 messages → only last 3 returned (default max_messages=3)."""
    import base64

    from tools.gmail import get_thread_context

    def _make_msg(idx: int) -> dict:
        plain_b64 = base64.urlsafe_b64encode(f"message {idx}".encode()).decode()
        return {
            "id": f"m{idx}",
            "threadId": "thread-abc",
            "internalDate": str(1700000000000 + idx * 1000),
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "sender@example.com"},
                    {"name": "Subject", "value": "Test"},
                    {"name": "Message-ID", "value": f"<m{idx}@mail>"},
                ],
                "body": {"data": plain_b64},
                "parts": [],
            },
        }

    fake_service = MagicMock()
    fake_service.users().threads().get().execute.return_value = {
        "messages": [_make_msg(i) for i in range(5)]
    }

    with patch("tools.gmail.get_gmail_service", return_value=fake_service):
        result = get_thread_context(PROJECT_ID, "thread-abc")

    assert len(result) == 3
    # Should be the last 3 messages (indices 2, 3, 4)
    assert result[-1]["message_id"] == "m4"


# ── test_mark_as_read_happy ───────────────────────────────────────────────────


def test_mark_as_read_happy(mock_secret):
    """mark_as_read calls messages.modify with removeLabelIds=["UNREAD"]."""
    from tools.gmail import mark_as_read

    fake_service = MagicMock()

    with patch("tools.gmail.get_gmail_service", return_value=fake_service):
        mark_as_read(PROJECT_ID, "msg-abc")

    call_kwargs = fake_service.users().messages().modify.call_args[1]
    assert call_kwargs["id"] == "msg-abc"
    assert "UNREAD" in call_kwargs["body"]["removeLabelIds"]


# ── test_send_email_happy ─────────────────────────────────────────────────────


def test_send_email_happy(mock_secret):
    """send_email returns the sent message_id from the API response."""
    from tools.gmail import send_email

    fake_service = MagicMock()
    fake_service.users().messages().send().execute.return_value = {"id": "sent-id-999"}

    with patch("tools.gmail.get_gmail_service", return_value=fake_service):
        result = send_email(PROJECT_ID, "to@example.com", "Hello", "Body text")

    assert result == "sent-id-999"
    call_kwargs = fake_service.users().messages().send.call_args[1]
    assert "raw" in call_kwargs["body"]


# ── test_send_email_with_thread_id ────────────────────────────────────────────


def test_send_email_with_thread_id(mock_secret):
    """send_email with thread_id includes threadId in body and sets In-Reply-To."""
    import base64
    import email as _email_lib

    from tools.gmail import send_email

    fake_service = MagicMock()
    fake_service.users().messages().send().execute.return_value = {"id": "sent-456"}

    with patch("tools.gmail.get_gmail_service", return_value=fake_service):
        send_email(
            PROJECT_ID,
            "to@example.com",
            "Re: Test",
            "Reply body",
            thread_id="thread-xyz",
            in_reply_to="<original@mail>",
        )

    call_kwargs = fake_service.users().messages().send.call_args[1]
    body = call_kwargs["body"]
    assert body.get("threadId") == "thread-xyz"

    # Decode raw bytes and check headers
    raw_bytes = base64.urlsafe_b64decode(body["raw"] + "==")
    msg = _email_lib.message_from_bytes(raw_bytes)
    assert msg["In-Reply-To"] == "<original@mail>"


# ── test_send_email_with_from_addr ────────────────────────────────────────────


def test_send_email_with_from_addr(mock_secret):
    """from_addr sets the From header in the MIME message."""
    import base64
    import email as _email_lib

    from tools.gmail import send_email

    fake_service = MagicMock()
    fake_service.users().messages().send().execute.return_value = {"id": "sent-789"}

    with patch("tools.gmail.get_gmail_service", return_value=fake_service):
        send_email(
            PROJECT_ID,
            "customer@example.com",
            "Hello",
            "Body text",
            from_addr="aos@sl10repairtechs.com",
        )

    call_kwargs = fake_service.users().messages().send.call_args[1]
    raw_bytes = base64.urlsafe_b64decode(call_kwargs["body"]["raw"] + "==")
    msg = _email_lib.message_from_bytes(raw_bytes)
    assert msg["From"] == "aos@sl10repairtechs.com"


# ── test_send_email_api_error ─────────────────────────────────────────────────


def test_send_email_api_error(mock_secret):
    """HttpError from messages.send → GmailAPIError raised."""
    from googleapiclient.errors import HttpError

    from tools.gmail import GmailAPIError, send_email

    fake_resp = MagicMock()
    fake_resp.status = 500
    fake_service = MagicMock()
    fake_service.users().messages().send().execute.side_effect = HttpError(
        resp=fake_resp, content=b"Server Error"
    )

    with patch("tools.gmail.get_gmail_service", return_value=fake_service):
        with pytest.raises(GmailAPIError):
            send_email(PROJECT_ID, "to@example.com", "Fail", "body")


# ── test_get_gmail_service_secret_missing ─────────────────────────────────────


def test_get_gmail_service_secret_missing():
    """SecretNotFoundError from get_secret → GmailAuthError raised."""
    from tools.gmail import GmailAuthError, get_gmail_service
    from tools.secrets import SecretNotFoundError

    with patch(
        "tools.gmail.get_secret", side_effect=SecretNotFoundError("GMAIL_OAUTH_CREDENTIALS")
    ):
        with pytest.raises(GmailAuthError):
            get_gmail_service(PROJECT_ID)
