"""
tools/gmail.py — Gmail API integration for Morphic-G AOS.

Provides inbox polling via Gmail history deltas, thread context retrieval,
outbound email sending, and watch registration for push notifications.

All functions accept an explicit project_id for secret scoping.
OAuth2 credentials are fetched at call-time from Secret Manager — no
global service instance is shared between requests.

Secret required: GMAIL_OAUTH_CREDENTIALS
  A JSON blob: {"client_id": "...", "client_secret": "...", "refresh_token": "..."}
  Scopes required: gmail.modify + gmail.send

Spec: Docs/email-comm-plan.md
"""

from __future__ import annotations

import base64
import email.mime.text
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from tools.secrets import SecretNotFoundError, get_secret

_gmail_log = logging.getLogger(__name__)

# ── Error types ────────────────────────────────────────────────────────────


class GmailAuthError(Exception):
    """Secret missing, token invalid, or OAuth scope insufficient."""


class GmailAPIError(Exception):
    """Gmail REST API returned a non-retryable error."""


# ── Internal helpers ───────────────────────────────────────────────────────

_GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


def _load_credentials(project_id: str) -> Any:
    """
    Load OAuth2 credentials from Secret Manager.

    Args:
        project_id: GCP project that owns the GMAIL_OAUTH_CREDENTIALS secret.

    Returns:
        google.oauth2.credentials.Credentials instance.

    Raises:
        GmailAuthError: Secret missing or JSON malformed.
    """
    try:
        raw = get_secret("GMAIL_OAUTH_CREDENTIALS", project_id)
    except SecretNotFoundError as exc:
        raise GmailAuthError(
            "GMAIL_OAUTH_CREDENTIALS not found in Secret Manager. "
            "Run scripts/setup_gmail_oauth.py to generate and store credentials."
        ) from exc
    except Exception as exc:
        raise GmailAuthError(f"Failed to load Gmail credentials: {exc}") from exc

    try:
        cred_dict = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GmailAuthError(f"GMAIL_OAUTH_CREDENTIALS is not valid JSON: {exc}") from exc

    try:
        from google.oauth2.credentials import Credentials

        return Credentials(
            token=None,
            refresh_token=cred_dict["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=cred_dict["client_id"],
            client_secret=cred_dict["client_secret"],
            scopes=_GMAIL_SCOPES,
        )
    except KeyError as exc:
        raise GmailAuthError(
            f"GMAIL_OAUTH_CREDENTIALS JSON is missing required key: {exc}. "
            "Expected keys: client_id, client_secret, refresh_token."
        ) from exc


def _extract_plain_text(payload: dict) -> str:
    """
    Recursively extract text/plain body from a Gmail message payload.
    Never returns text/html — avoids passing markup noise to the LLM.

    Args:
        payload: The ``payload`` dict from a Gmail messages.get() response.

    Returns:
        Decoded plain-text body, or empty string if not found.
    """
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if mime_type == "text/plain" and body_data:
        try:
            return base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")
        except Exception:
            return ""

    for part in payload.get("parts", []):
        result = _extract_plain_text(part)
        if result:
            return result

    return ""


def _parse_headers(headers: list[dict]) -> dict[str, str]:
    """
    Convert the Gmail headers list into a {name: value} dict.

    Args:
        headers: List of {"name": str, "value": str} dicts from Gmail API.

    Returns:
        Dict with header names as keys (case-preserved).
    """
    return {h["name"]: h["value"] for h in headers if "name" in h and "value" in h}


# ── Public API ─────────────────────────────────────────────────────────────


def get_gmail_service(project_id: str) -> Any:
    """
    Build and return an authenticated Gmail API service object.

    Fetches OAuth2 credentials from Secret Manager on every call — no
    global state. The returned service object is safe to use for one request
    and should not be cached across requests.

    Args:
        project_id: GCP project that owns the GMAIL_OAUTH_CREDENTIALS secret.

    Returns:
        A ``googleapiclient.discovery.Resource`` for the Gmail v1 API.

    Raises:
        GmailAuthError: Credentials missing, malformed, or OAuth scope
            insufficient.
    """
    from googleapiclient.discovery import build

    creds = _load_credentials(project_id)
    try:
        return build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as exc:
        raise GmailAuthError(f"Failed to build Gmail service: {exc}") from exc


def fetch_new_messages(
    project_id: str,
    history_id: str,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    """
    Fetch all messages added since ``history_id`` using the history delta API.

    Calls ``users.history.list(startHistoryId=history_id)`` to retrieve only
    the delta since the last processed ID — far more efficient than listing
    UNREAD messages and avoids reprocessing. Extracts ``text/plain`` body
    parts only; never parses ``text/html`` to avoid passing markup noise to
    the LLM.

    Args:
        project_id: GCP project for credential and secret lookup.
        history_id: The ``historyId`` from the last successfully processed
            Gmail push notification. Use the initial watch ``historyId`` as
            the seed on first run.

    Returns:
        A tuple of ``(messages, new_history_id, skipped_ids)`` where:
        - ``messages`` is a list of dicts, each with keys:
          ``message_id``, ``thread_id``, ``from_addr``, ``subject``,
          ``body``, ``received_at`` (ISO-8601 UTC string).
        - ``new_history_id`` is the latest ``historyId`` from the API
          response, to persist in the System_State Sheet.
        - ``skipped_ids`` is a list of Gmail message IDs that were
          permanently missing (404 after 3 retries) and skipped.

    Raises:
        GmailAuthError: Credential fetch failed.
        GmailAPIError:  Gmail API returned an error.
    """
    from googleapiclient.errors import HttpError

    service = get_gmail_service(project_id)
    try:
        history_resp = (
            service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=history_id,
                historyTypes=["messageAdded"],
            )
            .execute()
        )
    except HttpError as exc:
        raise GmailAPIError(
            f"Gmail history.list failed (startHistoryId={history_id}): {exc}"
        ) from exc
    except Exception as exc:
        raise GmailAPIError(f"Unexpected Gmail API error in fetch_new_messages: {exc}") from exc

    new_history_id: str = str(history_resp.get("historyId", history_id))
    history_records: list[dict] = history_resp.get("history", [])

    if not history_records:
        return [], new_history_id, []

    # Collect unique message IDs from all messageAdded records.
    seen_ids: set[str] = set()
    message_ids: list[str] = []
    for record in history_records:
        for added in record.get("messagesAdded", []):
            mid = added.get("message", {}).get("id", "")
            if mid and mid not in seen_ids:
                seen_ids.add(mid)
                message_ids.append(mid)

    messages: list[dict[str, Any]] = []
    skipped_ids: list[str] = []
    for mid in message_ids:
        msg = None
        for attempt in range(3):
            try:
                msg = service.users().messages().get(userId="me", id=mid, format="full").execute()
                break
            except HttpError as exc:
                if exc.resp.status == 404 and attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if exc.resp.status == 404:
                    # Message was deleted/archived before we could fetch it.
                    # Skip it so the history watermark can still advance.
                    break
                raise GmailAPIError(f"Gmail messages.get failed for {mid}: {exc}") from exc
        if msg is None:
            # Permanently missing after 3 attempts — record and skip.
            skipped_ids.append(mid)
            _gmail_log.warning(
                "fetch_new_messages: message %s not found after 3 attempts (404) — "
                "skipped. Message was likely deleted/archived before fetch.",
                mid,
            )
            continue

        headers = _parse_headers(msg.get("payload", {}).get("headers", []))
        received_ts = datetime.fromtimestamp(
            int(msg.get("internalDate", 0)) / 1000, tz=UTC
        ).isoformat()

        messages.append(
            {
                "message_id": msg["id"],
                "thread_id": msg.get("threadId", ""),
                "from_addr": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "body": _extract_plain_text(msg.get("payload", {})),
                "received_at": received_ts,
                "message_id_header": headers.get("Message-ID", ""),
            }
        )

    return messages, new_history_id, skipped_ids


def get_thread_context(
    project_id: str,
    thread_id: str,
    max_messages: int = 3,
) -> list[dict[str, Any]]:
    """
    Return the last ``max_messages`` exchanges in a Gmail thread.

    Fetches the full thread and extracts the last N messages as plain text,
    oldest first. Used by the ``process_gmail_notification`` LangGraph node
    to give the LLM Institutional Memory of the conversation before composing
    a reply.

    Args:
        project_id:   GCP project for credential lookup.
        thread_id:    The Gmail thread ID to retrieve.
        max_messages: Maximum number of messages to return (default 3).
                      Increase for long-running threads where more context
                      improves reply quality.

    Returns:
        List of message dicts (same shape as fetch_new_messages output),
        ordered oldest → newest, capped at ``max_messages``.

    Raises:
        GmailAuthError: Credential fetch failed.
        GmailAPIError:  Gmail API returned an error.
    """
    from googleapiclient.errors import HttpError

    service = get_gmail_service(project_id)
    try:
        thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    except HttpError as exc:
        raise GmailAPIError(f"Gmail threads.get failed for thread {thread_id}: {exc}") from exc
    except Exception as exc:
        raise GmailAPIError(f"Unexpected error fetching thread {thread_id}: {exc}") from exc

    raw_messages: list[dict] = thread.get("messages", [])
    # Take the last max_messages messages (most recent context).
    recent = raw_messages[-max_messages:] if len(raw_messages) > max_messages else raw_messages

    result: list[dict[str, Any]] = []
    for msg in recent:
        headers = _parse_headers(msg.get("payload", {}).get("headers", []))
        received_ts = datetime.fromtimestamp(
            int(msg.get("internalDate", 0)) / 1000, tz=UTC
        ).isoformat()
        result.append(
            {
                "message_id": msg["id"],
                "thread_id": thread_id,
                "from_addr": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "body": _extract_plain_text(msg.get("payload", {})),
                "received_at": received_ts,
                "message_id_header": headers.get("Message-ID", ""),
            }
        )

    return result


def mark_as_read(project_id: str, message_id: str) -> None:
    """
    Remove the UNREAD label from a Gmail message.

    Called after each successfully processed message to signal that the
    agent has consumed it. This prevents reprocessing if the watch fires
    again before the historyId is updated.

    Args:
        project_id: GCP project for credential lookup.
        message_id: The Gmail message ID to mark as read.

    Raises:
        GmailAuthError: Credential fetch failed.
        GmailAPIError:  Gmail API returned an error.
    """
    from googleapiclient.errors import HttpError

    service = get_gmail_service(project_id)
    try:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
    except HttpError as exc:
        raise GmailAPIError(
            f"Gmail messages.modify (mark_as_read) failed for {message_id}: {exc}"
        ) from exc
    except Exception as exc:
        raise GmailAPIError(f"Unexpected error in mark_as_read for {message_id}: {exc}") from exc


def send_email(
    project_id: str,
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
    in_reply_to: str | None = None,
    from_addr: str | None = None,
) -> str:
    """
    Send an email via the Gmail API.

    Sets ``In-Reply-To`` and ``References`` headers when ``in_reply_to``
    is provided, keeping the reply coherent in the recipient's inbox client.

    Args:
        project_id:  GCP project for credential lookup.
        to:          Recipient email address.
        subject:     Email subject line.
        body:        Plain-text email body.
        thread_id:   Gmail thread ID to reply within. When set, the sent
                     message is appended to the existing thread.
        in_reply_to: The ``Message-ID`` header value of the message being
                     replied to. Sets ``In-Reply-To`` and ``References``
                     headers for proper thread coherence.
        from_addr:   Override the ``From`` header. Must be a verified "Send
                     mail as" alias configured in Gmail settings. When
                     omitted, Gmail sends from the authenticated account.
                     Use ``settings.gmail.sender_address`` to set this.

    Returns:
        The Gmail message ID of the sent message.

    Raises:
        GmailAuthError: Credential fetch failed.
        GmailAPIError:  Gmail API returned an error.
    """
    from googleapiclient.errors import HttpError

    service = get_gmail_service(project_id)

    mime_msg = email.mime.text.MIMEText(body, "plain", "utf-8")
    mime_msg["To"] = to
    mime_msg["Subject"] = subject
    if from_addr:
        mime_msg["From"] = from_addr
    if in_reply_to:
        mime_msg["In-Reply-To"] = in_reply_to
        mime_msg["References"] = in_reply_to

    raw_bytes = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode("utf-8")
    send_body: dict[str, Any] = {"raw": raw_bytes}
    if thread_id:
        send_body["threadId"] = thread_id

    try:
        sent = service.users().messages().send(userId="me", body=send_body).execute()
        return str(sent["id"])
    except HttpError as exc:
        raise GmailAPIError(f"Gmail messages.send failed (to={to!r}): {exc}") from exc
    except Exception as exc:
        raise GmailAPIError(f"Unexpected error in send_email to {to!r}: {exc}") from exc


def setup_watch(project_id: str, topic_name: str, label_id: str) -> tuple[str, str]:
    """
    Register or renew a Gmail push watch for the given label.

    Gmail watch() tells Google to publish a Pub/Sub notification to
    ``topic_name`` whenever a message matching ``label_id`` changes.
    The watch expires every 7 days — renewal is mandatory. Call this
    at deploy time and renew via Cloud Scheduler (POST /gmail-renew-watch,
    every 23 hours).

    Args:
        project_id: GCP project for credential lookup.
        topic_name: Fully-qualified Pub/Sub topic resource name, e.g.
            ``projects/my-project/topics/gmail-notifications``.
        label_id:   Gmail label ID to monitor (e.g. ``Label_1234567890``).
                    Obtain from scripts/setup_gmail_oauth.py.

    Returns:
        A tuple of ``(expiration_ms_str, history_id_str)`` where
        ``expiration_ms_str`` is the watch expiration as epoch milliseconds
        and ``history_id_str`` is the current Gmail historyId at registration
        time. Store expiration under ``gmail_watch_expiration`` and
        history_id under ``gmail_last_history_id`` in the System_State Sheet
        so the first push notification has a valid prior watermark.

    Raises:
        GmailAuthError: Credential fetch failed.
        GmailAPIError:  Gmail API returned an error.
    """
    from googleapiclient.errors import HttpError

    service = get_gmail_service(project_id)
    try:
        response = (
            service.users()
            .watch(
                userId="me",
                body={
                    "topicName": topic_name,
                    "labelIds": ["INBOX"],
                    "labelFilterBehavior": "INCLUDE",
                },
            )
            .execute()
        )
        return str(response.get("expiration", "")), str(response.get("historyId", ""))
    except HttpError as exc:
        raise GmailAPIError(f"Gmail watch() failed: {exc}") from exc
    except Exception as exc:
        raise GmailAPIError(f"Unexpected error in setup_watch: {exc}") from exc
