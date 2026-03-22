"""
scripts/chat_emulator.py — Interactive local Chat emulator for Nexus-Prime.

Drives the LangGraph state machine directly, bypassing Cloud Run HTTP and
Google Chat JWT authentication entirely.  Relies on Application Default
Credentials (ADC) for any GCP calls the graph makes (Secret Manager, etc.)
but gracefully skips calls that fail in a local environment.

Usage:
    python scripts/chat_emulator.py

Special commands (type at the You> prompt):
    /quit   — exit the emulator
    /status — show last turn cost and token counts
    /vision — submit a text-only vision statement (VISION_SUBMITTED message)
    /help   — print this command list

Prerequisites:
    - `gcloud auth application-default login` completed (for Gemini API calls)
    - GCP_PROJECT_ID env var set (or configured in config/settings.yaml)
    - python -m scripts.chat_emulator  OR  python scripts/chat_emulator.py from repo root

Spec: GAOS-Manager-Spec.md §2.5 (Phase 2.5 Chat)
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import uuid
from typing import Any

# ── 1. Env setup (before any project imports) ─────────────────────────────────

# Skip Chat JWT verification — no HTTP involved in local emulator, but set it
# defensively in case any code path checks this flag.
os.environ.setdefault("SKIP_CHAT_JWT_VERIFY", "true")

# Suppress chatty google-auth warnings in interactive mode
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")

# ── 2. Capture send_message BEFORE importing the orchestrator ─────────────────
# The orchestrator imports tools.google_chat at module load time in some paths,
# so we patch the module-level function before that import chain runs.

# Add repo root to sys.path so the script works from any cwd.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import tools.google_chat as _chat_mod  # noqa: E402

_captured_replies: list[str] = []


def _capture_send(space_name: str, text: str, **kwargs: Any) -> None:
    """Replacement for tools.google_chat.send_message — stores reply locally."""
    _captured_replies.append(text)


def _capture_threaded_reply(space_name: str, thread_key: str, text: str) -> None:
    """Replacement for tools.google_chat.send_threaded_reply — stores reply locally."""
    _captured_replies.append(text)


_chat_mod.send_message = _capture_send  # type: ignore[assignment]
_chat_mod.send_threaded_reply = _capture_threaded_reply  # type: ignore[assignment]

# ── 3. Now import the orchestrator ────────────────────────────────────────────

from agents.nexus_prime.orchestrator import (  # noqa: E402
    NexusPrimeWorkingMemory,
    build_nexus_prime_graph,
)
from models import A2AMessage, MessageType  # noqa: E402

# ── 4. Build the graph once (MemorySaver used for checkpointing) ──────────────

print("Nexus-Prime Chat Emulator — building graph...", flush=True)
try:
    _graph = build_nexus_prime_graph()
except Exception as _build_exc:
    print(f"[FATAL] Could not build graph: {_build_exc}")
    sys.exit(1)

# Persistent thread ID so MemorySaver carries state across turns in the same
# session (same as one continuous Google Chat space conversation).
_THREAD_ID = str(uuid.uuid4())
_SPACE_NAME = "spaces/emulator"
_SENDER_EMAIL = "local@emulator.dev"

# Cost tracking across the session
_session_cost: float = 0.0
_session_tokens: int = 0
_last_cost: float = 0.0
_last_tokens: int = 0


def _get_project_id() -> str:
    pid = os.environ.get("GCP_PROJECT_ID", "")
    if not pid:
        try:
            from config import get_settings

            pid = get_settings().GCP_PROJECT_ID
        except Exception:
            pid = "morphic-gaos-prod"
    return pid


def _build_envelope(msg_type: MessageType, payload: dict[str, Any]) -> dict[str, Any]:
    """Construct a Pub/Sub push envelope identical to what /chat sends."""
    project_id = _get_project_id()
    task_id = str(uuid.uuid4())
    a2a = A2AMessage(
        source_agent="google-chat",
        target_agent="nexus-prime",
        project_id=project_id,
        task_id=task_id,
        message_type=msg_type,
        priority=3,
        payload=payload,
    )
    return {
        "message": {
            "data": base64.b64encode(a2a.model_dump_json().encode()).decode(),
            "messageId": task_id,
        },
        "subscription": "emulator/push",
    }


def _build_initial_state(envelope: dict[str, Any]) -> NexusPrimeWorkingMemory:
    """Mirror the initial_state dict from NexusPrimeAgent.run()."""
    return {  # type: ignore[return-value]
        "task_id": str(uuid.uuid4()),
        "project_id": _get_project_id(),
        "current_objective": "Processing Chat message from emulator",
        "sub_task_results": [],
        "parked_proposals": [],
        "error_history": [],
        "memory_context": {},
        "episodic_cache": {},
        "observation_buffer": [],
        "cost_usd": 0.0,
        "iteration_count": 0,
        "step_count": 0,
        "tokens_used": 0,
        "incoming_message": None,
        "messages": [],
        "hard_stop_triggered": False,
        "evolution_triggered": False,
        "active_broadcasts": [],
        "conflict_queue": [],
        "safety_check_passed": False,
        "system_state_summary": {},
        "last_ttl_sweep_at": None,
        "pending_project_row": None,
        "new_project_id": None,
        "candidate_code": None,
        "candidate_agent_id": None,
        "candidate_sha256": None,
        "_started_at": 0.0,
        "active_blueprints": {},
        "blueprint_constraints": [],
        "_next_node": "record",
        "monologue_frame": None,
        "_raw_incoming": envelope,
    }


async def _run_turn(msg_type: MessageType, payload: dict[str, Any]) -> str:
    """Run one graph invocation and return the captured reply."""
    global _session_cost, _session_tokens, _last_cost, _last_tokens

    _captured_replies.clear()
    envelope = _build_envelope(msg_type, payload)
    initial_state = _build_initial_state(envelope)

    try:
        final_state = await _graph.ainvoke(
            initial_state,
            config={"configurable": {"thread_id": _THREAD_ID}},
        )
        _last_cost = final_state.get("cost_usd", 0.0)
        _last_tokens = final_state.get("tokens_used", 0)
        _session_cost += _last_cost
        _session_tokens += _last_tokens
    except Exception as exc:
        return f"[Graph error] {exc}"

    if _captured_replies:
        return _captured_replies[0]
    return "[No reply captured — graph ran but send_message was not called]"


def _print_help() -> None:
    print(
        "\nNexus-Prime Chat Emulator commands:\n"
        "  /quit         — exit\n"
        "  /status       — show cost and token counts for last turn and session total\n"
        "  /vision       — submit a VISION_SUBMITTED event (text-only vision)\n"
        "  /help         — show this help\n"
        "  <any text>    — send as CHAT_MESSAGE\n"
    )


async def main() -> None:
    print(
        "\n"
        "==================================================\n"
        "  Nexus-Prime Chat Emulator  (local dev)\n"
        "  Bypasses JWT and Cloud Run -- direct graph call\n"
        "  Type /help for command list, /quit to exit.\n"
        "==================================================\n"
    )

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not user_input:
            continue

        # ── Special commands ──────────────────────────────────────────────────

        if user_input.lower() in ("/quit", "/exit", "quit", "exit"):
            print(f"Session total — cost: ${_session_cost:.6f}  tokens: {_session_tokens:,}")
            print("Bye.")
            break

        if user_input.lower() == "/help":
            _print_help()
            continue

        if user_input.lower() == "/status":
            print(
                f"  Last turn  — cost: ${_last_cost:.6f}  tokens: {_last_tokens:,}\n"
                f"  Session    — cost: ${_session_cost:.6f}  tokens: {_session_tokens:,}"
            )
            continue

        if user_input.lower() == "/vision":
            vision_text = input("Vision text: ").strip()
            if not vision_text:
                print("  (empty — skipped)")
                continue
            payload: dict[str, Any] = {
                "vision_text": vision_text,
                "submitted_by": _SENDER_EMAIL,
                "space_name": _SPACE_NAME,
                "vision_source": "text",
            }
            print("Nexus-Prime: ", end="", flush=True)
            reply = await _run_turn(MessageType.VISION_SUBMITTED, payload)
            print(reply, "\n")
            continue

        # ── Normal CHAT_MESSAGE ───────────────────────────────────────────────

        payload = {
            "text": user_input,
            "sender_email": _SENDER_EMAIL,
            "space_name": _SPACE_NAME,
            "message_name": f"messages/{uuid.uuid4().hex[:8]}",
        }
        print("Nexus-Prime: ", end="", flush=True)
        reply = await _run_turn(MessageType.CHAT_MESSAGE, payload)
        print(reply, "\n")


if __name__ == "__main__":
    asyncio.run(main())
