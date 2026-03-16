"""
main.py — Cloud Run HTTP entry point for all Morphic-G AOS agents.

Each Cloud Run service is deployed from the same codebase. The AGENT_NAME
environment variable selects which orchestrator handles incoming requests.

Endpoints:
  POST /pubsub      Pub/Sub push subscription delivery
  POST /ttl-sweep   Cloud Scheduler TTL sweep (Nexus-Prime only)
  POST /sync        Apps Script → promote approved skill (Nexus-Prime only)
  GET  /health      Cloud Run health check (always 200)

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

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Morphic-G AOS Agent",
    description="Cloud Run entry point for all GAOS orchestrators.",
    version="1.0.0",
)

# ── Agent registry ────────────────────────────────────────────────────────────

_AGENT_NAME: str = os.environ.get("AGENT_NAME", "nexus-prime")

_AGENT_REGISTRY: dict[str, str] = {
    "nexus-prime": "agents.nexus_prime.orchestrator",
    "ledger":      "agents.ledger.orchestrator",
    "beacon":      "agents.beacon.orchestrator",
    "pursuit":     "agents.pursuit.orchestrator",
    "foreman":     "agents.foreman.orchestrator",
    "steward":     "agents.steward.orchestrator",
    "scout":       "agents.scout.orchestrator",
}

_AGENT_CLASS: dict[str, str] = {
    "nexus-prime": "NexusPrimeAgent",
    "ledger":      "LedgerAgent",
    "beacon":      "BeaconAgent",
    "pursuit":     "PursuitAgent",
    "foreman":     "ForemanAgent",
    "steward":     "StewardAgent",
    "scout":       "ScoutAgent",
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
                f"Unknown AGENT_NAME='{_AGENT_NAME}'. "
                f"Valid values: {list(_AGENT_REGISTRY)}"
            )
        import importlib
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        _agent_instance = cls()
        log.info("Agent '%s' initialised.", _AGENT_NAME)
    return _agent_instance


# ── Helpers ───────────────────────────────────────────────────────────────────

def _verify_pubsub_audience(request: Request) -> None:
    """
    Defense-in-depth: confirm the Authorization header is present and
    starts with 'Bearer'. The actual OIDC token is verified by Cloud Run
    ingress before the request reaches this handler.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header.")


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> Response:
    """Liveness probe — always returns 200."""
    return Response(content='{"status":"ok"}', media_type="application/json")


@app.post("/pubsub")
async def pubsub(request: Request) -> JSONResponse:
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
        raise HTTPException(status_code=400, detail="Invalid JSON envelope.")

    agent = _get_agent()

    try:
        result = await agent.run(envelope)
        log.info("Agent '%s' completed task: %s", _AGENT_NAME, getattr(result, "task_id", "?"))
    except Exception as exc:
        # Return 500 so Pub/Sub retries the message.
        log.exception("Agent '%s' raised an exception: %s", _AGENT_NAME, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    return JSONResponse(content={"status": "ok"}, status_code=204)


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

    from models import A2AMessage, MessageType
    import uuid, json, base64

    # Synthesise a push envelope so the existing graph handles it via the
    # monitor → route → record path (TTL sweep is handled in monitor).
    synthetic_msg = A2AMessage(
        source_agent="cloud-scheduler",
        target_agent="nexus-prime",
        project_id=os.environ.get("GCP_PROJECT_ID", ""),
        task_id=str(uuid.uuid4()),
        message_type=MessageType.TTL_SWEEP,
        priority=1,
        payload={"trigger": "scheduled"},
    )
    envelope = {
        "message": {
            "data": base64.b64encode(
                synthetic_msg.model_dump_json().encode()
            ).decode(),
            "messageId": synthetic_msg.task_id,
        },
        "subscription": "scheduler/ttl-sweep",
    }

    agent = _get_agent()
    try:
        await agent.run(envelope)
    except Exception as exc:
        log.exception("TTL sweep failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

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
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")

    from models import A2AMessage, MessageType
    import uuid, base64

    synthetic_msg = A2AMessage(
        source_agent="apps-script",
        target_agent="nexus-prime",
        project_id=body.get("project_id", os.environ.get("GCP_PROJECT_ID", "")),
        task_id=body.get("proposal_id", str(uuid.uuid4())),
        message_type=MessageType.APPROVAL_RESULT,
        priority=2,
        payload=body,
    )
    envelope = {
        "message": {
            "data": base64.b64encode(
                synthetic_msg.model_dump_json().encode()
            ).decode(),
            "messageId": synthetic_msg.task_id,
        },
        "subscription": "apps-script/sync",
    }

    agent = _get_agent()
    try:
        result = await agent.run(envelope)
    except Exception as exc:
        log.exception("Sync handler failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    return JSONResponse(content={
        "status": "ok",
        "task_id": getattr(result, "task_id", ""),
    })


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
        workers=1,       # LangGraph state must not be shared across workers
    )
