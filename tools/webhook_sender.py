"""
tools/webhook_sender.py — HMAC-signed webhook delivery for Morphic-G AOS.

Sends POST requests to the Apps Script doPost endpoint when an agent
submits a proposal to the Approval Gate. The payload is signed with
HMAC-SHA256 using a secret retrieved from Secret Manager.

Spec: GAOS-Tools-Spec.md §6
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import time
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx

from config import get_settings
from tools.secrets import get_secret

# ── Error types ─────────────────────────────────────────────────────────────


class WebhookDeliveryError(Exception):
    """Non-2xx HTTP response from the Apps Script endpoint after retries."""


class WebhookTimeoutError(Exception):
    """Request timed out after 10 seconds."""


class WebhookURLError(Exception):
    """Webhook URL is invalid or points to a private/internal address."""


# ── Constants ────────────────────────────────────────────────────────────────

_TIMEOUT_SECONDS = 10
_MAX_RETRIES = 3


# ── Core function ────────────────────────────────────────────────────────────


def post_to_webhook(payload: dict, project_id: str) -> None:
    """
    Sign the JSON payload with HMAC-SHA256 and POST it to
    WEBHOOK_URL configured in Secret Manager.

    Steps:
      1. If payload contains a ``Proposed Code`` or ``code`` field, computes
         SHA-256 of that value and adds it as ``code_sha256`` before signing.
      2. Serializes payload to canonical JSON (sorted keys, no whitespace).
      3. Computes HMAC-SHA256(body, WEBHOOK_HMAC_SECRET).
      4. Attaches the hex digest as the ``X-AOS-Signature`` header.
      5. Adds ``X-AOS-Project-ID`` and ``X-AOS-Timestamp`` headers.
      6. POSTs to WEBHOOK_URL with Content-Type: application/json.

    Args:
        payload:    The dict to send. Must be JSON-serialisable.
        project_id: The AOS project namespace (used in the X-AOS-Project-ID
                    header; secrets are loaded from the GCP project).

    Raises:
        WebhookDeliveryError: Non-2xx response after retry exhaustion.
        WebhookTimeoutError:  Request timed out after all retries.
        WebhookURLError:      URL is not HTTPS or resolves to a private IP.
        SecretNotFoundError:  WEBHOOK_HMAC_SECRET or WEBHOOK_URL not found.
    """
    settings = get_settings()
    gcp_project = settings.GCP_PROJECT_ID

    webhook_url = get_secret("WEBHOOK_URL", gcp_project)
    hmac_secret = get_secret("WEBHOOK_HMAC_SECRET", gcp_project)

    # Validate webhook URL: must be HTTPS and not a private/internal address
    parsed = urlparse(webhook_url)
    if parsed.scheme != "https":
        raise WebhookURLError(
            f"Webhook URL must use HTTPS, got '{parsed.scheme}'."
        )
    try:
        addr = ipaddress.ip_address(parsed.hostname or "")
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise WebhookURLError(
                f"Webhook URL must not point to a private/internal address: {parsed.hostname}"
            )
    except ValueError:
        pass  # hostname is a domain name — DNS resolution is deferred to httpx

    # Augment payload with code_sha256 before signing if code is present
    payload = dict(payload)  # shallow copy — don't mutate the caller's dict
    code_value = payload.get("Proposed Code") or payload.get("code")
    if code_value and "code_sha256" not in payload:
        payload["code_sha256"] = hashlib.sha256(
            str(code_value).encode("utf-8")
        ).hexdigest()

    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    body_bytes = body.encode("utf-8")

    signature = hmac.new(
        hmac_secret.encode("utf-8"), body_bytes, hashlib.sha256
    ).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "X-AOS-Signature": signature,
        "X-AOS-Project-ID": project_id,
        "X-AOS-Timestamp": datetime.now(UTC).isoformat(),
    }

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = httpx.post(
                webhook_url,
                content=body_bytes,
                headers=headers,
                timeout=_TIMEOUT_SECONDS,
            )
            if resp.is_success:
                return
            last_exc = WebhookDeliveryError(
                f"Webhook returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
            if resp.status_code < 500:
                # 4xx errors are not transient — raise immediately
                raise last_exc
            # 5xx — back off and retry
            time.sleep(2 ** attempt)
        except httpx.TimeoutException as exc:
            last_exc = WebhookTimeoutError(
                f"Webhook request timed out after {_TIMEOUT_SECONDS}s."
            )
            last_exc.__cause__ = exc
            time.sleep(2 ** attempt)
        except (WebhookDeliveryError, WebhookTimeoutError):
            raise

    raise (last_exc or WebhookDeliveryError(
        f"Webhook still failing after {_MAX_RETRIES} retries."
    ))
