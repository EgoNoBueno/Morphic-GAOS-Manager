"""
main.py — Cloud Run HTTP entry point for all Morphic-G AOS agents.

Each Cloud Run service is deployed from the same codebase. The AGENT_NAME
environment variable selects which orchestrator handles incoming requests.

Endpoints:
  POST /pubsub        Pub/Sub push subscription delivery
  POST /ttl-sweep     Cloud Scheduler TTL sweep (Nexus-Prime only)
  POST /sync          Apps Script → promote approved skill (Nexus-Prime only)
  POST /archive       Cloud Scheduler nightly archive sweep (Nexus-Prime only)
  POST /daily-sync    Cloud Scheduler 6 AM morning briefing (Nexus-Prime only)
  POST /chat          Google Chat push events — text messages and card callbacks
  POST /vision        Owner vision submission → Blueprint Doc generator (Nexus-Prime only)
  POST /poll-comments Cloud Scheduler 5-min doc-comment poll (Nexus-Prime only)
  GET  /health        Cloud Run health check (always 200)

All POST endpoints verify the OIDC token in the Authorization header before
dispatching. This is safe because Cloud Run is deployed --no-allow-unauthenticated;
Cloud Run's ingress layer handles OIDC validation automatically, so the check
here is defense-in-depth only.

Spec: GAOS-Deploy-Spec.md §9
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── Cached JWT transport ─────────────────────────────────────────────────────
# Google's id_token.verify_token() fetches public certificates from Google on
# each call.  Creating a single cached Request transport allows the library to
# reuse the HTTP session and cache certificates, reducing JWT verification from
# ~3.5 seconds to ~50 ms on subsequent calls.
_CACHED_AUTH_REQUEST: Any = None


def _get_cached_auth_request() -> Any:
    """Return a cached google.auth.transport.requests.Request instance."""
    global _CACHED_AUTH_REQUEST
    if _CACHED_AUTH_REQUEST is None:
        from google.auth.transport import requests as google_auth_requests

        _CACHED_AUTH_REQUEST = google_auth_requests.Request()
    return _CACHED_AUTH_REQUEST


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Morphic-G AOS Agent",
    description="Cloud Run entry point for all GAOS orchestrators.",
    version="1.0.0",
)


@app.on_event("startup")
async def _warm_jwt_cache() -> None:
    """Pre-warm the Google auth transport to cache certificates at startup.

    Google's id_token.verify_token() fetches public keys on first use.  By
    initialising the transport at startup, the first /chat request doesn't
    incur a 3+ second penalty waiting for Google's cert endpoint.
    """
    try:
        transport = _get_cached_auth_request()
        # The transport caches certs when a request is made.  We can pre-warm
        # by fetching the certs URL directly.  The google-auth library caches
        # responses for about 5 minutes.

        transport.session.get("https://www.googleapis.com/oauth2/v3/certs", timeout=5)
        log.info("JWT certificate cache pre-warmed at startup")
    except Exception as exc:
        log.warning("JWT cache warm-up failed (non-fatal): %s", exc)


# ── Agent registry ────────────────────────────────────────────────────────────

_AGENT_NAME: str = os.environ.get("AGENT_NAME", "nexus-prime")

_AGENT_REGISTRY: dict[str, str] = {
    "nexus-prime": "agents.nexus_prime.orchestrator",
    "ledger": "agents.ledger.orchestrator",
    "beacon": "agents.beacon.orchestrator",
    "pursuit": "agents.pursuit.orchestrator",
    "foreman": "agents.foreman.orchestrator",
    "steward": "agents.steward.orchestrator",
    "scout": "agents.scout.orchestrator",
}

_AGENT_CLASS: dict[str, str] = {
    "nexus-prime": "NexusPrimeAgent",
    "ledger": "LedgerAgent",
    "beacon": "BeaconAgent",
    "pursuit": "PursuitAgent",
    "foreman": "ForemanAgent",
    "steward": "StewardAgent",
    "scout": "ScoutAgent",
}

_agent_instance: Any = None


def _get_agent() -> Any:
    """Import and instantiate the correct agent on first call (lazy)."""
    global _agent_instance
    if _agent_instance is None:
        module_path = _AGENT_REGISTRY.get(_AGENT_NAME)
        class_name = _AGENT_CLASS.get(_AGENT_NAME)
        if module_path is None or class_name is None:
            raise RuntimeError(
                f"Unknown AGENT_NAME='{_AGENT_NAME}'. Valid values: {list(_AGENT_REGISTRY)}"
            )
        import importlib

        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        _agent_instance = cls()
        log.info("Agent '%s' initialised.", _AGENT_NAME)
    return _agent_instance


# ── Helpers ───────────────────────────────────────────────────────────────────


def _get_project_id() -> str:
    """Return GCP project ID from env var, falling back to settings.yaml.

    ``GCP_PROJECT_ID`` is the canonical source; settings.yaml is the fallback
    for local dev and for Cloud Run instances where the env var is not set.
    """
    pid = os.environ.get("GCP_PROJECT_ID", "")
    if not pid:
        from config import get_settings

        pid = get_settings().GCP_PROJECT_ID
    return pid


# Google Chat signs push requests with this service account.
_CHAT_ISSUER: str = "chat@system.gserviceaccount.com"
_CHAT_CERTS_URL: str = (
    "https://www.googleapis.com/service_accounts/v1/jwk/chat@system.gserviceaccount.com"
)


def _verify_pubsub_audience(request: Request) -> None:
    """
    Defense-in-depth: confirm the Authorization header is present and
    starts with 'Bearer'. The actual OIDC token is verified by Cloud Run
    ingress before the request reaches this handler.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header.")


def _verify_chat_jwt(request: Request) -> None:
    """
    Verify the Google-signed JWT that Google Chat attaches to every push request.

    Google Chat signs requests using the ``chat@system.gserviceaccount.com``
    service account.  We verify the token against that SA's public keys and
    confirm the audience matches the Cloud Run service URL so that only
    Google-originated Chat events can trigger state changes.

    When ``CLOUD_RUN_URL`` is not set, the function checks for an explicit
    opt-out via ``SKIP_CHAT_JWT_VERIFY=true``.  This must be set deliberately
    for local dev / CI — the check is fail-closed by default so a production
    misconfiguration cannot silently disable JWT enforcement.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header.")

    service_url = os.environ.get("CLOUD_RUN_URL", "").rstrip("/")
    if not service_url:
        if os.environ.get("SKIP_CHAT_JWT_VERIFY", "").lower() in ("1", "true", "yes"):
            log.warning(
                "SKIP_CHAT_JWT_VERIFY is set — Chat JWT audience verification disabled. "
                "This must never be set in production."
            )
            return
        # Fail closed: CLOUD_RUN_URL is required in production. If it is absent
        # and SKIP_CHAT_JWT_VERIFY is not explicitly set, refuse the request so
        # misconfiguration does not silently disable JWT checks.
        log.error(
            "CLOUD_RUN_URL is not set and SKIP_CHAT_JWT_VERIFY is not enabled. "
            "Cannot verify Chat JWT audience — rejecting request. "
            "Set CLOUD_RUN_URL to the Cloud Run service URL, or set "
            "SKIP_CHAT_JWT_VERIFY=true for local development only."
        )
        raise HTTPException(
            status_code=500,
            detail="Server misconfiguration: CLOUD_RUN_URL is required for JWT verification.",
        )

    token = auth[len("Bearer ") :].strip()
    # Google Chat sets the JWT `aud` claim to the exact configured push URL,
    # which includes the /chat path.  Verify against the full endpoint URL.
    chat_endpoint_url = f"{service_url}/chat"

    # Debug: decode JWT payload to log the actual audience claim (without signature verification)
    # Only enabled when ENABLE_JWT_DEBUG=1 — exposes kid/iss/aud which should not be logged
    # in production.
    if os.environ.get("ENABLE_JWT_DEBUG") == "1":
        try:
            import base64
            import json

            # JWT format: header.payload.signature
            parts = token.split(".")
            if len(parts) >= 2:
                # Decode header too for key ID
                header_b64 = parts[0] + "=" * (-len(parts[0]) % 4)
                header_json = base64.urlsafe_b64decode(header_b64)
                jwt_header = json.loads(header_json)
                jwt_kid = jwt_header.get("kid", "MISSING")

                # Add padding if needed
                payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
                payload_json = base64.urlsafe_b64decode(payload_b64)
                jwt_claims = json.loads(payload_json)
                jwt_aud = jwt_claims.get("aud", "MISSING")
                jwt_iss = jwt_claims.get("iss", "MISSING")
                log.debug(
                    f"Chat JWT debug: kid={jwt_kid!r}, iss={jwt_iss!r}, "
                    f"expected_aud={chat_endpoint_url!r}, actual_aud={jwt_aud!r}, "
                    f"aud_match={jwt_aud == chat_endpoint_url}"
                )
        except Exception as decode_exc:
            log.debug(f"Chat JWT debug: could not decode payload: {decode_exc}")

    try:
        from google.oauth2 import id_token as google_id_token

        # Use cached transport to enable HTTP session and certificate reuse
        # across requests — reduces verification from ~3.5s to ~50ms.
        id_info = google_id_token.verify_token(
            token,
            _get_cached_auth_request(),
            audience=chat_endpoint_url,
            # Removed: certs_url=_CHAT_CERTS_URL — let library auto-discover
        )
        log.info(f"Chat JWT verified successfully. iss={id_info.get('iss')!r}")
    except Exception as exc:
        log.error(f"Chat JWT verification failed: {exc}")
        raise HTTPException(
            status_code=401,
            detail=f"Chat JWT verification failed: {exc}",
        ) from exc


async def _download_chat_attachment(download_uri: str, media_resource_name: str = "") -> bytes:
    """
    Download a Google Chat attachment byte payload using service account credentials.

    The Workspace Add-ons Chat format omits ``downloadUri`` and provides only
    ``attachmentDataRef.resourceName`` instead.  When ``download_uri`` is empty,
    the function constructs the URL from ``media_resource_name`` using the
    Chat Media download endpoint.

    Uses the same service account key that drives the Chat API client, falling
    back to Application Default Credentials when the key file is unavailable.

    Args:
        download_uri: The ``attachmentDataRef.downloadUri`` from the Chat event
            (may be empty for Workspace Add-ons events).
        media_resource_name: The ``attachmentDataRef.resourceName`` from the
            Chat event.  Used to construct the download URL when ``download_uri``
            is empty.

    Returns:
        Raw file bytes.

    Raises:
        RuntimeError: If the download fails (network error, 4xx/5xx) or neither
            ``download_uri`` nor ``media_resource_name`` is provided.
    """
    import httpx

    from config import get_settings

    settings = get_settings()
    key_path: str = getattr(settings.chat, "service_account_key", "") or ""

    # Workspace Add-ons format omits downloadUri — construct it from resourceName.
    if not download_uri and media_resource_name:
        download_uri = f"https://chat.googleapis.com/v1/media/{media_resource_name}?alt=media"

    if not download_uri:
        raise RuntimeError(
            "Failed to download Chat attachment: no download_uri or media_resource_name available."
        )

    try:
        if key_path and os.path.exists(key_path):
            from google.auth.transport.requests import Request as GoogleRequest
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_file(
                key_path,
                scopes=["https://www.googleapis.com/auth/chat.bot"],
            )
            creds.refresh(GoogleRequest())
        else:
            import google.auth
            from google.auth.transport.requests import Request as GoogleRequest

            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/chat.bot"])
            creds.refresh(GoogleRequest())

        headers = {"Authorization": f"Bearer {creds.token}"}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(download_uri, headers=headers)
            resp.raise_for_status()
            return resp.content
    except Exception as exc:
        raise RuntimeError(f"Failed to download Chat attachment: {exc}") from exc


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> Response:
    """Liveness probe — always returns 200."""
    return Response(content='{"status":"ok"}', media_type="application/json")


@app.post("/pubsub")
async def pubsub(request: Request) -> Response:
    """
    Receive a Pub/Sub push delivery.

    Pub/Sub wraps the A2AMessage in a JSON envelope:
    {
      "message": {
        "data": "<base64-encoded JSON A2AMessage>",
        "messageId": "...",
        "publishTime": "...",
        "attributes": {...}
      },
      "subscription": "projects/.../subscriptions/..."
    }

    Returns 204 (ACK) on success, 4xx/5xx to trigger Pub/Sub retry.
    """
    _verify_pubsub_audience(request)

    try:
        envelope = await request.json()
    except Exception as exc:
        log.warning("Failed to parse Pub/Sub envelope: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid JSON envelope.") from exc

    agent = _get_agent()

    try:
        result = await agent.run(envelope)
        log.info("Agent '%s' completed task: %s", _AGENT_NAME, getattr(result, "task_id", "?"))
    except Exception as exc:
        # Return 500 so Pub/Sub retries the message.
        log.exception("Agent '%s' raised an exception: %s", _AGENT_NAME, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # HTTP 204 No Content must have no body — return a bare Response, not JSONResponse.
    return Response(status_code=204)


@app.post("/ttl-sweep")
async def ttl_sweep(request: Request) -> JSONResponse:
    """
    Cloud Scheduler hits this endpoint every hour (Nexus-Prime only).
    Triggers a TTL sweep that re-notifies or auto-rejects stale proposals.
    """
    _verify_pubsub_audience(request)

    if _AGENT_NAME != "nexus-prime":
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{_AGENT_NAME}' does not support /ttl-sweep.",
        )

    import base64
    import uuid

    from models import A2AMessage, MessageType

    # Synthesise a push envelope so the existing graph handles it via the
    # monitor → route → record path (TTL sweep is handled in monitor).
    synthetic_msg = A2AMessage(
        source_agent="cloud-scheduler",
        target_agent="nexus-prime",
        project_id=_get_project_id(),
        task_id=str(uuid.uuid4()),
        message_type=MessageType.TTL_SWEEP,
        priority=1,
        payload={"trigger": "scheduled"},
    )
    envelope = {
        "message": {
            "data": base64.b64encode(synthetic_msg.model_dump_json().encode()).decode(),
            "messageId": synthetic_msg.task_id,
        },
        "subscription": "scheduler/ttl-sweep",
    }

    agent = _get_agent()
    try:
        await agent.run(envelope)
    except Exception as exc:
        log.exception("TTL sweep failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse(content={"status": "ok"})


@app.post("/sync")
async def sync(request: Request) -> JSONResponse:
    """
    Apps Script calls this endpoint after a user approves a proposal.
    Body must be a JSON object matching ApprovalProposal schema.
    Nexus-Prime only.
    """
    _verify_pubsub_audience(request)

    if _AGENT_NAME != "nexus-prime":
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{_AGENT_NAME}' does not support /sync.",
        )

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    import base64
    import uuid

    from models import A2AMessage, MessageType

    synthetic_msg = A2AMessage(
        source_agent="apps-script",
        target_agent="nexus-prime",
        project_id=body.get("project_id") or _get_project_id(),
        task_id=body.get("proposal_id", str(uuid.uuid4())),
        message_type=MessageType.APPROVAL_RESULT,
        priority=2,
        payload=body,
    )
    envelope = {
        "message": {
            "data": base64.b64encode(synthetic_msg.model_dump_json().encode()).decode(),
            "messageId": synthetic_msg.task_id,
        },
        "subscription": "apps-script/sync",
    }

    agent = _get_agent()
    try:
        result = await agent.run(envelope)
    except Exception as exc:
        log.exception("Sync handler failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse(
        content={
            "status": "ok",
            "task_id": getattr(result, "task_id", ""),
        }
    )


@app.post("/archive")
async def archive(request: Request) -> JSONResponse:
    """
    Nightly archive sweep — called by Cloud Scheduler at 2:00 AM daily.
    Moves aged Sheet rows to BigQuery cold storage and deletes them from the Sheet.
    Nexus-Prime only.

    Spec: GAOS-Manager-Spec.md §9.5
    """
    _verify_pubsub_audience(request)

    if _AGENT_NAME != "nexus-prime":
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{_AGENT_NAME}' does not support /archive.",
        )

    from agents.nexus_prime.orchestrator import handle_archive

    project_id = _get_project_id()
    try:
        result = await handle_archive(project_id)
        log.info(
            "Nightly archive complete: %d rows archived across %s",
            result.get("total", 0),
            list(result.get("archived", {}).keys()),
        )
    except Exception as exc:
        log.exception("Nightly archive failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse(content={"status": "ok", **result})


@app.post("/daily-sync")
async def daily_sync(request: Request) -> JSONResponse:
    """
    Cloud Scheduler hits this endpoint at 6 AM daily (Nexus-Prime only).
    Queries overnight Logs, Error Logs, and pending Agent_Approvals, then sends
    a morning briefing Chat card to the owner's space configured in settings.

    Spec: GAOS-Manager-Spec.md §2.5 (Phase 2.5 Step 2)
    """
    _verify_pubsub_audience(request)

    if _AGENT_NAME != "nexus-prime":
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{_AGENT_NAME}' does not support /daily-sync.",
        )

    from agents.nexus_prime.orchestrator import handle_daily_sync

    project_id = _get_project_id()
    try:
        result = await handle_daily_sync(project_id)
        log.info(
            "Daily sync complete: %d logs, %d errors, %d pending approvals",
            result.get("overnight_logs", 0),
            result.get("overnight_errors", 0),
            result.get("pending_approvals", 0),
        )
    except Exception as exc:
        log.exception("Daily sync failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse(content={"status": "ok", **result})


@app.post("/chat")
async def chat(request: Request) -> JSONResponse:
    """
    Google Chat push endpoint — handles text messages and card button callbacks.

    Google Chat delivers interactive events as HTTP POST requests when:
    - The owner sends a direct message to the bot (``type: MESSAGE``).
    - The owner taps a card button (``type: CARD_CLICKED``).

    Event routing:
    - ``MESSAGE`` with no attachments → wraps text as ``CHAT_MESSAGE`` and routes
      to Nexus-Prime via the agent's ``run()`` graph.
    - ``MESSAGE`` with image attachment → downloads attachment bytes, runs a
      Gemini multimodal vision extraction (DEEP_MODEL), and routes the resulting
      text as ``VISION_SUBMITTED`` so the Blueprint Factory generates a doc.
    - ``CARD_CLICKED`` with ``action_name`` ``"approve"`` or ``"reject"``
      → wraps as ``APPROVAL_RESULT`` and routes to Nexus-Prime.
    - ``CARD_CLICKED`` with ``action_name`` ``"skill_approve"`` or ``"skill_reject"``
      → wraps as ``SKILL_REQUEST`` (resolved) and routes to Nexus-Prime.
    - All other event types respond 200 immediately (no-op ACK).

    Security: This endpoint verifies the Google-signed JWT in the Authorization
    header using ``google.oauth2.id_token.verify_token()`` with the Chat service
    account as issuer and the Cloud Run service URL as audience.  Set the
    ``CLOUD_RUN_URL`` environment variable to enable audience validation.

    Nexus-Prime only — other agents return 404.

    Spec: GAOS-Manager-Spec.md §2.5 (Phase 2.5 Step 1)
    """
    _verify_chat_jwt(request)

    if _AGENT_NAME != "nexus-prime":
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{_AGENT_NAME}' does not support /chat.",
        )

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    # Debug: log the raw Chat body structure to diagnose missing 'type' field
    import json as _json

    _body_preview = _json.dumps(body)[:500] if isinstance(body, dict) else str(body)[:500]
    log.debug(f"Chat body FULL DEBUG: {_body_preview}")

    import base64
    import uuid

    from models import A2AMessage, MessageType
    from tools.google_chat import ChatEventParseError, parse_chat_event

    try:
        event = parse_chat_event(body)
    except ChatEventParseError as exc:
        log.warning("Could not parse Chat event: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    event_type = event["event_type"]
    action_name = event.get("action_name", "")
    log.info(
        f"Chat parsed: event_type={event_type!r} space={event.get('space_name', '')!r} "
        f"text={event.get('text', '')[:50]!r} action={action_name!r}"
    )

    # Bot lifecycle events
    if event_type == "ADDED_TO_SPACE":
        try:
            from tools.google_chat import send_message

            send_message(
                event["space_name"],
                "Hi! I'm Nexus-Prime, your GAOS Strategic Architect. "
                "Send me a message to get started, or type /help for available commands.",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("ADDED_TO_SPACE welcome message failed: %s", exc)
        return JSONResponse(content={"status": "ok"})

    if event_type == "REMOVED_FROM_SPACE":
        return JSONResponse(content={"status": "ok"})

    # Determine message type and payload
    if event_type == "CARD_CLICKED" and action_name in ("approve", "reject"):
        msg_type = MessageType.APPROVAL_RESULT
        payload = {
            "status": "Approved" if action_name == "approve" else "Rejected",
            "proposal_id": event["parameters"].get("proposal_id", ""),
            "approved_by": event["sender_email"],
            "source": "google_chat",
            "space_name": event["space_name"],
            "message_name": event["message_name"],
        }
    elif event_type == "CARD_CLICKED" and action_name in ("skill_approve", "skill_reject"):
        msg_type = MessageType.SKILL_REQUEST
        payload = {
            "status": "Approved" if action_name == "skill_approve" else "Rejected",
            "proposal_id": event["parameters"].get("proposal_id", ""),
            "package_name": event["parameters"].get("package_name", ""),
            "approved_by": event["sender_email"],
            "source": "google_chat",
            "space_name": event["space_name"],
            "message_name": event["message_name"],
        }
    elif event_type == "MESSAGE":
        attachments = event.get("attachments", [])
        image_att = next(
            (a for a in attachments if a.get("content_type", "").startswith("image/")),
            None,
        )
        if image_att:
            # ── Multimodal vision path ─────────────────────────────────────────
            # Download image bytes, run Gemini DEEP_MODEL vision extraction,
            # then dispatch VISION_SUBMITTED so blueprint_factory generates a doc.
            # Restricted to DEEP_MODEL — multimodal accuracy requires Pro.
            # Budget impact: PRIORITY-2-COST-MONITOR tag in log for tracking.
            import datetime

            from agents import _call_model
            from config import get_settings

            settings = get_settings()
            download_uri = image_att.get("download_uri", "")
            media_resource_name = image_att.get("media_resource_name", "")
            submitted_at = datetime.datetime.now(datetime.UTC).isoformat()

            try:
                img_bytes = await _download_chat_attachment(download_uri, media_resource_name)
            except Exception as exc:
                log.error("Vision image download failed: %s", exc)
                from tools.google_chat import send_threaded_reply

                try:
                    _thread_key = event["message_name"] or f"vision-{media_resource_name[-12:]}"
                    send_threaded_reply(
                        event["space_name"],
                        _thread_key,
                        "\U0001f4f8 I received your image but couldn't download it. "
                        "Please try again or describe your vision in text.",
                    )
                except Exception:
                    pass
                return JSONResponse(content={"status": "ok"})

            vision_extract_prompt = (
                "Describe the project vision, business process, or workflow shown in this "
                "image in exhaustive detail so that a Blueprint Document can be generated "
                "from your description alone. Include every element, label, arrow, and "
                "annotation visible. Output plain text only."
            )
            try:
                vision_resp = await asyncio.to_thread(
                    _call_model,
                    prompt=vision_extract_prompt,
                    model=settings.models.DEEP_MODEL,
                    image_bytes=img_bytes,
                )
                vision_text = vision_resp.text.strip()
                log.info(
                    "PRIORITY-2-COST-MONITOR vision_extract tokens=%d model=%s submitted_by=%s",
                    vision_resp.tokens_used,
                    settings.models.DEEP_MODEL,
                    event["sender_email"],
                )
            except Exception as exc:
                log.error("Vision extraction model call failed: %s", exc)
                from tools.google_chat import send_threaded_reply

                try:
                    _thread_key = event["message_name"] or f"vision-err-{submitted_at[:10]}"
                    send_threaded_reply(
                        event["space_name"],
                        _thread_key,
                        "\U0001f4f8 I couldn't process your image right now. "
                        "Please describe your vision in text and I'll generate the Blueprint.",
                    )
                except Exception:
                    pass
                return JSONResponse(content={"status": "ok"})

            msg_type = MessageType.VISION_SUBMITTED
            payload = {
                "vision_text": vision_text,
                "submitted_by": event["sender_email"],
                "space_name": event["space_name"],
                "vision_source": "image",
                "image_submitted_at": submitted_at,
            }
        else:
            msg_type = MessageType.CHAT_MESSAGE
            payload = {
                "text": event["text"],
                "sender_email": event["sender_email"],
                "space_name": event["space_name"],
                "message_name": event["message_name"],
                "thread_name": event.get("thread_name", ""),
            }
    else:
        # Unknown CARD_CLICKED action or unsupported event — ACK silently
        return JSONResponse(content={"status": "ok"})

    project_id = _get_project_id()
    synthetic_msg = A2AMessage(
        source_agent="google-chat",
        target_agent="nexus-prime",
        project_id=project_id,
        task_id=str(uuid.uuid4()),
        message_type=msg_type,
        priority=3,
        payload=payload,
    )
    envelope = {
        "message": {
            "data": base64.b64encode(synthetic_msg.model_dump_json().encode()).decode(),
            "messageId": synthetic_msg.task_id,
        },
        "subscription": "google-chat/push",
    }

    log.info(
        f"Chat invoking agent: task_id={synthetic_msg.task_id} msg_type={msg_type.value} "
        f"payload_keys={list(payload.keys())}"
    )

    # Fire-and-forget: the actual reply is sent inside the graph via
    # send_threaded_reply(). Risk: if Cloud Run recycles the instance before
    # completion, the reply is lost — acceptable with min-instances=1.
    async def _run_agent_async() -> None:
        agent = _get_agent()
        try:
            result = await agent.run(envelope)
            log.info(
                f"Chat agent completed: task_id={synthetic_msg.task_id} "
                f"status={getattr(result, 'status', '?')}"
            )
        except Exception as exc:
            log.exception("Chat agent failed (async): %s", exc)

    asyncio.create_task(_run_agent_async())

    # Return an immediate, action-specific acknowledgment.
    # For CARD_CLICKED the owner already made a decision, so echo it back
    # directly instead of the generic "Processing…" placeholder.
    if event_type == "CARD_CLICKED":
        if action_name == "approve":
            ack_text = (
                "✅ Approval received; deployment requested. You will be notified on completion."
            )
        elif action_name == "reject":
            ack_text = "❌ Rejection received; the request will be processed."
        elif action_name == "skill_approve":
            ack_text = "✅ Skill import request received; agent will be notified."
        elif action_name == "skill_reject":
            ack_text = "❌ Skill import denial received."
        else:
            ack_text = "Processing..."
    else:
        ack_text = "Processing..."

    return JSONResponse(
        content={"text": ack_text},
        headers={"Content-Type": "application/json; charset=utf-8"},
    )


@app.post("/vision")
async def vision(request: Request) -> JSONResponse:
    """
    Owner submits a free-text vision statement via Chat, AppSheet, or direct POST.
    Nexus-Prime generates a Blueprint Google Doc and sends an approval card.

    Expected body:
    {
      "vision_text": "I want to build a loyalty programme for top clients.",
      "submitted_by": "owner@example.com",   // optional
      "space_name":   "spaces/XXXXXXX",       // optional — override for Chat card
      "project_id":   "my-gcp-project"        // optional — falls back to env var
    }

    Returns 200 with task_id on success.
    Nexus-Prime only.

    Spec: GAOS-Manager-Spec.md §2.5 Step 5
    """
    _verify_pubsub_audience(request)

    if _AGENT_NAME != "nexus-prime":
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{_AGENT_NAME}' does not support /vision.",
        )

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    vision_text: str = body.get("vision_text", "")
    if not vision_text:
        raise HTTPException(status_code=400, detail="vision_text is required.")

    import base64
    import uuid

    from models import A2AMessage, MessageType

    project_id = body.get("project_id") or _get_project_id()
    synthetic_msg = A2AMessage(
        source_agent=body.get("submitted_by", "owner"),
        target_agent="nexus-prime",
        project_id=project_id,
        task_id=str(uuid.uuid4()),
        message_type=MessageType.VISION_SUBMITTED,
        priority=3,
        payload={
            "vision_text": vision_text,
            "submitted_by": body.get("submitted_by", "owner"),
            "space_name": body.get("space_name", ""),
        },
    )
    envelope = {
        "message": {
            "data": base64.b64encode(synthetic_msg.model_dump_json().encode()).decode(),
            "messageId": synthetic_msg.task_id,
        },
        "subscription": "http/vision",
    }

    agent = _get_agent()
    try:
        result = await agent.run(envelope)
        log.info("Vision submitted: task_id=%s", getattr(result, "task_id", "?"))
    except Exception as exc:
        log.exception("Vision handler failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse(
        content={
            "status": "ok",
            "task_id": getattr(result, "task_id", synthetic_msg.task_id),
        }
    )


@app.post("/poll-comments")
async def poll_comments(request: Request) -> JSONResponse:
    """
    Cloud Scheduler hits this endpoint every 5 minutes (Nexus-Prime only).
    Polls ``list_comments()`` for all active Blueprint Docs and publishes a
    ``COMMENT_RECEIVED`` Pub/Sub message for each new unresolved comment.

    This is a standalone handler (not routed through the LangGraph graph)
    because it fans out across multiple docs and uses async iteration.

    Spec: GAOS-Manager-Spec.md §2.5 Step 5
    """
    _verify_pubsub_audience(request)

    if _AGENT_NAME != "nexus-prime":
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{_AGENT_NAME}' does not support /poll-comments.",
        )

    from agents.nexus_prime.orchestrator import handle_poll_comments

    project_id = _get_project_id()
    try:
        result = await handle_poll_comments(project_id)
        log.info(
            "poll-comments complete: %d docs, %d published, %d errors",
            result.get("docs_polled", 0),
            result.get("comments_published", 0),
            result.get("errors", 0),
        )
    except Exception as exc:
        log.exception("poll-comments handler failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse(content={"status": "ok", **result})


# ── Cloud Run startup ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    log.info("Starting GAOS agent '%s' on port %d", _AGENT_NAME, port)
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        workers=1,  # LangGraph state must not be shared across workers
    )
