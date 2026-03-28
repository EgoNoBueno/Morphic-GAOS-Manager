"""
tools/google_chat.py — Google Chat integration for Morphic-G AOS.

Provides:
  - send_message()          Send a plain-text message to a Chat space.
  - send_card()             Send a rich Card v2 to a Chat space.
  - send_approval_card()    Post an Approve/Reject card for the Approval Gate.
  - send_skill_import_card() Post a card requesting library installation approval.
  - send_reply_in_thread()  Reply in an existing thread via server-assigned thread name.
  - parse_chat_event()      Validate and parse an inbound Chat push payload.

Authentication:
  Uses a Google service account with the Chat API scope
  (chat.bot) via google-auth + google-api-python-client.
  The service account key path is loaded from settings (chat.service_account_key)
  or falls back to ADC.

Spec: GAOS-Manager-Spec.md §2.5 (Phase 2.5 — Conversation Layer)
"""

from __future__ import annotations

import html
import logging
from typing import Any

from google.auth.transport.requests import Request as AuthRequest
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import get_settings

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

_CHAT_SCOPES = ["https://www.googleapis.com/auth/chat.bot"]
_API_NAME = "chat"
_API_VERSION = "v1"

# ── Error types ─────────────────────────────────────────────────────────────


class ChatDeliveryError(Exception):
    """The Chat API returned a non-2xx response."""


class ChatConfigError(Exception):
    """Chat is not configured (missing space_id or credentials)."""


class ChatEventParseError(Exception):
    """Inbound Chat push payload could not be parsed."""


# ── Client factory ──────────────────────────────────────────────────────────


def _get_chat_service() -> Any:
    """
    Build and return an authenticated Google Chat API service client.

    Tries in this order:
    1. Service account key at settings.chat.service_account_key (JSON path).
    2. Application Default Credentials (ADC) — works in Cloud Run.

    Returns:
        A googleapiclient Resource for the Chat v1 API.
    """
    settings = get_settings()
    key_path: str = getattr(getattr(settings, "chat", None), "service_account_key", "") or ""

    if key_path:
        creds = service_account.Credentials.from_service_account_file(key_path, scopes=_CHAT_SCOPES)
    else:
        import google.auth

        creds, _ = google.auth.default(scopes=_CHAT_SCOPES)

    creds.refresh(AuthRequest())
    return build(_API_NAME, _API_VERSION, credentials=creds, cache_discovery=False)


# ── Public API ──────────────────────────────────────────────────────────────


def send_message(space_name: str, text: str) -> dict:
    """
    Send a plain-text message to a Google Chat space.

    Args:
        space_name: The Chat space resource name, e.g. ``spaces/XXXXXXXXX``.
        text:       Plain-text message body (≤ 4096 characters).

    Returns:
        The Chat API Message resource as a dict.

    Raises:
        ChatConfigError:   space_name is empty.
        ChatDeliveryError: Chat API returned an error.
    """
    if not space_name:
        raise ChatConfigError("space_name must not be empty.")
    if len(text) > 4096:
        text = text[:4093] + "..."

    service = _get_chat_service()
    try:
        result = (
            service.spaces().messages().create(parent=space_name, body={"text": text}).execute()
        )
        log.info("Chat message sent to %s: messageId=%s", space_name, result.get("name"))
        return result
    except HttpError as exc:
        raise ChatDeliveryError(
            f"Chat API error {exc.status_code} sending message to {space_name}: {exc.reason}"
        ) from exc


def send_threaded_reply(space_name: str, thread_key: str, text: str) -> dict:
    """Send a reply that stays inside a specific thread using a developer-chosen thread key.

    Falls back to creating a new thread if no existing thread with the given key exists.
    This is the preferred function for Chat responses — it keeps all messages in a single
    conversation thread rather than creating a new top-level thread per reply.

    Args:
        space_name: The Chat space resource name (e.g. "spaces/XXXXXXXXX").
        thread_key: A stable developer-chosen string identifying the thread.
            Good values: the original ``message_name`` from the inbound Chat event,
            or computed keys like ``f"approval-{proposal_id}"``.
        text: Plain-text message body (≤ 4096 characters; truncated if longer).

    Returns:
        The Chat API Message resource dict returned by the API.

    Raises:
        ChatConfigError: If ``space_name`` or ``thread_key`` is empty.
        ChatDeliveryError: If the Chat API returns an HTTP error.
    """
    if not space_name:
        raise ChatConfigError("space_name must not be empty.")
    if not thread_key:
        raise ChatConfigError("thread_key must not be empty.")
    if len(text) > 4096:
        text = text[:4093] + "..."

    service = _get_chat_service()
    try:
        result = (
            service.spaces()
            .messages()
            .create(
                parent=space_name,
                messageReplyOption="REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD",
                body={"text": text, "thread": {"threadKey": thread_key}},
            )
            .execute()
        )
        log.info(
            "Chat threaded reply sent to %s thread=%s: messageId=%s",
            space_name,
            thread_key,
            result.get("name"),
        )
        return result
    except HttpError as exc:
        raise ChatDeliveryError(
            f"Chat API error {exc.status_code} sending threaded reply to {space_name}: {exc.reason}"
        ) from exc


def send_reply_in_thread(space_name: str, thread_name: str, text: str) -> dict:
    """Send a reply in an existing Chat thread using the server-assigned thread resource name.

    Unlike ``send_threaded_reply()`` which uses a developer-assigned ``threadKey``,
    this function references the thread by its server-assigned resource name
    (e.g. ``"spaces/XXXXX/threads/YYYYY"``), ensuring the reply appears in the
    same thread as the user's original message rather than creating a new thread.

    Use this whenever the inbound Chat event includes ``message.thread.name``
    (available in ``parse_chat_event()`` as the ``thread_name`` key).

    Args:
        space_name:  The Chat space resource name (e.g. ``"spaces/XXXXXXXXX"``).
        thread_name: The server-assigned thread resource name
                     (e.g. ``"spaces/XXXXX/threads/YYYYY"``).
        text:        Plain-text message body (≤ 4096 characters; truncated if longer).

    Returns:
        The Chat API Message resource dict returned by the API.

    Raises:
        ChatConfigError:   If ``space_name`` or ``thread_name`` is empty.
        ChatDeliveryError: If the Chat API returns an HTTP error.
    """
    if not space_name:
        raise ChatConfigError("space_name must not be empty.")
    if not thread_name:
        raise ChatConfigError("thread_name must not be empty.")
    if len(text) > 4096:
        text = text[:4093] + "..."

    service = _get_chat_service()
    try:
        result = (
            service.spaces()
            .messages()
            .create(
                parent=space_name,
                messageReplyOption="REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD",
                body={"text": text, "thread": {"name": thread_name}},
            )
            .execute()
        )
        log.info(
            "Chat reply sent to %s in thread %s: messageId=%s",
            space_name,
            thread_name,
            result.get("name"),
        )
        return result
    except HttpError as exc:
        raise ChatDeliveryError(
            f"Chat API error {exc.status_code} replying in thread {thread_name}: {exc.reason}"
        ) from exc


def send_card(space_name: str, card: dict) -> dict:
    """
    Send a Card v2 message to a Chat space.

    Args:
        space_name: The Chat space resource name.
        card:       A fully-formed Card v2 dict following the Chat API schema.
                    Must have a ``header`` and at least one ``section``.

    Returns:
        The Chat API Message resource as a dict.

    Raises:
        ChatConfigError:   space_name is empty.
        ChatDeliveryError: Chat API returned an error.
    """
    if not space_name:
        raise ChatConfigError("space_name must not be empty.")

    body = {"cardsV2": [{"cardId": card.get("cardId", "card-1"), "card": card}]}
    service = _get_chat_service()
    try:
        result = service.spaces().messages().create(parent=space_name, body=body).execute()
        log.info("Chat card sent to %s: messageId=%s", space_name, result.get("name"))
        return result
    except HttpError as exc:
        raise ChatDeliveryError(
            f"Chat API error {exc.status_code} sending card to {space_name}: {exc.reason}"
        ) from exc


def send_approval_card(
    space_name: str,
    proposal_id: str,
    agent_id: str,
    issue_summary: str,
    proposed_action: str,
    priority: int,
    cost_usd: float,
    doc_url: str = "",
    reasoning_summary: str = "",
) -> dict:
    """
    Post an Approve / Reject card to the owner's Chat space for the Approval Gate.

    The card uses a Dual-Mode layout across up to three sections:

    **Section 1 — Context:** Agent ID, priority badge, estimated cost, and the
    issue summary.  Gives the approver immediate situational awareness.

    **Section 2 — Strategic Reasoning (conditional):** Rendered only when
    ``reasoning_summary`` is non-empty.  Displays the Strategic Architect's
    pre-task ``monologue_frame`` reasoning so the approver understands *why*
    the agent chose this response mode before seeing the technical proposal.

    **Section 3 — Decision:** The proposed action and Approve / Reject buttons.
    An optional "View Blueprint" button is added when ``doc_url`` is provided.

    Button clicks are delivered as interactive callbacks to ``POST /chat``
    with ``action.actionMethodName`` set to ``"approve"`` or ``"reject"``.

    Args:
        space_name:        Chat space resource name (e.g. ``spaces/XXXXXXX``).
        proposal_id:       The ApprovalProposal row ID (stored in col A of Agent_Approvals).
        agent_id:          The requesting agent (e.g. ``"beacon"``).
        issue_summary:     Plain-English description of what needs approval (≤ 280 chars).
        proposed_action:   What the agent proposes to do if approved (≤ 280 chars).
        priority:          Proposal priority (1–5).
        cost_usd:          Estimated cost if approved (0.0 if none).
        doc_url:           Optional URL to the Blueprint Google Doc for full context.
        reasoning_summary: Optional Strategic Architect reasoning from the think node's
                           ``monologue_frame``.  Shown as Section 2 when non-empty.

    Returns:
        The Chat API Message resource dict.

    Raises:
        ChatConfigError:   space_name or proposal_id is empty.
        ChatDeliveryError: Chat API returned an error.
    """
    if not space_name:
        raise ChatConfigError("space_name must not be empty.")
    if not proposal_id:
        raise ChatConfigError("proposal_id must not be empty.")

    # System convention: 1 = lowest severity, 5 = highest (opposite of ITIL P1-critical).
    priority_label = {
        1: "P1 Low",
        2: "P2 Info",
        3: "P3 Alert",
        4: "P4 Approval",
        5: "P5 Critical",
    }.get(priority, f"P{priority}")
    cost_text = f"${cost_usd:.4f}" if cost_usd > 0 else "No cost"

    # ── Section 1: Identity + Context ────────────────────────────────────────
    _issue = html.escape(issue_summary)
    section_context: dict = {
        "widgets": [
            {
                "textParagraph": {
                    "text": (
                        f"<b>🤖 Agent:</b> {html.escape(agent_id)}  |  "
                        f"<b>Priority:</b> {priority_label}  |  "
                        f"<b>Est. cost:</b> {cost_text}"
                    )
                }
            },
            {"textParagraph": {"text": f"<b>Issue:</b> {_issue}"}},
        ]
    }
    sections: list[dict] = [section_context]

    # ── Section 2: Strategic Architect Reasoning (conditional) ───────────────
    if reasoning_summary:
        sections.append(
            {
                "header": "🧠 Strategic Architect Reasoning",
                "widgets": [
                    {"textParagraph": {"text": html.escape(reasoning_summary)}},
                ],
            }
        )

    # ── Section 3: Proposed Action + Decision Buttons ────────────────────────
    buttons: list[dict] = []
    if doc_url:
        buttons.append(
            {
                "text": "View Blueprint",
                "onClick": {"openLink": {"url": doc_url}},
            }
        )
    buttons += [
        {
            "text": "✅ Approve",
            "onClick": {
                "action": {
                    "actionMethodName": "approve",
                    "parameters": [{"key": "proposal_id", "value": proposal_id}],
                }
            },
        },
        {
            "text": "❌ Reject",
            "onClick": {
                "action": {
                    "actionMethodName": "reject",
                    "parameters": [{"key": "proposal_id", "value": proposal_id}],
                }
            },
        },
    ]
    section_action: dict = {
        "header": "⚡ Decision Required",
        "widgets": [
            {"textParagraph": {"text": f"<b>Proposed action:</b> {html.escape(proposed_action)}"}},
            {"buttonList": {"buttons": buttons}},
        ],
    }
    sections.append(section_action)

    card: dict = {
        "cardId": f"approval-{proposal_id}",
        "header": {
            "title": "🤖 Morphic Agent — Approval Required",
            "subtitle": f"Proposal {proposal_id}",
        },
        "sections": sections,
    }

    return send_card(space_name, card)


def send_skill_import_card(
    space_name: str,
    proposal_id: str,
    agent_id: str,
    package_name: str,
    reason: str,
    pypi_url: str = "",
) -> dict:
    """
    Post a Skill Import approval card requesting permission to install a Python package.

    The owner can approve (pip install proceeds, logged to Sheet) or reject
    (agent falls back to existing capabilities).

    Args:
        space_name:   Chat space resource name.
        proposal_id:  The SKILL_REQUEST proposal ID.
        agent_id:     The agent requesting the package.
        package_name: The PyPI package name (e.g. ``"pandas"``).
        reason:       Why the agent needs this package (≤ 280 chars).
        pypi_url:     Optional link to the PyPI project page.

    Returns:
        The Chat API Message resource dict.

    Raises:
        ChatConfigError:   space_name or proposal_id is empty.
        ChatDeliveryError: Chat API returned an error.
    """
    if not space_name:
        raise ChatConfigError("space_name must not be empty.")
    if not proposal_id:
        raise ChatConfigError("proposal_id must not be empty.")
    if not package_name:
        raise ChatConfigError("package_name must not be empty.")

    widgets: list[dict] = [
        {"textParagraph": {"text": f"<b>Agent:</b> {html.escape(agent_id)}"}},
        {"textParagraph": {"text": f"<b>Package:</b> <code>{html.escape(package_name)}</code>"}},
        {"textParagraph": {"text": f"<b>Reason:</b> {html.escape(reason)}"}},
    ]

    buttons: list[dict] = []
    if pypi_url:
        buttons.append({"text": "View on PyPI", "onClick": {"openLink": {"url": pypi_url}}})
    buttons += [
        {
            "text": "✅ Install",
            "onClick": {
                "action": {
                    "actionMethodName": "skill_approve",
                    "parameters": [
                        {"key": "proposal_id", "value": proposal_id},
                        {"key": "package_name", "value": package_name},
                    ],
                }
            },
        },
        {
            "text": "❌ Deny",
            "onClick": {
                "action": {
                    "actionMethodName": "skill_reject",
                    "parameters": [
                        {"key": "proposal_id", "value": proposal_id},
                        {"key": "package_name", "value": package_name},
                    ],
                }
            },
        },
    ]
    widgets.append({"buttonList": {"buttons": buttons}})

    card: dict = {
        "cardId": f"skill-import-{proposal_id}",
        "header": {
            "title": "📦 Morphic Agent — Skill Import Request",
            "subtitle": f"Package: {package_name}",
        },
        "sections": [{"widgets": widgets}],
    }

    return send_card(space_name, card)


def send_infra_proposal_card(
    space_name: str,
    proposal_id: str,
    change_lines: list[str],
    irreversible_warning: str = "",
) -> dict:
    """Post an infrastructure change proposal card for owner approval.

    Designed for non-technical readers — uses plain language throughout.
    Button clicks deliver ``actionMethodName: "infra_approve"`` or
    ``"infra_reject"`` to ``POST /chat``.

    Args:
        space_name:           Chat space resource name.
        proposal_id:          Manifest proposal_id (stored in Agent_Approvals).
        change_lines:         Human-readable list of proposed changes (one per entry).
        irreversible_warning: Non-empty string triggers a ⚠️ warning section.
            Should concisely explain what cannot be undone automatically.

    Returns:
        The Chat API Message resource dict.

    Raises:
        ChatConfigError:   space_name or proposal_id is empty.
        ChatDeliveryError: Chat API returned an error.
    """
    if not space_name:
        raise ChatConfigError("space_name must not be empty.")
    if not proposal_id:
        raise ChatConfigError("proposal_id must not be empty.")

    sections: list[dict] = []

    # ── Section 1 — What will happen ─────────────────────────────────────────
    change_text = "<br>".join(
        f"• {html.escape(line)}" for line in (change_lines or ["No changes needed."])
    )
    sections.append(
        {
            "header": "What will happen if you approve",
            "widgets": [{"textParagraph": {"text": change_text}}],
        }
    )

    # ── Section 2 — Irreversible warning (conditional) ───────────────────────
    if irreversible_warning:
        sections.append(
            {
                "header": "⚠️  Warning — cannot be undone automatically",
                "widgets": [{"textParagraph": {"text": html.escape(irreversible_warning)}}],
            }
        )

    # ── Section 3 — Rollback assurance + decision buttons ────────────────────
    assurance = (
        "If you approve, GAOS will verify that everything worked. "
        "If any step fails, it will undo the changes automatically."
    )
    buttons = [
        {
            "text": "✅ Approve Changes",
            "onClick": {
                "action": {
                    "actionMethodName": "infra_approve",
                    "parameters": [{"key": "proposal_id", "value": proposal_id}],
                }
            },
        },
        {
            "text": "❌ Reject",
            "onClick": {
                "action": {
                    "actionMethodName": "infra_reject",
                    "parameters": [{"key": "proposal_id", "value": proposal_id}],
                }
            },
        },
    ]
    sections.append(
        {
            "widgets": [
                {"textParagraph": {"text": assurance}},
                {"buttonList": {"buttons": buttons}},
            ]
        }
    )

    card: dict = {
        "cardId": f"infra-proposal-{proposal_id}",
        "header": {
            "title": "🔧 GAOS wants to make infrastructure changes",
            "subtitle": f"Proposal {proposal_id[:8]}…",
        },
        "sections": sections,
    }
    return send_card(space_name, card)


def parse_chat_event(body: dict) -> dict:
    """
    Validate and normalise an inbound Google Chat push event.

    Supports TWO payload formats:

    1. **Legacy Chat Bot API** — root fields: ``type``, ``space``, ``user``, ``message``
    2. **Workspace Add-ons format** — ``commonEventObject``, ``chat.messagePayload``, etc.

    This function detects the format and extracts the fields GAOS needs into a flat dict:

    .. code-block:: python

        {
            "event_type":   str,          # MESSAGE | CARD_CLICKED | ...
            "space_name":   str,          # e.g. "spaces/XXXXXXX"
            "sender_email": str,          # sender's Google account email
            "text":         str,          # message text (empty for CARD_CLICKED)
            "action_name":  str,          # button actionMethodName (CARD_CLICKED only)
            "parameters":   dict[str,str],# button parameters keyed by "key"
            "message_name": str,          # Chat message resource name
            "thread_name":  str,          # server-assigned thread resource name (may be "")
            "attachments":  list[dict],   # attachment metadata (MESSAGE only)
        }

    Args:
        body: The raw JSON body dict from the Chat push endpoint.

    Returns:
        Normalised event dict as described above.

    Raises:
        ChatEventParseError: body is missing required fields or has unexpected structure.
    """
    try:
        # ── Detect format ────────────────────────────────────────────────────
        # Workspace Add-ons format has commonEventObject with hostApp="CHAT"
        common_event = body.get("commonEventObject", {})
        is_addons_format = common_event.get("hostApp") == "CHAT"

        if is_addons_format:
            return _parse_addons_format(body, common_event)
        else:
            return _parse_legacy_format(body)

    except ChatEventParseError:
        raise
    except Exception as exc:
        raise ChatEventParseError(f"Unexpected error parsing Chat event: {exc}") from exc


def _extract_attachments(message: dict) -> list[dict]:
    """Extract attachments from a Chat message dict.

    Args:
        message: The message dict containing an optional 'attachment' list.

    Returns:
        List of attachment dicts with keys: content_type, content_name,
        attachment_message_name, media_resource_name, download_uri.
    """
    attachments: list[dict] = []
    for att in message.get("attachment", []):
        att_ref: dict = att.get("attachmentDataRef", {})
        download_uri: str = att_ref.get("downloadUri", "")
        media_resource_name: str = att_ref.get("resourceName", "")
        if download_uri or media_resource_name:
            attachments.append(
                {
                    "content_type": att.get("contentType", "application/octet-stream"),
                    "content_name": att.get("contentName", ""),
                    "attachment_message_name": att.get("name", ""),
                    "media_resource_name": media_resource_name,
                    "download_uri": download_uri,
                }
            )
    return attachments


def _parse_addons_format(body: dict, common_event: dict) -> dict:
    """Parse Workspace Add-ons Chat event format."""
    chat_obj: dict = body.get("chat", {})

    # Determine event type from which payload is present
    if "messagePayload" in chat_obj:
        event_type = "MESSAGE"
        payload = chat_obj["messagePayload"]
        message: dict = payload.get("message", {})
        text = message.get("text", "").strip()
        message_name = message.get("name", "")
        thread_name = message.get("thread", {}).get("name", "")
        space: dict = payload.get("space", body.get("space", {}))
        space_name = space.get("name", "")
        # Sender from payload or body
        sender: dict = payload.get("sender", body.get("user", {}))
        sender_email = sender.get("email", "")
        action_name = ""
        parameters: dict[str, str] = {}
        attachments = _extract_attachments(message)

    elif "buttonClickedPayload" in chat_obj:
        event_type = "CARD_CLICKED"
        payload = chat_obj["buttonClickedPayload"]
        message: dict = payload.get("message", {})
        message_name = message.get("name", "")
        thread_name = message.get("thread", {}).get("name", "")
        space: dict = payload.get("space", body.get("space", {}))
        space_name = space.get("name", "")
        sender: dict = payload.get("sender", body.get("user", {}))
        sender_email = sender.get("email", "")
        text = ""

        # Action from buttonClickedPayload
        action_name = payload.get("action", {}).get("actionMethodName", "")
        parameters = {}
        for param in payload.get("action", {}).get("parameters", []):
            key = param.get("key", "")
            value = param.get("value", "")
            if key:
                parameters[key] = value
        attachments = []

    elif "addedToSpacePayload" in chat_obj:
        event_type = "ADDED_TO_SPACE"
        payload = chat_obj["addedToSpacePayload"]
        space: dict = payload.get("space", body.get("space", {}))
        space_name = space.get("name", "")
        sender_email = ""
        text = ""
        message_name = ""
        thread_name = ""
        action_name = ""
        parameters = {}
        attachments = []

    elif "removedFromSpacePayload" in chat_obj:
        event_type = "REMOVED_FROM_SPACE"
        payload = chat_obj["removedFromSpacePayload"]
        space: dict = payload.get("space", body.get("space", {}))
        space_name = space.get("name", "")
        sender_email = ""
        text = ""
        message_name = ""
        thread_name = ""
        action_name = ""
        parameters = {}
        attachments = []

    else:
        # Unknown Add-ons event type — log keys for debugging
        chat_keys = list(chat_obj.keys()) if chat_obj else []
        raise ChatEventParseError(f"Unknown Workspace Add-ons Chat event. chat keys: {chat_keys}")

    return {
        "event_type": event_type,
        "space_name": space_name,
        "sender_email": sender_email,
        "text": text,
        "action_name": action_name,
        "parameters": parameters,
        "message_name": message_name,
        "thread_name": thread_name,
        "attachments": attachments,
    }


def _parse_legacy_format(body: dict) -> dict:
    """Parse legacy Chat Bot API event format."""
    event_type: str = body.get("type", "")
    if not event_type:
        raise ChatEventParseError("Missing 'type' field in Chat event body.")

    space: dict = body.get("space", {})
    space_name: str = space.get("name", "")

    sender: dict = body.get("user", {})
    sender_email: str = sender.get("email", "")

    message: dict = body.get("message", {})
    text: str = message.get("text", "").strip()
    message_name: str = message.get("name", "")
    thread_name: str = message.get("thread", {}).get("name", "")

    action_name = ""
    parameters: dict[str, str] = {}

    if event_type == "CARD_CLICKED":
        action: dict = body.get("action", {})
        action_name = action.get("actionMethodName", "")
        for param in action.get("parameters", []):
            key = param.get("key", "")
            value = param.get("value", "")
            if key:
                parameters[key] = value

    # Extract image/file attachments — only present on MESSAGE events.
    attachments = _extract_attachments(message) if event_type == "MESSAGE" else []

    return {
        "event_type": event_type,
        "space_name": space_name,
        "sender_email": sender_email,
        "text": text,
        "action_name": action_name,
        "parameters": parameters,
        "message_name": message_name,
        "thread_name": thread_name,
        "attachments": attachments,
    }
