"""
agents/nexus_prime/orchestrator.py — Tier 1 Root Agent

Nexus-Prime is the AOS general manager. It does not perform domain work
directly — it routes tasks to Tier 2 orchestrators, owns the Approval Gate,
and governs the self-evolution protocol.

Construction spec: Docs/GAOS-Agent-Spec.md
Identity file:     Docs/agents/nexus-prime.md
Master spec:       Docs/GAOS-Manager-Spec.md §1
Nexus spec:        Docs/GAOS-Nexus-Prime-Spec.md
"""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from agents import (
    AgentState,
    ModelResponse,
    _call_model,
    _elapsed_seconds,
    _load_context_trio,
    _load_identity_file,
    _log_cloud,
    _run_evolution_loop,
    _validate_proposal_coherence,
    _write_heartbeat,
    log_state_transition,
    utcnow_date,
    utcnow_iso,
    validate_code_safety,
)
from models import A2AMessage, AgentWorkingMemory, ApprovalProposal, MessageType, MonologueFrame
from tools.circuit_breaker import (
    CircuitOpenError,
)
from tools.circuit_breaker import (
    check as cb_check,
)
from tools.circuit_breaker import (
    record_failure as cb_failure,
)
from tools.circuit_breaker import (
    record_success as cb_success,
)
from tools.phoenix import save_checkpoint

# ── Nexus-Prime working memory ────────────────────────────────────────────────


class NexusPrimeWorkingMemory(AgentWorkingMemory, total=False):
    """
    Extends the common AgentWorkingMemory with Nexus-Prime-specific fields.
    Defined in GAOS-Nexus-Prime-Spec.md §2.
    """

    active_broadcasts: list[A2AMessage]
    conflict_queue: list[dict]
    # parked_proposals (list[str]) is inherited from AgentWorkingMemory

    # Project initialization (ephemeral per task)
    pending_project_row: dict | None
    new_project_id: str | None

    # Evolution gate
    candidate_code: str | None
    candidate_agent_id: str | None
    candidate_sha256: str | None
    safety_check_passed: bool

    # System health
    system_state_summary: dict
    last_ttl_sweep_at: str | None

    # Raw Pub/Sub envelope — set by run(), consumed by monitor()
    _raw_incoming: Any

    # Vision workflow (Phase 2.5 Step 5)
    active_blueprints: dict  # maps blueprint_id → doc_id
    blueprint_constraints: list[dict]  # active constraint stack per blueprint

    # Strategic Architect — think node
    _next_node: str  # Set by think(); routes to diagnose or knowledge_review
    monologue_frame: dict | None  # Last MonologueFrame dict; used by tests for assertion


# ── Constraint compaction threshold ──────────────────────────────────────────

_COMPACTION_THRESHOLD = 5

# ── Secret validation cache ───────────────────────────────────────────────────
# Tracks which project_ids have already passed the GEMINI_API_KEY check so
# boot() does not hit Secret Manager on every request (boot() is the LangGraph
# entry point and runs on every ainvoke call). sys.exit(1) fires only on the
# first encounter for a given pid; subsequent requests short-circuit.
_validated_pids: set[str] = set()


# ── Decision / format model selection ────────────────────────────────────────

_DECISION_NODES = {"diagnose", "knowledge_review", "conflict_resolve"}
_FORMAT_NODES = {"record"}


def _model_for_node(node_name: str) -> str:
    from config import get_settings

    s = get_settings()
    if node_name in _DECISION_NODES:
        return s.models.DEEP_MODEL
    if node_name in _FORMAT_NODES:
        return s.models.LOCAL_MODEL
    return s.models.FAST_MODEL


# ── Prompt builders ───────────────────────────────────────────────────────────


def _build_diagnosis_prompt(msg: A2AMessage, similar: list, recent_errors: list) -> str:
    return (
        f"Agent {msg.source_agent} escalated with error:\n"
        f"  fingerprint: {msg.payload.get('error_fingerprint', 'n/a')}\n"
        f"  description: {msg.payload.get('description', 'n/a')}\n\n"
        f"Similar past failures ({len(similar)}):\n"
        + "\n".join(f"  - {e.get('result_summary', '')} [{e.get('status', '')}]" for e in similar)
        + f"\n\nRecent sheet entries ({len(recent_errors)}):\n"
        + "\n".join(f"  - {r}" for r in recent_errors[:3])
        + "\n\nDecide: is self-repair possible, or is human approval required?\n"
        'Return JSON: {"suggests_code_change": bool, "rationale": str, "fix_summary": str}'
    )


def _build_knowledge_review_prompt(candidate: dict, duplicates: list) -> str:
    dup_block = (
        "\n".join(
            f"  - (memory_id={d.get('memory_id', '?')}) {d.get('content', str(d))}"
            for d in duplicates[:5]
        )
        if duplicates
        else "  (none)"
    )
    return (
        f"Candidate knowledge entry:\n{candidate.get('content', '')}\n\n"
        f"Existing Memory Bank entries for this domain ({len(duplicates)} retrieved):\n"
        + dup_block
        + "\n\nAssess the candidate's confidence and uniqueness.\n"
        "If the candidate REFINES or REPLACES one of the existing entries (same concept, "
        "updated or corrected information), set supersedes_memory_id to that entry's memory_id "
        "and set supersession_reason to a one-sentence explanation of why the old entry is being "
        "retired (e.g. 'Updated vendor terms override the 2024 policy'). "
        "If it is purely new information with no existing entry to retire, set both "
        "supersedes_memory_id and supersession_reason to null.\n\n"
        'Return JSON: {"confidence": float, "is_duplicate": bool, "rationale": str, '
        '"supersedes_memory_id": str | null, "supersession_reason": str | null}'
    )


def _build_conflict_prompt(conflict: dict) -> str:
    return (
        f"Conflicting state for entity '{conflict.get('entity_key', 'unknown')}':\n"
        f"  Agent A ({conflict.get('agent_a', '?')}): {conflict.get('value_a', '')}\n"
        f"  Agent B ({conflict.get('agent_b', '?')}): {conflict.get('value_b', '')}\n\n"
        "Arbitrate which value is correct. Return JSON: "
        '{"decision": str, "rationale": str, "winning_agent": str}'
    )


# ── Private helpers ───────────────────────────────────────────────────────────


def _compute_priority(state: NexusPrimeWorkingMemory) -> int:
    msg = state.get("incoming_message")
    if msg is None:
        return 3
    return getattr(msg, "priority", 3)


def _build_think_prompt(
    state: NexusPrimeWorkingMemory,
    msg: A2AMessage | None,
    priority: int,
) -> str:
    """Build the pre-response reasoning prompt for the think node."""
    msg_type = msg.message_type.value if msg else "NONE"
    source = msg.source_agent if msg else "unknown"
    payload_summary = str(msg.payload or {})[:300] if msg else ""
    return (
        f"You are Nexus-Prime, the AOS Tier 1 root agent. A new message has arrived.\n\n"
        f"Message type:      {msg_type}\n"
        f"Source agent:      {source}\n"
        f"Priority:          {priority}/5\n"
        f"Payload (trimmed): {payload_summary}\n\n"
        f"Before responding, reason through the following and return ONLY valid JSON:\n"
        f"  1. Is there a knowledge gap that prevents you from acting confidently?\n"
        f"     If so, what specifically is missing?\n"
        f"  2. Is a partial result available despite the gap?\n"
        f"  3. Which response mode applies?\n"
        f"     - 'Direct'   — all context available; execute immediately\n"
        f"     - 'Reframe'  — a faster/better alternative exists; surface it first\n"
        f"     - 'Research' — knowledge gap detected; provide best partial result and "
        f"state the condition needed for the full result\n"
        f"     - 'Tactical' — priority >= 4 or time-critical; lead with the most critical action\n"
        f"  4. Summarize your reasoning in one sentence.\n\n"
        f"Return JSON with exactly these keys: knowledge_gap_detected (bool), "
        f"knowledge_gap_description (str), partial_result_available (bool), "
        f"response_mode (str — one of Direct/Reframe/Research/Tactical), "
        f"reasoning_summary (str)"
    )


def _route_from_think(state: NexusPrimeWorkingMemory) -> str:
    """Sub-router after think — returns the value stored by think() in _next_node."""
    return state.get("_next_node", "record")  # type: ignore[typeddict-item]


def _log_hard_stop(state: NexusPrimeWorkingMemory, reason: str) -> None:
    _log_cloud(
        agent_id="nexus-prime",
        project_id=state.get("project_id", ""),
        log_type="security",
        task_id=state.get("task_id", ""),
        message=f"HARD_STOP: {reason}",
        severity="CRITICAL",
    )


def _format_heartbeat(state: NexusPrimeWorkingMemory) -> str:
    from config import get_settings

    s = get_settings()
    model = s.models.LOCAL_MODEL
    parked = len(state.get("parked_proposals", []))
    summary_prompt = (
        f"Summarize in one sentence: project={state.get('project_id')}, "
        f"task_id={state.get('task_id')}, parked_proposals={parked}, "
        f"cost_usd={state.get('cost_usd', 0):.4f}, "
        f"hard_stop={state.get('hard_stop_triggered', False)}"
    )
    try:
        resp = _call_model(summary_prompt, model=model)
        return resp.text.strip()
    except Exception:
        return f"Nexus-Prime cycle complete. Parked={parked}."


def _write_to_pending_knowledge(
    state: NexusPrimeWorkingMemory, candidate: dict, verdict: ModelResponse
) -> None:
    from tools.google_sheets import append_row

    row = {
        "timestamp": utcnow_iso(),
        "agent_id": candidate.get("agent_id", ""),
        "domain": candidate.get("domain", ""),
        "content": candidate.get("content", "")[:1000],
        "confidence": verdict.data.get("confidence", 0.0),
        "rationale": verdict.data.get("rationale", "")[:500],
        "status": "Pending Human Review",
    }
    try:
        append_row("Pending_Knowledge", row, state["project_id"])
    except Exception:
        pass


def _create_sheet_workbook(new_pid: str) -> str:
    """Clone the master Sheet workbook for a new project via Drive API. Returns file ID."""
    import google.auth
    from googleapiclient.discovery import build  # type: ignore[import-untyped]

    from config import get_settings

    settings = get_settings()
    template_id = settings.sheet.workbook_id
    # Full drive scope required: files().copy() on a pre-existing template file
    # (not created by this SA) cannot be authorised with drive.file scope.
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/" + "drive"])
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    result = (
        service.files()
        .copy(
            fileId=template_id,
            body={"name": f"[{new_pid}] AOS Dashboard"},
        )
        .execute()
    )
    return result.get("id", "")


def _create_drive_folder(new_pid: str) -> str:
    """Create a Knowledge/ Drive folder for a new project. Returns folder path key."""
    from config import get_settings
    from tools.drive import write_file

    settings = get_settings()
    # write_file(file_path, content, project_id) — creates parent folders automatically
    write_file(
        file_path=f"{new_pid}/Knowledge/README.md",
        content=f"# {new_pid} — Knowledge Base\n\nAuto-created by Nexus-Prime.",
        project_id=settings.GCP_PROJECT_ID,
    )
    return f"knowledge/{new_pid}"


def _trigger_sync_to_vertex(row: dict, state: NexusPrimeWorkingMemory) -> None:
    """Send the approved code to Apps Script syncSkillsToVertex() via webhook."""
    from tools.webhook_sender import post_to_webhook

    payload = {
        "action": "syncSkillsToVertex",
        "proposal_id": row.get("ID", ""),
        "agent_id": row.get("Agent ID", ""),
        "code": row.get("Proposed Code", ""),
        "code_sha256": row.get("code_sha256", ""),
    }
    post_to_webhook(payload, state["project_id"])


# ── Graph node functions ───────────────────────────────────────────────────────


def think(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """
    Mandatory pre-response reasoning node (Nexus-Prime only, Tier 1).

    Runs between ``route`` and every output-producing node (diagnose,
    knowledge_review). Uses FAST_MODEL with the Context Trio as system
    prompt to determine the Strategic Architect response_mode, detect
    knowledge gaps, and log a MonologueFrame to BigQuery before the
    downstream node executes.

    Tactical mode trigger: incoming message priority >= 4.

    Spec: GAOS-Nexus-Prime-Spec.md §3.2 — think node
    """
    from config import get_settings
    from tools.bigquery import insert_row

    settings = get_settings()
    msg = state.get("incoming_message")
    project_id = state["project_id"]
    task_id = state.get("task_id", "")

    # Map message type → downstream node (mirrors route logic for think-gated paths)
    msg_type = msg.message_type if msg else None
    if msg_type in (MessageType.ESCALATION, MessageType.EVOLUTION_REQUEST):
        next_node = "diagnose"
    elif msg_type == MessageType.KNOWLEDGE_CANDIDATE:
        next_node = "knowledge_review"
    elif msg_type == MessageType.CHAT_MESSAGE:
        next_node = "chat_respond"
    else:
        next_node = "record"
    state["_next_node"] = next_node  # type: ignore[typeddict-item]

    priority = _compute_priority(state)

    # Tactical fast-path — skip the LLM call when priority >= 4; the mode is always
    # "Tactical" regardless of what the model would return, so calling it wastes tokens.
    if priority >= 4:
        data: dict = {
            "response_mode": "Tactical",
            "knowledge_gap_detected": False,
            "knowledge_gap_description": "",
            "partial_result_available": False,
            "reasoning_summary": "High-priority message; Tactical mode applied without model call.",
        }
    # Conversational fast-path — CHAT_MESSAGE mode and next node are deterministic.
    # Skipping the LLM call here halves the hot-path latency for every chat message.
    elif msg_type == MessageType.CHAT_MESSAGE:
        data = {
            "response_mode": "Direct",
            "knowledge_gap_detected": False,
            "knowledge_gap_description": "",
            "partial_result_available": False,
            "reasoning_summary": "CHAT_MESSAGE — conversational direct response, no pre-analysis needed.",
        }
    else:
        context_trio = _load_context_trio()
        prompt = _build_think_prompt(state, msg, priority)

        try:
            resp = _call_model(
                prompt,
                model=settings.models.FAST_MODEL,
                system_prompt=context_trio,
                parse_json=True,
            )
        except Exception as exc:
            _log_cloud(
                "nexus-prime",
                project_id,
                "task",
                task_id,
                f"think: _call_model failed — {exc}",
                "WARNING",
            )
            return state

        state["cost_usd"] = state.get("cost_usd", 0.0) + resp.cost_usd
        state["tokens_used"] = state.get("tokens_used", 0) + resp.tokens_used

        data = resp.data or {}
        data.setdefault("reasoning_summary", resp.text[:500])

        # Validate and normalise response_mode
        _VALID_MODES = {"Research", "Direct", "Reframe", "Tactical"}
        if data.get("response_mode") not in _VALID_MODES:
            data["response_mode"] = "Research" if data.get("knowledge_gap_detected") else "Direct"

    try:
        frame = MonologueFrame(
            task_id=task_id,
            project_id=project_id,
            knowledge_gap_detected=bool(data.get("knowledge_gap_detected", False)),
            knowledge_gap_description=str(data.get("knowledge_gap_description", "")),
            partial_result_available=bool(data.get("partial_result_available", False)),
            response_mode=data["response_mode"],
            reasoning_summary=str(data.get("reasoning_summary", "")),
            timestamp=utcnow_iso(),
        )
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"think: MonologueFrame validation failed — {exc}",
            "WARNING",
        )
        return state

    state["monologue_frame"] = frame.model_dump()  # type: ignore[typeddict-item]

    try:
        insert_row("aos_logs.monologue_frames", frame.model_dump(), project_id)
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"think: BQ insert failed — {exc}",
            "WARNING",
        )

    _log_cloud(
        "nexus-prime",
        project_id,
        "task",
        task_id,
        f"think: mode={frame.response_mode} gap={frame.knowledge_gap_detected} next={next_node}",
    )
    return state


def boot(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """
    Runs once at service startup. Initialises subscriptions, loads parked
    proposals from Agent_Approvals, and validates credentials.
    """
    from config import get_settings
    from tools.google_sheets import get_all_records, init_sheets_client
    from tools.pubsub import ensure_topic_exists

    state["_started_at"] = time.time()
    state.setdefault("active_broadcasts", [])
    state.setdefault("conflict_queue", [])
    state.setdefault("parked_proposals", [])
    state.setdefault("messages", [])
    state.setdefault("sub_task_results", [])
    state.setdefault("error_history", [])
    state.setdefault("observation_buffer", [])
    state.setdefault("memory_context", {})
    state.setdefault("episodic_cache", {})
    state.setdefault("cost_usd", 0.0)
    state.setdefault("iteration_count", 0)
    state.setdefault("step_count", 0)
    state.setdefault("tokens_used", 0)
    state.setdefault("hard_stop_triggered", False)
    state.setdefault("evolution_triggered", False)
    state.setdefault("safety_check_passed", False)
    state.setdefault("system_state_summary", {})
    state.setdefault("current_objective", "Booting")
    state.setdefault("active_blueprints", {})
    state.setdefault("blueprint_constraints", [])

    settings = get_settings()
    # Use `or` so an empty-string project_id falls back to settings, not just absent key.
    pid = state.get("project_id") or settings.GCP_PROJECT_ID
    state["project_id"] = pid

    try:
        init_sheets_client(pid)
    except Exception as _sheets_init_exc:
        _log_cloud(
            "nexus-prime",
            pid,
            "task",
            state.get("task_id", "boot"),
            f"boot: init_sheets_client failed — {_sheets_init_exc}",
            "ERROR",
        )

    # Fail fast if critical secrets are missing (Rule 9 §7 Step 3).
    # _validated_pids caches successful checks so Secret Manager is called at
    # most once per pid per process lifetime — boot() is the LangGraph entry
    # point and runs on every ainvoke(), making per-request validation unsafe.
    if pid not in _validated_pids:
        try:
            from tools.secrets import SecretAccessDenied, SecretNotFoundError, get_secret

            get_secret("GEMINI_API_KEY", pid)
            _validated_pids.add(pid)
        except (SecretNotFoundError, SecretAccessDenied) as exc:
            _log_cloud(
                "nexus-prime", pid, "security", "boot", f"STARTUP_FAILURE: {exc}", "CRITICAL"
            )
            import sys

            sys.exit(1)

    # Ensure all Pub/Sub topics exist (idempotent)
    for topic in settings.pubsub.all_topics:
        try:
            ensure_topic_exists(topic)
        except Exception:
            pass  # Non-fatal; topic may already exist in a different project

    # Load parked proposals (store IDs only, consistent with list[str])
    try:
        all_proposals = get_all_records("Agent_Approvals", pid)
        state["parked_proposals"] = [
            r["ID"]
            for r in all_proposals
            if r.get("Status") in ("Pending", "Needs Revision") and r.get("ID")
        ]
    except Exception:
        pass

    # Load Project Registry for system state summary
    try:
        registry = get_all_records("Project Registry", pid)
        state["system_state_summary"] = {
            r["project_id"]: r["status"] for r in registry if r.get("project_id")
        }
    except Exception:
        pass

    _log_cloud("nexus-prime", pid, "task", state.get("task_id", "boot"), "Boot complete")
    return state


def monitor(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """Decode incoming Pub/Sub push envelope from state and populate working memory."""
    from tools.pubsub import decode_push_message

    project_id = state.get("project_id", "")
    task_id = state.get("task_id", "monitor")

    raw = state.get("_raw_incoming")  # type: ignore[typeddict-item]
    if not raw:
        _log_cloud("nexus-prime", project_id, "task", task_id, "monitor: no _raw_incoming")
        state["step_count"] = state.get("step_count", 0) + 1
        return state

    _log_cloud("nexus-prime", project_id, "task", task_id, "monitor: decoding push message")
    try:
        msg = decode_push_message(raw)
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"monitor: decode_push_message failed — {exc}",
            "ERROR",
        )
        state["step_count"] = state.get("step_count", 0) + 1
        return state

    state["incoming_message"] = msg
    state["project_id"] = msg.project_id
    state["task_id"] = msg.task_id or str(uuid.uuid4())
    state["step_count"] = state.get("step_count", 0) + 1
    _log_cloud(
        "nexus-prime",
        msg.project_id,
        "task",
        state["task_id"],
        f"monitor: decoded msg_type={msg.message_type.value} from={msg.source_agent}",
    )
    return state


def route(state: NexusPrimeWorkingMemory) -> str:
    """Pure routing — DEEP_MODEL not required; no I/O."""
    msg = state.get("incoming_message")
    if msg is None:
        return "record"

    # APPROVAL_RESULT: inline sub-routing so LangGraph receives a node name directly.
    # (Avoids adding _route_approval as a passthrough node.)
    if msg.message_type == MessageType.APPROVAL_RESULT:
        payload = msg.payload or {}
        status = payload.get("status", "")
        if status == "Approved":
            return "promote"
        if status == "Rejected":
            return "record"
        return "park_or_broadcast"

    routing_table = {
        MessageType.STATUS_UPDATE: "record",
        MessageType.TASK_COMPLETE: "record",
        MessageType.ESCALATION: "think",  # think → diagnose
        MessageType.EVOLUTION_REQUEST: "think",  # think → diagnose
        MessageType.KNOWLEDGE_CANDIDATE: "think",  # think → knowledge_review
        MessageType.CHAT_MESSAGE: "think",  # think → chat_respond
        MessageType.BROADCAST: "conflict_resolve",
        MessageType.NEW_PROJECT: "init_project",
        MessageType.VISION_SUBMITTED: "vision_blueprint",
        MessageType.PLAN_REVIEW: "iterate_plan",
        MessageType.COMMENT_RECEIVED: "iterate_plan",
        MessageType.SKILL_REQUEST: "handle_skill_request",
        MessageType.STOCK_INSUFFICIENT: "market_watchdog",
        MessageType.DEAL_CLOSED: "roi_optimizer",
        MessageType.INFRA_PROVISION_APPROVED: "handle_infra_provision",
        MessageType.INFRA_PROVISION_REJECTED: "handle_infra_provision",
        MessageType.APPROVAL_REQUEST: "handle_approval_request",
    }
    return routing_table.get(msg.message_type, "record")


def diagnose(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """
    Analyse an escalated failure or evolution request using DEEP_MODEL.
    Decides whether self-repair or human approval is needed.
    """
    from tools.google_sheets import find_rows
    from tools.memory import query_memory_bank

    msg = state.get("incoming_message")
    if msg is None:
        return state

    error_fp = msg.payload.get("error_fingerprint", "") if msg.payload else ""

    similar: list = []
    recent_errors: list = []
    try:
        similar = query_memory_bank(
            query=error_fp,
            corpus=f"gaos-{msg.source_agent.replace('-', '_')}",
            project_id=state["project_id"],
            top_k=3,
        )
    except Exception:
        pass
    try:
        recent_errors = find_rows("Error Logs", "error_fingerprint", error_fp, state["project_id"])
    except Exception:
        pass

    prompt = _build_diagnosis_prompt(msg, similar, recent_errors)
    resp = _call_model(prompt, model=_model_for_node("diagnose"), parse_json=True)
    state["cost_usd"] = state.get("cost_usd", 0.0) + resp.cost_usd
    state["tokens_used"] = state.get("tokens_used", 0) + resp.tokens_used
    state["messages"].append({"role": "assistant", "content": resp.text})

    if resp.data.get("suggests_code_change"):
        evo = _run_evolution_loop(
            issue=msg.payload.get("description", error_fp),
            agent_id=msg.source_agent,
            context=resp.data.get("fix_summary", ""),
        )
        state["evolution_triggered"] = True
        state["candidate_code"] = evo["code"]
        state["candidate_agent_id"] = msg.source_agent
        state["cost_usd"] += evo["cost_usd"]
        state["iteration_count"] += evo["iterations"]

        # Log EvolutionTaskOutcome
        _log_cloud(
            agent_id="nexus-prime",
            project_id=state["project_id"],
            log_type="evolution_task",
            task_id=state.get("task_id", ""),
            message=(
                f"EvolutionTaskOutcome agent={msg.source_agent} "
                f"iterations={evo['iterations']} "
                f"stopping_constraint={evo['stopping_constraint']}"
            ),
            extra=evo,
        )

    return state


def propose_gate(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """
    Submits a code evolution proposal to the Approval Gate.
    Hard-stops if validate_code_safety() fails.
    """
    from tools.google_sheets import append_row
    from tools.webhook_sender import post_to_webhook

    candidate_code: str = state.get("candidate_code") or ""  # type: ignore[assignment]
    if not candidate_code:
        return state

    safety = validate_code_safety(candidate_code)
    if not safety["passed"]:
        state["hard_stop_triggered"] = True
        _log_hard_stop(state, f"BLOCKED_STATIC: {safety['reason']}")
        return state

    sha256 = hashlib.sha256(candidate_code.encode()).hexdigest()
    proposal_id = str(uuid.uuid4())

    msg = state.get("incoming_message")
    payload = msg.payload if (msg and msg.payload) else {}

    agent_id_str: str = state.get("candidate_agent_id") or ""  # type: ignore[assignment]
    proposal = ApprovalProposal(
        id=proposal_id,
        agent_id=agent_id_str,
        issue=payload.get("issue", payload.get("description", "")),
        trigger_reason=payload.get("trigger_reason", "EVOLUTION_REQUEST"),
        stopping_constraint=payload.get("stopping_constraint", ""),
        iterations_run=state.get("iteration_count", 0),
        total_cost_usd=state.get("cost_usd", 0.0),
        proposed_code=candidate_code,
        code_sha256=sha256,
    )

    # ── Quality gate: coherence check before writing to Agent_Approvals ───────
    coherence = _validate_proposal_coherence(proposal, state["project_id"])
    if not coherence["passed"]:
        _log_cloud(
            "nexus-prime",
            state["project_id"],
            "task",
            state.get("task_id", ""),
            f"propose_gate: coherence warning — {coherence['reason']}",
            "WARNING",
        )
        # Append the warning to stopping_constraint so the owner sees it in-Sheet.
        suffix = f" [QUALITY WARNING: {coherence['reason'][:120]}]"
        proposal = proposal.model_copy(
            update={"stopping_constraint": (proposal.stopping_constraint + suffix).strip()}
        )
    for warn_msg in coherence.get("warnings", []):
        _log_cloud(
            "nexus-prime",
            state["project_id"],
            "task",
            state.get("task_id", ""),
            f"propose_gate: {warn_msg}",
            "WARNING",
        )

    row = proposal.to_sheet_row()

    try:
        cb_check("nexus-prime", "google-sheets")
        append_row("Agent_Approvals", row, state["project_id"])
        cb_success("nexus-prime", "google-sheets")
        # Store proposal ID (str) consistent with AgentWorkingMemory.parked_proposals
        state["parked_proposals"].append(proposal_id)
        state["candidate_sha256"] = sha256
        post_to_webhook(row, state["project_id"])

        # Notify owner via Google Chat card with strategic reasoning
        from config import get_settings
        from tools.google_chat import send_approval_card

        settings = get_settings()
        owner_space: str = getattr(settings.chat, "owner_space", "") or ""
        if owner_space:
            monologue: dict = state.get("monologue_frame") or {}  # type: ignore[assignment]
            try:
                send_approval_card(
                    space_name=owner_space,
                    proposal_id=proposal_id,
                    agent_id=agent_id_str,
                    issue_summary=payload.get("issue", payload.get("description", ""))[:280],
                    proposed_action=(
                        f"Code evolution proposed by {agent_id_str}. "
                        "Review the Blueprint Doc for full context."
                    )[:280],
                    priority=getattr(msg, "priority", 3) if msg else 3,  # type: ignore[arg-type]
                    cost_usd=state.get("cost_usd", 0.0),  # type: ignore[arg-type]
                    reasoning_summary=monologue.get("reasoning_summary", ""),
                )
            except Exception as card_exc:
                _log_cloud(
                    "nexus-prime",
                    state["project_id"],
                    "task",
                    state.get("task_id", ""),
                    f"propose_gate: Chat card failed (non-fatal): {card_exc}",
                    "WARNING",
                )
    except CircuitOpenError:
        _log_cloud(
            "nexus-prime",
            state["project_id"],
            "task",
            state.get("task_id", ""),
            "propose_gate: Google Sheets circuit open — approval proposal skipped",
            "ERROR",
        )
    except Exception as exc:
        cb_failure("nexus-prime", "google-sheets")
        _log_cloud(
            "nexus-prime",
            state["project_id"],
            "task",
            state.get("task_id", ""),
            f"propose_gate error: {exc}",
            "ERROR",
        )

    return state


def knowledge_review(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """
    Evaluate a KNOWLEDGE_CANDIDATE message. Promotes high-confidence
    unique entries immediately; sends uncertain ones to Pending_Knowledge.
    """
    from tools.memory import query_memory_bank, write_approved_memory

    msg = state.get("incoming_message")
    if msg is None:
        return state

    candidate = msg.payload or {}
    duplicates: list = []
    try:
        duplicates = query_memory_bank(
            query=candidate.get("content", ""),
            corpus=f"gaos-{candidate.get('domain', 'global')}",
            project_id=state["project_id"],
            top_k=5,
        )
    except Exception:
        pass

    prompt = _build_knowledge_review_prompt(candidate, duplicates)
    resp = _call_model(prompt, model=_model_for_node("knowledge_review"), parse_json=True)
    state["cost_usd"] = state.get("cost_usd", 0.0) + resp.cost_usd

    confidence = resp.data.get("confidence", 0.0)
    is_dup = resp.data.get("is_duplicate", True)

    if confidence >= 0.80 and not is_dup:
        try:
            from models import MemoryEntry

            entry = MemoryEntry(
                project_id=state["project_id"],
                agent_id=msg.source_agent,
                knowledge_type=candidate.get("knowledge_type", "fact"),
                domain=candidate.get("domain", "global"),
                content=candidate.get("content", ""),
                confidence=float(resp.data.get("confidence", 0.8)),
                approved_by="nexus-prime",
                approved_at=datetime.now(UTC),
                supersedes=resp.data.get("supersedes_memory_id") or None,
                tags=candidate.get("tags", []),
            )
            write_approved_memory(entry=entry, project_id=state["project_id"])
            supersession_reason = resp.data.get("supersession_reason") or None
            if entry.supersedes:
                reason_for_log = supersession_reason or "(no reason provided)"
                _log_cloud(
                    "nexus-prime",
                    state["project_id"],
                    "task",
                    state.get("task_id", ""),
                    f"SUPERSESSION_AUDIT: memory_id={entry.supersedes} retired by "
                    f"new entry (domain={entry.domain}). Reason: {reason_for_log}",
                    "INFO",
                )
            try:
                from tools.memory_mirror import MemoryMirrorError, sync_to_atlas

                sync_to_atlas(entry, supersession_reason=supersession_reason)
            except MemoryMirrorError as mirror_exc:
                _log_cloud(
                    "nexus-prime",
                    state["project_id"],
                    "task",
                    state.get("task_id", ""),
                    f"knowledge_review: Atlas sync failed (non-fatal): {mirror_exc}",
                    "WARNING",
                )
        except Exception:
            _write_to_pending_knowledge(state, candidate, resp)
    else:
        _write_to_pending_knowledge(state, candidate, resp)

    return state


def promote(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """
    Process an Approved signal. Verifies SHA-256 hash then triggers
    syncSkillsToVertex via Apps Script webhook.
    """
    from tools.google_sheets import find_row, update_row

    msg = state.get("incoming_message")
    if msg is None:
        return state

    proposal_id = msg.payload.get("proposal_id", "") if msg.payload else ""
    if not proposal_id:
        return state

    try:
        row = find_row("Agent_Approvals", "ID", proposal_id, state["project_id"])
    except Exception:
        return state

    if row is None:
        _log_cloud(
            "nexus-prime",
            state["project_id"],
            "security",
            state.get("task_id", ""),
            f"promote: proposal {proposal_id} not found",
            "ERROR",
        )
        return state

    # Hash verification — reject if tampered
    live_sha = hashlib.sha256((row.get("Proposed Code") or "").encode()).hexdigest()
    stored_sha = row.get("code_sha256", "")
    if live_sha != stored_sha:
        _log_cloud(
            "nexus-prime",
            state["project_id"],
            "security",
            state.get("task_id", ""),
            f"CODE_HASH_MISMATCH proposal={proposal_id}",
            "CRITICAL",
        )
        try:
            update_row(
                "Agent_Approvals", proposal_id, {"Status": "Needs Revision"}, state["project_id"]
            )
        except Exception:
            pass
        return state

    _trigger_sync_to_vertex(row, state)

    try:
        update_row("Agent_Approvals", proposal_id, {"Status": "Deployed"}, state["project_id"])
    except Exception:
        pass

    state["parked_proposals"] = [p for p in state.get("parked_proposals", []) if p != proposal_id]
    return state


def init_project(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """
    Provision infrastructure for a new project namespace when a Pending
    row appears in the Project Registry.
    """
    from tools.google_sheets import get_all_records, update_row

    pid = state.get("project_id", "")
    try:
        registry = get_all_records("Project Registry", pid)
    except Exception:
        return state

    pending_rows = [r for r in registry if r.get("status") == "Pending"]
    if not pending_rows:
        return state

    row = pending_rows[0]  # Process one at a time
    new_pid = row.get("project_id", "")
    if not new_pid:
        return state

    try:
        new_sheet_id = _create_sheet_workbook(new_pid)
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            pid,
            "task",
            state.get("task_id", ""),
            f"init_project: sheet creation failed — {exc}",
            "ERROR",
        )
        return state

    try:
        new_folder_id = _create_drive_folder(new_pid)
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            pid,
            "task",
            state.get("task_id", ""),
            f"init_project: drive folder creation failed — {exc}",
            "ERROR",
        )
        return state

    try:
        update_row(
            "Project Registry",
            row.get("ID", new_pid),
            {
                "status": "Active",
                "sheet_workbook_id": new_sheet_id,
                "drive_folder_id": new_folder_id,
            },
            pid,
        )
    except Exception:
        pass

    state["pending_project_row"] = {
        **row,
        "sheet_workbook_id": new_sheet_id,
        "drive_folder_id": new_folder_id,
    }
    state["new_project_id"] = new_pid
    return state


def notify_agents(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """Broadcast PROJECT_INITIALIZED to all Tier 2 orchestrators."""
    from tools.pubsub import publish

    new_pid = state.get("new_project_id")
    if not new_pid:
        return state

    prow = state.get("pending_project_row") or {}
    broadcast = A2AMessage(
        source_agent="nexus-prime",
        target_agent="broadcast",
        project_id=state["project_id"],
        task_id=state.get("task_id", str(uuid.uuid4())),
        message_type=MessageType.BROADCAST,
        priority=2,
        payload={
            "action": "PROJECT_INITIALIZED",
            "new_project_id": new_pid,
            "sheet_workbook_id": prow.get("sheet_workbook_id", ""),
            "drive_folder_id": prow.get("drive_folder_id", ""),
        },
    )

    for agent in ("ledger", "beacon", "pursuit", "foreman", "steward", "scout"):
        try:
            publish(f"agent.{agent}.events", broadcast)
        except Exception:
            pass

    state.setdefault("active_broadcasts", []).append(broadcast)  # type: ignore[typeddict-item]
    return state


def conflict_resolve(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """Arbitrate cross-domain conflicts using DEEP_MODEL and broadcast resolution."""
    from tools.pubsub import publish

    for conflict in state.get("conflict_queue", []):
        prompt = _build_conflict_prompt(conflict)
        resp = _call_model(prompt, model=_model_for_node("conflict_resolve"), parse_json=True)
        state["cost_usd"] = state.get("cost_usd", 0.0) + resp.cost_usd

        broadcast = A2AMessage(
            source_agent="nexus-prime",
            target_agent="broadcast",
            project_id=state["project_id"],
            task_id=state.get("task_id", str(uuid.uuid4())),
            message_type=MessageType.BROADCAST,
            priority=2,
            payload={
                "action": "CONFLICT_RESOLVED",
                "entity_key": conflict.get("entity_key"),
                "resolution": resp.data.get("decision", resp.text[:200]),
                "rationale": resp.data.get("rationale", ""),
            },
        )

        for agent in ("ledger", "beacon", "pursuit", "foreman", "steward", "scout"):
            try:
                publish(f"agent.{agent}.events", broadcast)
            except Exception:
                pass

        state.setdefault("active_broadcasts", []).append(broadcast)  # type: ignore[typeddict-item]

    state["conflict_queue"] = []
    return state


def park_or_broadcast(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """Handle Needs-Revision or ambiguous approval responses."""
    from tools.google_sheets import update_row

    msg = state.get("incoming_message")
    if msg is None:
        return state

    proposal_id = msg.payload.get("proposal_id", "") if msg.payload else ""
    new_status = msg.payload.get("status", "Parked") if msg.payload else "Parked"

    if proposal_id:
        try:
            update_row("Agent_Approvals", proposal_id, {"Status": new_status}, state["project_id"])
        except Exception:
            pass

    return state


def record(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """
    Terminal node for every path. Writes BigQuery task_outcome and
    publishes a STATUS_UPDATE heartbeat. Uses LOCAL_MODEL for formatting.
    """
    from tools.bigquery import insert_row
    from tools.pubsub import publish

    msg = state.get("incoming_message")

    log_state_transition(
        agent_id="nexus-prime",
        project_id=state.get("project_id", ""),
        task_id=state.get("task_id", ""),
        from_state=AgentState.EXECUTION,
        to_state=AgentState.OBSERVATION,
        reason="terminal node reached",
    )

    outcome: dict[str, Any] = {
        "task_id": state.get("task_id", ""),
        "project_id": state.get("project_id", ""),
        "agent_id": "nexus-prime",
        "task_type": msg.message_type.value if msg else "TTL_SWEEP",
        "status": "hard_stop" if state.get("hard_stop_triggered") else "success",
        "error_fingerprint": (msg.payload or {}).get("error_fingerprint", "") if msg else "",
        "cost_usd": state.get("cost_usd", 0.0),
        "duration_seconds": _elapsed_seconds(dict(state)),  # type: ignore[arg-type]
        "timestamp": utcnow_iso(),
        "log_date": utcnow_date(),
    }

    try:
        cb_check("nexus-prime", "bigquery")
        insert_row("aos_logs.task_outcomes", outcome)
        cb_success("nexus-prime", "bigquery")
    except CircuitOpenError:
        _log_cloud(
            "nexus-prime",
            state.get("project_id", ""),
            "task",
            state.get("task_id", ""),
            "record: BigQuery circuit open — task_outcome not persisted",
            "WARNING",
        )
    except Exception:
        cb_failure("nexus-prime", "bigquery")
    else:
        try:
            checkpoint_state = dict(state)
            checkpoint_state.setdefault("agent_id", "nexus-prime")
            save_checkpoint("nexus-prime", state.get("project_id", ""), checkpoint_state)
        except Exception as _cp_exc:
            _log_cloud(
                "nexus-prime",
                state.get("project_id", ""),
                "task",
                state.get("task_id", ""),
                f"record: checkpoint skipped (invalid state): {_cp_exc}",
                "WARNING",
            )

    # When APPROVAL_RESULT (status=Rejected) arrives via the Chat card-click path,
    # the Sheet row has not been touched yet (unlike the Apps Script path where the
    # owner edits the Sheet first, then Apps Script fires the Pub/Sub message).
    # Update the row so Agent_Approvals stays in sync regardless of approval path.
    # This is idempotent — writing "Rejected" over an already-"Rejected" row is harmless.
    if (
        msg
        and msg.message_type == MessageType.APPROVAL_RESULT
        and (msg.payload or {}).get("status") == "Rejected"
    ):
        _rejection_payload = msg.payload or {}
        _rejection_proposal_id = _rejection_payload.get("proposal_id", "")
        if _rejection_proposal_id:
            from tools.google_sheets import update_row as _sheets_update_row

            try:
                _sheets_update_row(
                    "Agent_Approvals",
                    _rejection_proposal_id,
                    {
                        "Status": "Rejected",
                        "Approved By": _rejection_payload.get("approved_by", ""),
                    },
                    state["project_id"],
                )
                state["parked_proposals"] = [
                    p for p in state.get("parked_proposals", []) if p != _rejection_proposal_id
                ]
            except Exception as _exc:
                _log_cloud(
                    "nexus-prime",
                    state["project_id"],
                    "task",
                    state.get("task_id", ""),
                    f"record: Sheet update for Rejected proposal failed: {_exc}",
                    "WARNING",
                )

    # Don't re-publish a STATUS_UPDATE when already processing one — prevents
    # the nexus-prime.sub.events self-delivery loop.
    heartbeat_text = _format_heartbeat(state)
    if not (msg and msg.message_type == MessageType.STATUS_UPDATE):
        heartbeat = A2AMessage(
            source_agent="nexus-prime",
            target_agent="broadcast",
            project_id=state.get("project_id", ""),
            task_id=state.get("task_id", str(uuid.uuid4())),
            message_type=MessageType.STATUS_UPDATE,
            priority=1,
            payload={"summary": heartbeat_text},
        )

        try:
            publish("agent.nexus-prime.events", heartbeat)
        except Exception:
            pass

    _write_heartbeat(
        agent_id="nexus-prime",
        project_id=state.get("project_id", ""),
        status="hard_stop" if state.get("hard_stop_triggered") else "IDLE",
        objective=heartbeat_text[:255],
        open_proposals=len(state.get("parked_proposals", [])),
        last_error="" if not state.get("hard_stop_triggered") else "hard_stop triggered",
        tab="Main Control Plane",
    )

    return state


# ── Nightly archive job ───────────────────────────────────────────────────────


async def handle_archive(project_id: str) -> dict[str, Any]:
    """
    Nightly Sheet → BigQuery archive sweep.

    Called directly by POST /archive (Cloud Scheduler, 2AM daily).
    Not a graph node — runs outside the LangGraph state machine.

    Steps per GAOS-Manager-Spec.md §9.5:
      1. Summarize   — LOCAL_MODEL weekly aggregate → observability_weekly (Mondays only)
      2. Archive     — aged rows moved to BigQuery cold storage
      3. Delete      — successfully archived rows deleted from Sheet
      3.5 Distill   — LOCAL_MODEL distills last 24 h of logs per active agent → Pending_Knowledge
      4. Report      — one summary row appended to the Logs tab
      5. Alert       — ALERT published if any tab exceeds 25,000 rows

    Retention policy:
      Logs tab:        30 days → aos_logs.task_outcomes
      Error Logs tab:  30 days → aos_logs.evolution_tasks
      Agent_Approvals: 90 days (closed status only) → aos_logs.approval_history
    """
    from datetime import datetime, timedelta

    from config import get_settings
    from tools.bigquery import insert_rows as bq_insert_rows
    from tools.google_sheets import (
        append_row,
        get_all_records,
        get_all_records_with_row_numbers,
        init_sheets_client,
    )
    from tools.google_sheets import (
        delete_rows as sheets_delete_rows,
    )
    from tools.pubsub import publish

    settings = get_settings()
    init_sheets_client(project_id)
    now = datetime.now(UTC)
    cutoff_30 = now - timedelta(days=30)
    cutoff_90 = now - timedelta(days=90)
    task_id = str(uuid.uuid4())
    stats: dict[str, int] = {}
    cost_usd = 0.0

    def _parse_ts(ts_str: str) -> datetime:
        """Parse a timestamp string into a datetime object.
        Ensures the date is within BigQuery's allowed range.

        Args:
            ts_str: The timestamp string to parse.

        Returns:
            A datetime object within the allowed range.
        """
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except (ValueError, AttributeError):
            try:
                dt = datetime.strptime(ts_str.strip(), "%m/%d/%Y %H:%M:%S").replace(tzinfo=UTC)
            except ValueError:
                dt = datetime(1970, 1, 1, tzinfo=UTC)

        # Ensure the date is within BigQuery's allowed range
        min_date = datetime.now(tz=UTC) - timedelta(days=3650)
        max_date = datetime.now(tz=UTC) + timedelta(days=366)
        if dt < min_date or dt > max_date:
            dt = min_date if dt < min_date else max_date

        return dt

    # ── 0. Idempotency guard — skip if archive already ran within last 4 hours ─
    try:
        recent_logs = get_all_records("Logs", project_id)
        already_ran = any(
            r.get("agent_id") == "nexus-prime"
            and r.get("level") == "ARCHIVE"
            and _parse_ts(r.get("timestamp", "")) >= now - timedelta(hours=4)
            for r in recent_logs
        )
        if already_ran:
            _log_cloud(
                "nexus-prime",
                project_id,
                "task",
                task_id,
                "handle_archive skipped — already ran within last 4 hours",
                "WARNING",
            )
            return {"skipped": True, "reason": "archive already ran today", "task_id": task_id}
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"idempotency check failed (proceeding anyway): {exc}",
            "WARNING",
        )

    # ── 1. Weekly observability summary (Mondays only) ────────────────────────
    if now.weekday() == 0:
        try:
            logs_week = get_all_records("Logs", project_id)
            err_week = get_all_records("Error Logs", project_id)
            week_cutoff = now - timedelta(days=7)
            recent_logs = [r for r in logs_week if _parse_ts(r.get("timestamp", "")) >= week_cutoff]
            recent_errs = [r for r in err_week if _parse_ts(r.get("timestamp", "")) >= week_cutoff]
            top_agents = list({r["agent_id"] for r in recent_logs[:50] if r.get("agent_id")})
            summary_prompt = (
                f"Summarize in one sentence: week ending {now.strftime('%Y-W%U')}, "
                f"{len(recent_logs)} log entries, {len(recent_errs)} error entries. "
                f"Active agents: {', '.join(top_agents[:5]) or 'none'}. "
                "Format: 'Week of <date>: <N> entries, <summary>.'"
            )
            resp = _call_model(summary_prompt, model=settings.models.LOCAL_MODEL)
            cost_usd += resp.cost_usd
            bq_insert_rows(
                "aos_logs.observability_weekly",
                [
                    {
                        "week_label": now.strftime("%Y-W%U"),
                        "log_count": len(recent_logs),
                        "error_count": len(recent_errs),
                        "summary": resp.text.strip()[:1000],
                        "project_id": project_id,
                        "created_at": now.isoformat(),
                    }
                ],
            )
        except Exception as exc:
            _log_cloud(
                "nexus-prime",
                project_id,
                "task",
                task_id,
                f"archive weekly summary failed: {exc}",
                "WARNING",
            )

    # ── 2+3. Archive Logs tab → task_outcomes ────────────────────────────────
    try:
        log_numbered = get_all_records_with_row_numbers("Logs", project_id)
        aged_logs = [
            (rn, r) for rn, r in log_numbered if _parse_ts(r.get("timestamp", "")) < cutoff_30
        ]
        if aged_logs:
            bq_rows = [
                {
                    "task_id": task_id,
                    "project_id": r.get("project_id", project_id),
                    "agent_id": r.get("agent_id", ""),
                    "task_type": r.get("level", "LOG"),
                    "status": "archived",
                    "error_fingerprint": "",
                    "cost_usd": 0.0,
                    "duration_seconds": 0.0,
                    "timestamp": _parse_ts(r.get("timestamp", "")).isoformat(),
                    "log_date": _parse_ts(r.get("timestamp", "")).date().isoformat(),
                }
                for _, r in aged_logs
            ]
            # Deterministic insertId per row — BQ deduplicates retries within ~1 min window.
            log_row_ids = [
                hashlib.sha256(
                    f"{project_id}:{r.get('timestamp', '')}:{r.get('agent_id', '')}:{r.get('level', '')}".encode()
                ).hexdigest()
                for _, r in aged_logs
            ]
            try:
                bq_insert_rows("aos_logs.task_outcomes", bq_rows, row_ids=log_row_ids)
                sheets_delete_rows("Logs", [rn for rn, _ in aged_logs], project_id)
                stats["Logs"] = len(aged_logs)
            except Exception as exc:
                _log_cloud(
                    "nexus-prime",
                    project_id,
                    "task",
                    task_id,
                    f"archive Logs → BQ failed: {exc}",
                    "ERROR",
                )
                stats["Logs"] = 0
        else:
            stats["Logs"] = 0
    except Exception as exc:
        _log_cloud(
            "nexus-prime", project_id, "task", task_id, f"archive Logs read failed: {exc}", "ERROR"
        )
        stats["Logs"] = 0

    # ── Archive Error Logs tab → evolution_tasks ─────────────────────────────
    try:
        err_numbered = get_all_records_with_row_numbers("Error Logs", project_id)
        aged_errs = [
            (rn, r) for rn, r in err_numbered if _parse_ts(r.get("timestamp", "")) < cutoff_30
        ]
        if aged_errs:
            bq_rows = [
                {
                    "task_id": str(uuid.uuid4()),
                    "project_id": r.get("project_id", project_id),
                    "agent_id": r.get("agent_id", ""),
                    "error_type": r.get("error_type", ""),
                    "error_message": r.get("message", "")[:1000],
                    "iterations": 0,
                    "stopping_constraint": "archived",
                    "cost_usd": 0.0,
                    "timestamp": _parse_ts(r.get("timestamp", "")).isoformat(),
                    "log_date": _parse_ts(r.get("timestamp", "")).date().isoformat(),
                }
                for _, r in aged_errs
            ]
            # Deterministic insertId per row — guards against duplicate BQ inserts on retry.
            err_row_ids = [
                hashlib.sha256(
                    f"{project_id}:{r.get('timestamp', '')}:{r.get('agent_id', '')}:{r.get('error_type', '')}:{r.get('message', '')[:200]}".encode()
                ).hexdigest()
                for _, r in aged_errs
            ]
            try:
                bq_insert_rows("aos_logs.evolution_tasks", bq_rows, row_ids=err_row_ids)
                sheets_delete_rows("Error Logs", [rn for rn, _ in aged_errs], project_id)
                stats["Error Logs"] = len(aged_errs)
            except Exception as exc:
                _log_cloud(
                    "nexus-prime",
                    project_id,
                    "task",
                    task_id,
                    f"archive Error Logs → BQ failed: {exc}",
                    "ERROR",
                )
                stats["Error Logs"] = 0
        else:
            stats["Error Logs"] = 0
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"archive Error Logs read failed: {exc}",
            "ERROR",
        )
        stats["Error Logs"] = 0

    # ── Archive Agent_Approvals → approval_history ───────────────────────────
    _CLOSED_STATUSES = {"Approved", "Rejected", "Deployed"}
    try:
        approval_numbered = get_all_records_with_row_numbers("Agent_Approvals", project_id)
        aged_approvals = [
            (rn, r)
            for rn, r in approval_numbered
            if r.get("Status", "") in _CLOSED_STATUSES
            and _parse_ts(r.get("Timestamp", "")) < cutoff_90
        ]
        if aged_approvals:
            bq_rows = [
                {
                    "proposal_id": r.get("ID", ""),
                    "project_id": project_id,
                    "agent_id": r.get("Agent ID", ""),
                    "issue": r.get("Issue", "")[:500],
                    "status": r.get("Status", ""),
                    "approved_by": r.get("Approved By", ""),
                    "approver_tier": int(r.get("Approver Tier", 0) or 0),
                    "cost_usd": float(r.get("Total Cost USD", 0) or 0),
                    "code_sha256": r.get("code_sha256", ""),
                    "timestamp": _parse_ts(r.get("Timestamp", "")).isoformat(),
                    "log_date": _parse_ts(r.get("Timestamp", "")).date().isoformat(),
                }
                for _, r in aged_approvals
            ]
            try:
                bq_insert_rows("aos_logs.approval_history", bq_rows)
                sheets_delete_rows("Agent_Approvals", [rn for rn, _ in aged_approvals], project_id)
                stats["Agent_Approvals"] = len(aged_approvals)
            except Exception as exc:
                _log_cloud(
                    "nexus-prime",
                    project_id,
                    "task",
                    task_id,
                    f"archive Agent_Approvals → BQ failed: {exc}",
                    "ERROR",
                )
                stats["Agent_Approvals"] = 0
        else:
            stats["Agent_Approvals"] = 0
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"archive Agent_Approvals read failed: {exc}",
            "ERROR",
        )
        stats["Agent_Approvals"] = 0

    # ── 3.5 Progressive episodic distillation ─────────────────────────────────
    # After archives trim the Logs tab to recent-only rows, read them back and
    # build one candidate Pending_Knowledge entry per agent that logged ≥ 5
    # messages in the last 24 h.  Uses LOCAL_MODEL for distillation; writes to
    # Pending_Knowledge via flush_observations() so human approval is required
    # before anything reaches the Memory Bank.
    _AGENT_DOMAIN: dict[str, str] = {
        "beacon": "marketing",
        "ledger": "accounting",
        "pursuit": "sales",
        "foreman": "operations",
        "steward": "admin",
        "scout": "research",
        "nexus-prime": "global",
    }
    _DISTILL_MIN_ENTRIES = 5
    distilled_count = 0
    try:
        from tools.memory import flush_observations

        yesterday = now - timedelta(hours=24)
        recent_logs = get_all_records("Logs", project_id)
        # Group recent log messages by agent_id
        agent_messages: dict[str, list[str]] = {}
        for row in recent_logs:
            if _parse_ts(row.get("timestamp", "")) < yesterday:
                continue
            aid = row.get("agent_id", "").strip()
            if not aid:
                continue
            agent_messages.setdefault(aid, []).append(row.get("message", ""))

        for aid, messages in agent_messages.items():
            if len(messages) < _DISTILL_MIN_ENTRIES:
                continue
            domain = _AGENT_DOMAIN.get(aid, "global")
            excerpt = "\n".join(f"- {m}" for m in messages[:30])[:2000]
            distill_prompt = (
                f"You are a knowledge management system for a multi-agent AI platform.\n"
                f"Agent: {aid} | Domain: {domain}\n\n"
                f"Recent activity log (last 24 h):\n{excerpt}\n\n"
                f"Summarize 2-3 concise, actionable lessons this agent should remember "
                f"about its domain. Plain bullets only, no headings."
            )
            try:
                distill_resp = _call_model(distill_prompt, model=settings.models.LOCAL_MODEL)
                cost_usd += distill_resp.cost_usd
                summary_text = distill_resp.text.strip()[:1000]
                if not summary_text:
                    continue
                raw_key = f"{aid}:{domain}:{summary_text}"
                content_hash = hashlib.sha256(raw_key.encode()).hexdigest()[:16]
                knowledge_id = str(uuid.uuid4())
                obs_now = now.isoformat()
                flush_observations(
                    [
                        {
                            "knowledge_id": knowledge_id,
                            "content_hash": content_hash,
                            "agent_id": aid,
                            "project_id": project_id,
                            "knowledge_type": "pattern",
                            "domain": domain,
                            "content": summary_text,
                            "evidence": "",
                            "confidence": 0.25,
                            "observation_count": 1,
                            "status": "Buffered",
                            "proposed_at": obs_now,
                            "last_seen_at": obs_now,
                            "approved_by": "",
                            "approved_at": "",
                            "rejection_reason": "",
                            "promoted_memory_id": "",
                        }
                    ],
                    project_id,
                )
                distilled_count += 1
            except Exception as exc_inner:
                _log_cloud(
                    "nexus-prime",
                    project_id,
                    "task",
                    task_id,
                    f"distillation failed for agent {aid}: {exc_inner}",
                    "WARNING",
                )
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"progressive distillation step failed: {exc}",
            "WARNING",
        )

    # ── 4. Report ─────────────────────────────────────────────────────────────
    total_archived = sum(stats.values())
    report_msg = (
        f"NIGHTLY_ARCHIVE complete: {total_archived} rows archived, "
        f"{distilled_count} agents distilled. "
        + " | ".join(f"{tab}={n}" for tab, n in stats.items())
    )
    try:
        append_row(
            "Logs",
            {
                "timestamp": now.isoformat(),
                "agent_id": "nexus-prime",
                "level": "ARCHIVE",
                "message": report_msg,
                "project_id": project_id,
            },
            project_id,
        )
    except Exception:
        pass

    # ── 5. Alert if any monitored tab exceeds 25,000 rows ────────────────────
    _ALERT_THRESHOLD = 25_000
    for tab in ("Logs", "Error Logs", "Agent_Approvals", "Pending_Knowledge"):
        try:
            count = len(get_all_records(tab, project_id))
            if count > _ALERT_THRESHOLD:
                alert = A2AMessage(
                    source_agent="nexus-prime",
                    target_agent="nexus-prime",
                    project_id=project_id,
                    task_id=task_id,
                    message_type=MessageType.ALERT,
                    priority=3,
                    payload={
                        "tab": tab,
                        "row_count": count,
                        "reason": f"Tab '{tab}' has {count} rows (threshold: {_ALERT_THRESHOLD})",
                    },
                )
                publish("agent.nexus-prime.events", alert)
        except Exception:
            pass

    _log_cloud("nexus-prime", project_id, "task", task_id, report_msg)

    return {
        "archived": stats,
        "total": total_archived,
        "cost_usd": round(cost_usd, 6),
        "task_id": task_id,
    }


# ── Morning briefing job ──────────────────────────────────────────────────────


async def handle_daily_sync(project_id: str) -> dict[str, Any]:
    """
    Morning briefing — called by POST /daily-sync (Cloud Scheduler, 6 AM daily).
    Not a graph node — runs outside the LangGraph state machine.

    Steps:
      1. Query overnight Logs tab (last 24 h)
      2. Query overnight Error Logs tab (last 24 h)
      3. Query pending rows in Agent_Approvals tab
      4. Compose a Chat Card v2 briefing card
      5. Send card to settings.chat.owner_space via send_card()
      6. Return summary dict

    Spec: GAOS-Manager-Spec.md §2.5 (Phase 2.5 Step 2)
    """
    from datetime import datetime, timedelta

    from config import get_settings
    from tools.google_chat import send_card
    from tools.google_sheets import get_all_records, init_sheets_client

    settings = get_settings()
    init_sheets_client(project_id)
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=24)
    task_id = str(uuid.uuid4())

    def _parse_ts(ts_str: str) -> datetime:
        """Parse timestamp; handles ISO and Google Sheets M/D/YYYY H:MM:SS format."""
        if not ts_str:
            return datetime(1970, 1, 1, tzinfo=UTC)
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except (ValueError, AttributeError):
            pass
        try:
            return datetime.strptime(ts_str.strip(), "%m/%d/%Y %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            return datetime(1970, 1, 1, tzinfo=UTC)

    # ── 1. Overnight Logs ────────────────────────────────────────────────────
    try:
        all_logs = get_all_records("Logs", project_id)
        overnight_logs = [r for r in all_logs if _parse_ts(r.get("timestamp", "")) >= cutoff]
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"daily-sync: failed to read Logs tab: {exc}",
            "WARNING",
        )
        overnight_logs = []

    # ── 2. Overnight Error Logs ───────────────────────────────────────────────
    try:
        all_errors = get_all_records("Error Logs", project_id)
        overnight_errors = [r for r in all_errors if _parse_ts(r.get("timestamp", "")) >= cutoff]
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"daily-sync: failed to read Error Logs tab: {exc}",
            "WARNING",
        )
        overnight_errors = []

    # ── 3. Pending approvals ─────────────────────────────────────────────────
    try:
        all_approvals = get_all_records("Agent_Approvals", project_id)
        pending = [r for r in all_approvals if r.get("status", "").lower() in ("pending", "")]
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"daily-sync: failed to read Agent_Approvals tab: {exc}",
            "WARNING",
        )
        pending = []

    # ── 4. Compose briefing card ─────────────────────────────────────────────
    date_str = now.strftime("%A, %B %d, %Y")
    active_agents = sorted({r.get("agent_id", "") for r in overnight_logs if r.get("agent_id")})
    agent_list = ", ".join(active_agents[:5]) or "none"

    activity_section: dict = {
        "header": "Overnight Activity (last 24 h)",
        "widgets": [
            {
                "textParagraph": {
                    "text": (
                        f"📋 <b>{len(overnight_logs)}</b> log "
                        f"{'entry' if len(overnight_logs) == 1 else 'entries'} "
                        f"across {len(active_agents)} agent(s): {agent_list}"
                    ),
                }
            },
            {
                "textParagraph": {
                    "text": (
                        f"{'⚠️' if overnight_errors else '✅'} "
                        f"<b>{len(overnight_errors)}</b> "
                        f"error{'s' if len(overnight_errors) != 1 else ''} logged overnight"
                    ),
                }
            },
        ],
    }

    # ── Build Sheet URL for clickable links ──────────────────────────────────
    workbook_id: str = getattr(settings.sheet, "workbook_id", "") or ""
    sheet_base_url = (
        f"https://docs.google.com/spreadsheets/d/{workbook_id}/edit" if workbook_id else ""
    )

    pending_text = (
        f"🔔 <b>{len(pending)}</b> proposal(s) awaiting your approval"
        if pending
        else "✅ No pending approvals"
    )
    actions_widgets: list[dict] = [{"textParagraph": {"text": pending_text}}]
    if sheet_base_url:
        actions_buttons: list[dict] = [
            {
                "text": "Open Agent Approvals",
                "onClick": {"openLink": {"url": f"{sheet_base_url}#gid=0"}},
            }
        ]
        if overnight_errors:
            actions_buttons.append(
                {
                    "text": "View Error Logs",
                    "onClick": {"openLink": {"url": f"{sheet_base_url}#gid=1"}},
                }
            )
        actions_buttons.append(
            {
                "text": "View All Logs",
                "onClick": {"openLink": {"url": sheet_base_url}},
            }
        )
        actions_widgets.append({"buttonList": {"buttons": actions_buttons}})

    actions_section: dict = {
        "header": "Pending Actions",
        "widgets": actions_widgets,
    }

    card: dict = {
        "header": {
            "title": "Good morning! AOS Daily Briefing",
            "subtitle": date_str,
        },
        "sections": [activity_section, actions_section],
    }

    # ── 5. Send card ─────────────────────────────────────────────────────────
    owner_space = settings.chat.owner_space
    if not owner_space:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            "daily-sync: chat.owner_space not configured — briefing card not sent",
            "WARNING",
        )
    else:
        try:
            send_card(owner_space, card)
        except Exception as exc:
            _log_cloud(
                "nexus-prime",
                project_id,
                "task",
                task_id,
                f"daily-sync: failed to send briefing card: {exc}",
                "WARNING",
            )

    _log_cloud(
        "nexus-prime",
        project_id,
        "task",
        task_id,
        f"DAILY_SYNC complete: {len(overnight_logs)} logs, "
        f"{len(overnight_errors)} errors, {len(pending)} pending approvals",
    )

    return {
        "overnight_logs": len(overnight_logs),
        "overnight_errors": len(overnight_errors),
        "pending_approvals": len(pending),
        "space_name": owner_space,
        "task_id": task_id,
    }


# ── Vision workflow helpers ───────────────────────────────────────────────────


def _build_vision_prompt(vision_text: str, project_id: str) -> str:
    """Build the Blueprint Doc generation prompt from a raw vision statement."""
    return (
        f"You are Nexus-Prime, the AOS general manager. The owner has submitted the following "
        f"business vision:\n\n"
        f'"{vision_text}"\n\n'
        f"Generate a structured project blueprint in Markdown with the following sections:\n"
        f"## Objective\n"
        f"## Success Criteria\n"
        f"## Agents Involved\n"
        f"## Milestones\n"
        f"## Constraints\n"
        f"## Open Questions\n\n"
        f"The blueprint should be actionable, concise, and reference the AOS agent capabilities "
        f"(Beacon, Foreman, Scout, Steward, Ledger, Pursuit) where relevant. "
        f"Return ONLY the Markdown text — no code fences, no preamble."
    )


def _build_compaction_prompt(constraints: list[dict]) -> str:
    """Build the constraint compaction prompt for Gemini Flash."""
    lines = "\n".join(
        f'C{i + 1}: "{c.get("text", c.get("constraint_text", ""))}"'
        for i, c in enumerate(constraints)
    )
    return (
        f"Compress these {len(constraints)} design constraints into one concise "
        f"'Design Constraints' paragraph without losing any requirement:\n\n"
        f"{lines}\n\n"
        f"Return ONLY the paragraph text — no headings, no preamble."
    )


def vision_blueprint(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """
    Handle a VISION_SUBMITTED message.

    1. Read vision_text and submitter info from the incoming message payload.
    2. Call Gemini Pro to generate Blueprint Doc Markdown content.
    3. Create the Google Doc in the blueprints folder.
    4. Append a row to the Project_Incubator Sheet tab.
    5. Send an approval card to the owner's Chat space with a link to the Doc.
    6. Log success.

    Spec: GAOS-Manager-Spec.md §2.5 Step 5
    """
    from config import get_settings
    from tools.google_chat import send_approval_card
    from tools.google_docs import create_document
    from tools.google_sheets import append_row

    msg = state.get("incoming_message")
    if msg is None:
        return state

    payload = msg.payload or {}
    vision_text: str = payload.get("vision_text", "")
    submitted_by: str = payload.get("submitted_by", msg.source_agent)
    space_name: str = payload.get("space_name", "")
    project_id = state["project_id"]
    task_id = state.get("task_id", str(uuid.uuid4()))

    if not vision_text:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            "vision_blueprint: empty vision_text — skipping",
            "WARNING",
        )
        return state

    settings = get_settings()

    # ── 1. Generate blueprint content via Gemini ──────────────────────────────
    prompt = _build_vision_prompt(vision_text, project_id)
    try:
        resp = _call_model(prompt, model=settings.models.DEEP_MODEL, parse_json=False)
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"vision_blueprint: model call failed: {exc}",
            "ERROR",
        )
        if space_name:
            from tools.google_chat import send_threaded_reply

            try:
                send_threaded_reply(
                    space_name,
                    task_id,
                    "\U0001f9e0 Vision received but Blueprint generation failed. "
                    "Our team has been notified.",
                )
            except Exception:
                pass
        return state
    state["cost_usd"] = state.get("cost_usd", 0.0) + resp.cost_usd
    state["tokens_used"] = state.get("tokens_used", 0) + resp.tokens_used
    blueprint_content = resp.text.strip()

    # Append image-source note when the vision was extracted from a photo
    if payload.get("vision_source") == "image":
        image_ts = payload.get("image_submitted_at", utcnow_iso())
        blueprint_content += (
            f"\n\n---\n"
            f"_Generated from image submission on {image_ts} "
            f"(multimodal vision extraction — {settings.models.DEEP_MODEL})_"
        )
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"PRIORITY-2-COST-MONITOR vision_blueprint image_source tokens={resp.tokens_used} "
            f"submitted_by={submitted_by}",
            "INFO",
        )

    # ── 2. Create the Google Doc ──────────────────────────────────────────────
    doc_title = f"Blueprint — {vision_text[:60]}"
    blueprint_id = task_id  # use task_id as stable blueprint identifier
    doc_id = ""
    doc_url = ""
    doc_creation_error = ""
    try:
        doc_id = create_document(
            title=doc_title,
            project_id=project_id,
            folder_id=settings.docs.blueprints_folder_id,
            initial_content=blueprint_content,
        )
        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
        _log_cloud(
            "nexus-prime", project_id, "task", task_id, f"vision_blueprint: created doc {doc_id}"
        )
    except Exception as exc:
        doc_creation_error = str(exc)
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"vision_blueprint: doc creation failed: {exc}",
            "ERROR",
        )

    # ── 3. Register blueprint in working memory ───────────────────────────────
    active: dict = state.get("active_blueprints") or {}  # type: ignore[assignment]
    active[blueprint_id] = doc_id
    state["active_blueprints"] = active
    state["blueprint_constraints"] = state.get("blueprint_constraints") or []  # type: ignore[assignment]

    # ── 4. Append row to Project_Incubator Sheet tab ──────────────────────────
    incubator_row = {
        "id": blueprint_id,
        "vision_text": vision_text[:500],
        "submitted_by": submitted_by,
        "submitted_at": utcnow_iso(),
        "status": "Pending Review",
        "doc_id": doc_id,
        "project_id": project_id,
    }
    try:
        append_row("Project_Incubator", incubator_row, project_id)
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"vision_blueprint: Sheet append failed: {exc}",
            "WARNING",
        )

    # ── 5. Send approval card to owner's Chat space ───────────────────────────
    owner_space = settings.chat.owner_space if settings.chat.owner_space else space_name
    if owner_space:
        try:
            send_approval_card(
                space_name=owner_space,
                proposal_id=blueprint_id,
                agent_id="nexus-prime",
                issue_summary=f"New vision submitted by {submitted_by}: {vision_text[:120]}",
                proposed_action="Blueprint Doc created. Review and approve to proceed.",
                priority=3,
                cost_usd=state.get("cost_usd", 0.0),
                doc_url=doc_url,
            )
        except Exception as exc:
            _log_cloud(
                "nexus-prime",
                project_id,
                "task",
                task_id,
                f"vision_blueprint: Chat card failed: {exc}",
                "WARNING",
            )

    # ── 6. Send threaded reply to the originating Chat space ─────────────────
    # The approval card goes to owner_space; this reply confirms receipt in the
    # original conversation thread so the sender knows the request was processed.
    if space_name:
        from tools.google_chat import send_threaded_reply

        try:
            thread_key = task_id
            if doc_url:
                reply_text = (
                    f"\U0001f9e0 Vision received and Blueprint Doc created!\n"
                    f"Review it here: {doc_url}\n"
                    f"An approval card has been sent for your review."
                )
            else:
                reply_text = (
                    "\U0001f9e0 Vision received but Blueprint Doc creation failed.\n"
                    f"Error: {doc_creation_error or 'unknown'}"
                )
            send_threaded_reply(space_name, thread_key, reply_text)
        except Exception as exc:
            _log_cloud(
                "nexus-prime",
                project_id,
                "task",
                task_id,
                f"vision_blueprint: threaded reply failed: {exc}",
                "WARNING",
            )

    _log_cloud(
        "nexus-prime",
        project_id,
        "task",
        task_id,
        f"vision_blueprint complete: blueprint_id={blueprint_id} doc_id={doc_id}",
    )
    return state


# ── Constraint compaction ─────────────────────────────────────────────────────


def _run_compaction(
    state: NexusPrimeWorkingMemory,
    blueprint_id: str,
    constraints: list[dict],
) -> str:
    """
    Compact *constraints* into a single paragraph using Gemini Flash.
    Archives originals to BigQuery and returns the compacted paragraph text.

    Spec: SCRATCH.md §ITERATE_PLAN Constraint Compaction Scheme
    """
    from config import get_settings
    from tools.bigquery import insert_rows

    settings = get_settings()
    project_id = state["project_id"]
    task_id = state.get("task_id", "")
    compacted_at = utcnow_iso()

    prompt = _build_compaction_prompt(constraints)
    resp = _call_model(prompt, model=settings.models.FAST_MODEL, parse_json=False)
    state["cost_usd"] = state.get("cost_usd", 0.0) + resp.cost_usd
    compacted_text = resp.text.strip()

    # Archive originals to BigQuery
    bq_rows = [
        {
            "blueprint_id": blueprint_id,
            "constraint_text": c.get("text", c.get("constraint_text", "")),
            "comment_author": c.get("comment_author", ""),
            "comment_timestamp": c.get("comment_timestamp", compacted_at),
            "compacted_at": compacted_at,
        }
        for c in constraints
    ]
    try:
        insert_rows("aos_logs.blueprint_constraints", bq_rows, project_id)
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"_run_compaction: BQ archive failed: {exc}",
            "WARNING",
        )

    _log_cloud(
        "nexus-prime",
        project_id,
        "task",
        task_id,
        f"_run_compaction: compacted {len(constraints)} constraints for blueprint {blueprint_id}",
    )
    return compacted_text


def iterate_plan(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """
    Handle PLAN_REVIEW or COMMENT_RECEIVED messages.

    1. Extract constraint/comment text from the payload.
    2. Append to the active blueprint_constraints list.
    3. If count reaches _COMPACTION_THRESHOLD: compact → single paragraph
       and archive originals to BigQuery.
    4. Append the new/compacted constraint paragraph to the Blueprint Doc.

    Spec: GAOS-Manager-Spec.md §2.5 Step 5, SCRATCH.md §ITERATE_PLAN
    """
    from tools.google_docs import append_content

    msg = state.get("incoming_message")
    if msg is None:
        return state

    payload = msg.payload or {}
    blueprint_id: str = payload.get("blueprint_id", "")
    constraint_text: str = payload.get("constraint_text", payload.get("comment_text", ""))
    comment_author: str = payload.get("comment_author", msg.source_agent)
    comment_timestamp: str = payload.get("comment_timestamp", utcnow_iso())
    project_id = state["project_id"]
    task_id = state.get("task_id", str(uuid.uuid4()))

    if not constraint_text:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            "iterate_plan: empty constraint_text — skipping",
            "WARNING",
        )
        return state

    constraints: list[dict] = list(state.get("blueprint_constraints") or [])  # type: ignore[assignment]
    constraints.append(
        {
            "blueprint_id": blueprint_id,
            "text": constraint_text,
            "comment_author": comment_author,
            "comment_timestamp": comment_timestamp,
        }
    )

    doc_content_to_append = constraint_text

    # ── Compaction check ──────────────────────────────────────────────────────
    blueprint_constraints = [c for c in constraints if c.get("blueprint_id") == blueprint_id]
    if len(blueprint_constraints) >= _COMPACTION_THRESHOLD:
        compacted = _run_compaction(state, blueprint_id, blueprint_constraints)
        # Replace blueprint-specific constraints with the compacted entry
        other_constraints = [c for c in constraints if c.get("blueprint_id") != blueprint_id]
        n = len(blueprint_constraints)
        compacted_entry = {
            "blueprint_id": blueprint_id,
            "text": f"COMPACTED_CONSTRAINTS (from {n} reviewed comments): {compacted}",
            "comment_author": "nexus-prime",
            "comment_timestamp": utcnow_iso(),
        }
        constraints = other_constraints + [compacted_entry]
        doc_content_to_append = compacted_entry["text"]

    state["blueprint_constraints"] = constraints

    # ── Append to Blueprint Doc ───────────────────────────────────────────────
    active_blueprints: dict = state.get("active_blueprints") or {}  # type: ignore[assignment]
    doc_id = active_blueprints.get(blueprint_id, "")
    if doc_id:
        try:
            append_content(
                doc_id=doc_id,
                content=f"\n\n**Constraint ({utcnow_date()}):** {doc_content_to_append}",
                project_id=project_id,
            )
        except Exception as exc:
            _log_cloud(
                "nexus-prime",
                project_id,
                "task",
                task_id,
                f"iterate_plan: append_content failed for doc {doc_id}: {exc}",
                "WARNING",
            )

    _log_cloud(
        "nexus-prime",
        project_id,
        "task",
        task_id,
        f"iterate_plan complete: blueprint_id={blueprint_id} total_constraints={len(constraints)}",
    )
    return state


# ── Doc-comment poll standalone handler ──────────────────────────────────────


async def handle_poll_comments(project_id: str) -> dict[str, Any]:
    """
    Cloud Scheduler job handler — polls ``list_comments()`` for all active
    Blueprint Docs and publishes a ``COMMENT_RECEIVED`` Pub/Sub message for
    each new unresolved comment.

    Called every 5 minutes by the ``doc-comment-poll`` Scheduler job via
    ``POST /poll-comments``.

    Spec: GAOS-Manager-Spec.md §2.5 Step 5
    """
    from asyncio import Semaphore, gather

    from config import get_settings
    from tools.google_docs import list_comments
    from tools.google_sheets import get_all_records, init_sheets_client
    from tools.pubsub import publish

    get_settings()  # validate config is loadable
    init_sheets_client(project_id)
    task_id = str(uuid.uuid4())
    published = 0
    errors = 0

    # Retrieve active blueprints from the Project_Incubator tab
    incubator_rows: list[dict] = []
    try:
        incubator_rows = get_all_records("Project_Incubator", project_id)
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"poll_comments: failed to read Project_Incubator: {exc}",
            "WARNING",
        )

    active_docs = [
        (row.get("id", ""), row.get("doc_id", ""))
        for row in incubator_rows
        if row.get("doc_id") and row.get("status", "") not in ("Archived", "Rejected")
    ]

    semaphore = Semaphore(5)  # Limit concurrent tasks

    async def process_document(blueprint_id: str, doc_id: str) -> None:
        nonlocal published, errors
        try:
            async with semaphore:
                comments = list_comments(doc_id=doc_id, project_id=project_id)
        except Exception as exc:
            _log_cloud(
                "nexus-prime",
                project_id,
                "task",
                task_id,
                f"poll_comments: list_comments failed for doc {doc_id}: {exc}",
                "WARNING",
            )
            errors += 1
            return

        for comment in comments:
            if comment.get("resolved"):
                continue

            from models import A2AMessage, MessageType

            msg = A2AMessage(
                source_agent="doc-comment-poll",
                target_agent="nexus-prime",
                project_id=project_id,
                task_id=str(uuid.uuid4()),
                message_type=MessageType.COMMENT_RECEIVED,
                priority=2,
                payload={
                    "blueprint_id": blueprint_id,
                    "doc_id": doc_id,
                    "comment_id": comment.get("id", ""),
                    "constraint_text": comment.get("content", ""),
                    "comment_author": comment.get("author", ""),
                    "comment_timestamp": comment.get("created_time", utcnow_iso()),
                },
            )
            try:
                publish("agent.nexus-prime.events", msg)
                published += 1
            except Exception as exc:
                _log_cloud(
                    "nexus-prime",
                    project_id,
                    "task",
                    task_id,
                    f"poll_comments: publish failed for comment {comment.get('id')}: {exc}",
                    "WARNING",
                )
                errors += 1

    await gather(*(process_document(blueprint_id, doc_id) for blueprint_id, doc_id in active_docs))

    _log_cloud(
        "nexus-prime",
        project_id,
        "task",
        task_id,
        f"poll_comments complete: {published} published, {errors} errors, "
        f"{len(active_docs)} docs polled",
    )

    return {
        "docs_polled": len(active_docs),
        "comments_published": published,
        "errors": errors,
        "task_id": task_id,
    }


# ── handle_sheets_sync ────────────────────────────────────────────────────────

#: Sheet tabs and their corresponding BigQuery staging tables.
_SYNC_TABS: list[tuple[str, str]] = [
    ("Agent_Approvals", "aos_logs.staging_approvals"),
    ("Logs", "aos_logs.staging_logs"),
    ("Error Logs", "aos_logs.staging_errors"),
    ("Pending_Knowledge", "aos_logs.staging_pending_knowledge"),
]


def _normalize_header(header: str) -> str:
    """Normalize a Sheet header to a valid BigQuery column name.

    Rules: strip whitespace → lowercase → spaces to ``_`` → ``/`` to ``_``.

    Args:
        header: Raw Sheet column header string (e.g. ``"Approved By"``).

    Returns:
        Normalized column name safe for BigQuery (e.g. ``"approved_by"``).
    """
    return header.strip().lower().replace(" ", "_").replace("/", "_")


async def handle_sheets_sync(project_id: str) -> dict[str, Any]:
    """Sync 4 operational Sheet tabs to BigQuery staging tables for Grafana.

    Called by ``POST /sheets-sync`` (Cloud Scheduler, every 5 minutes).
    Not a graph node — runs outside the LangGraph state machine.

    Each tab is read in full, headers are normalized to valid BQ column names,
    a ``synced_at`` timestamp is injected, and the staging table is fully
    replaced via ``replace_rows()`` (DELETE WHERE TRUE + streaming insert).

    Tab failures are non-fatal: a WARNING is logged and the remaining tabs
    continue. The returned dict will include an ``"error"`` key for any tab
    that failed.

    Spec: GAOS-Manager-Spec.md §9.6 (Phase 5 Step 8)

    Args:
        project_id: Active GAOS project ID — passed through to all tool calls.

    Returns:
        Dict with one key per staging table mapped to row count (int), or
        ``{"error": str}`` for failed tabs. Also includes ``"synced_at"``
        (ISO timestamp) and ``"task_id"`` (UUID).
    """
    from datetime import UTC, datetime

    from tools.bigquery import replace_rows
    from tools.google_sheets import get_all_records, init_sheets_client

    task_id = str(uuid.uuid4())
    synced_at = datetime.now(UTC).isoformat()

    init_sheets_client(project_id)

    result: dict[str, Any] = {}

    for tab_name, staging_table in _SYNC_TABS:
        try:
            raw_rows: list[dict] = get_all_records(tab_name, project_id)

            normalized: list[dict[str, Any]] = []
            for row in raw_rows:
                normalized_row = {_normalize_header(k): v for k, v in row.items()}
                normalized_row["synced_at"] = synced_at
                normalized.append(normalized_row)

            replace_rows(staging_table, normalized, project_id)

            table_key = staging_table.split(".")[-1]
            result[table_key] = len(normalized)

            _log_cloud(
                "nexus-prime",
                project_id,
                "task",
                task_id,
                f"sheets-sync: {tab_name} → {staging_table} ({len(normalized)} rows)",
            )
        except Exception as exc:
            table_key = staging_table.split(".")[-1]
            result[table_key] = {"error": str(exc)}
            _log_cloud(
                "nexus-prime",
                project_id,
                "task",
                task_id,
                f"sheets-sync: {tab_name} failed — {exc}",
                "WARNING",
            )

    result["synced_at"] = synced_at
    result["task_id"] = task_id
    return result


def handle_skill_request(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """
    Handle a SKILL_REQUEST message — two sub-cases based on payload content.

    **Inbound request** (no ``status`` in payload):
    An agent encountered a ``ModuleNotFoundError`` on a library outside the
    import allowlist and needs owner approval to install it. This path:

    1. Posts a ``send_skill_import_card()`` to the owner's Chat space.
    2. Writes an audit row to the ``Agent_Approvals`` Sheet tab.
    3. Parks the ``proposal_id`` in ``state["parked_proposals"]``.

    **Resolution** (payload contains ``status: Approved | Rejected``):
    The owner tapped Install or Deny on the Chat card (routed here from
    ``main.py`` skill_approve / skill_reject CARD_CLICKED). This path:

    1. Updates the ``Agent_Approvals`` row to reflect the decision.
    2. Removes ``proposal_id`` from ``state["parked_proposals"]``.
    3. If Approved: publishes ``SKILL_REQUEST`` back to the requesting agent
       so its Write-Test-Refine loop can resume.
    4. If Rejected: publishes an ``ALERT`` to the requesting agent to trigger
       a hard-stop with reason ``skill_request_rejected``.

    Spec: SCRATCH.md §SKILL_REQUEST — Library Installation Approval Card
    """
    from config import get_settings
    from tools.google_chat import send_skill_import_card
    from tools.google_sheets import append_row, update_row
    from tools.pubsub import publish

    msg = state.get("incoming_message")
    if msg is None:
        return state

    payload = msg.payload or {}
    project_id = state["project_id"]
    task_id = state.get("task_id", str(uuid.uuid4()))
    status = payload.get("status", "")

    # ── Resolution path ───────────────────────────────────────────────────────
    if status in ("Approved", "Rejected"):
        proposal_id = payload.get("proposal_id", "")
        package_name = payload.get("package_name", "")
        approved_by = payload.get("approved_by", "")

        # Update audit row in Sheet
        if proposal_id:
            try:
                update_row(
                    "Agent_Approvals",
                    proposal_id,
                    {"Status": status, "Approved By": approved_by},
                    project_id,
                )
            except Exception as exc:
                _log_cloud(
                    "nexus-prime",
                    project_id,
                    "task",
                    task_id,
                    f"handle_skill_request: update_row failed: {exc}",
                    "WARNING",
                )

            # Remove from parked list
            state["parked_proposals"] = [
                p for p in state.get("parked_proposals", []) if p != proposal_id
            ]

        # Determine requesting agent topic
        source_agent = msg.source_agent  # set to "google-chat" for card-click path
        # The actual requesting agent is stored in the payload when possible.
        # Use `or source_agent` so an empty/falsy agent_id falls back correctly.
        requesting_agent = payload.get("agent_id") or source_agent
        topic = f"agent.{requesting_agent}.events"

        if status == "Approved":
            reply = A2AMessage(
                source_agent="nexus-prime",
                target_agent=requesting_agent,
                project_id=project_id,
                task_id=task_id,
                message_type=MessageType.SKILL_REQUEST,
                priority=3,
                payload={
                    "status": "Approved",
                    "proposal_id": proposal_id,
                    "package_name": package_name,
                    "approved_by": approved_by,
                },
            )
            try:
                publish(topic, reply)
            except Exception as exc:
                _log_cloud(
                    "nexus-prime",
                    project_id,
                    "task",
                    task_id,
                    f"handle_skill_request: publish Approved failed: {exc}",
                    "WARNING",
                )
        else:
            alert = A2AMessage(
                source_agent="nexus-prime",
                target_agent=requesting_agent,
                project_id=project_id,
                task_id=task_id,
                message_type=MessageType.ALERT,
                priority=3,
                payload={
                    "status": "Rejected",
                    "proposal_id": proposal_id,
                    "package_name": package_name,
                    "reason": "skill_request_rejected",
                },
            )
            try:
                publish(topic, alert)
            except Exception as exc:
                _log_cloud(
                    "nexus-prime",
                    project_id,
                    "task",
                    task_id,
                    f"handle_skill_request: publish Rejected failed: {exc}",
                    "WARNING",
                )

        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"handle_skill_request: resolution {status} for proposal={proposal_id} "
            f"package={package_name} agent={requesting_agent}",
        )
        return state

    # ── Inbound request path ──────────────────────────────────────────────────
    package_name: str = payload.get("package_name", "")
    agent_id: str = payload.get("agent_id") or msg.source_agent
    reason: str = payload.get("reason", "")
    pypi_url: str = payload.get("pypi_url", "")

    if not package_name:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            "handle_skill_request: missing package_name — skipping",
            "WARNING",
        )
        return state

    proposal_id = str(uuid.uuid4())

    # Write audit row to Agent_Approvals
    row = {
        "ID": proposal_id,
        "Agent ID": agent_id,
        "Issue": f"Skill import request: {package_name}",
        "Trigger Reason": reason[:500]
        if reason
        else "ModuleNotFoundError in Write-Test-Refine loop",
        "Stopping Constraint": "Owner must approve before library is installed",
        "Iterations Run": 0,
        "Total Cost USD": 0.0,
        "Proposed Code": "",
        "Status": "Pending",
        "Timestamp": utcnow_iso(),
        "Approved By": "",
        "Approver Tier": 5,
        "code_sha256": "",
    }
    try:
        append_row("Agent_Approvals", row, project_id)
        state["parked_proposals"].append(proposal_id)
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"handle_skill_request: Sheet append failed: {exc}",
            "WARNING",
        )

    # Send Chat card to owner's space
    settings = get_settings()
    owner_space = settings.chat.owner_space
    if owner_space:
        try:
            send_skill_import_card(
                space_name=owner_space,
                proposal_id=proposal_id,
                agent_id=agent_id,
                package_name=package_name,
                reason=reason,
                pypi_url=pypi_url,
            )
        except Exception as exc:
            _log_cloud(
                "nexus-prime",
                project_id,
                "task",
                task_id,
                f"handle_skill_request: Chat card failed: {exc}",
                "WARNING",
            )
    else:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            "handle_skill_request: chat.owner_space not configured — card not sent",
            "WARNING",
        )

    _log_cloud(
        "nexus-prime",
        project_id,
        "task",
        task_id,
        f"handle_skill_request: inbound request parked proposal_id={proposal_id} "
        f"package={package_name} agent={agent_id}",
    )
    return state


def chat_respond(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """
    Conversational response node for CHAT_MESSAGE events.

    Calls FAST_MODEL with the Strategic Architect context trio and the user's
    chat message, then sends the reply back to the originating Chat space.
    Routes here from think() when msg_type is CHAT_MESSAGE.

    Spec: GAOS-Nexus-Prime-Spec.md §3.3 — chat_respond node
    """
    from config import get_settings
    from tools.google_chat import send_reply_in_thread, send_threaded_reply

    project_id = state.get("project_id", "")
    task_id = state.get("task_id", "")
    _log_cloud("nexus-prime", project_id, "task", task_id, "chat_respond: ENTRY")

    msg = state.get("incoming_message")
    if msg is None:
        _log_cloud("nexus-prime", project_id, "task", task_id, "chat_respond: no incoming_message")
        return state

    payload = msg.payload or {}
    user_text: str = payload.get("text", "")
    space_name: str = payload.get("space_name", "")
    message_name: str = payload.get("message_name", "")
    thread_name: str = payload.get("thread_name", "")
    project_id = state["project_id"]

    _log_cloud(
        "nexus-prime",
        project_id,
        "task",
        task_id,
        f"chat_respond: user_text={user_text[:50]!r} space={space_name!r}",
    )

    if not user_text or not space_name:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"chat_respond: BAIL — text empty={not user_text}, space empty={not space_name}",
            "WARNING",
        )
        return state

    settings = get_settings()
    context_trio = _load_context_trio()

    # Chat-specific formatting rules — Google Chat renders plain text, not Markdown
    chat_format_rules = (
        "\n\n--- CHAT FORMATTING RULES ---\n"
        "You are responding in Google Chat. Do NOT use Markdown formatting:\n"
        "- No ** for bold or emphasis\n"
        "- No * for italics\n"
        "- No ` for code\n"
        '- Use plain quotation marks " for quotes\n'
        "- Keep responses concise (under 500 characters when possible)\n"
        "- Do not acknowledge or echo the user's question — answer directly\n"
    )
    system_prompt = context_trio + chat_format_rules

    # Include active project summary for grounding
    system_state: dict = state.get("system_state_summary") or {}  # type: ignore[assignment]
    system_ctx = ""
    if system_state:
        projects = ", ".join(system_state.keys())
        system_ctx = f"\n\nActive GAOS projects: {projects}"

    prompt = f"{user_text}{system_ctx}"

    try:
        resp = _call_model(
            prompt,
            model=settings.models.LOCAL_MODEL,
            system_prompt=system_prompt,
        )
        reply = resp.text.strip() or "I'm processing your request."
        state["cost_usd"] = state.get("cost_usd", 0.0) + resp.cost_usd
        state["tokens_used"] = state.get("tokens_used", 0) + resp.tokens_used
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"chat_respond: model call failed — {exc}",
            "WARNING",
        )
        reply = "I'm having trouble processing your request right now. Please try again."

    try:
        if thread_name:
            # Use server-assigned thread name — replies in the user's actual thread.
            send_reply_in_thread(space_name, thread_name, reply)
        else:
            # Fallback: developer threadKey. May create a new thread on first use.
            thread_key = message_name or f"chat-{task_id}"
            send_threaded_reply(space_name, thread_key, reply)
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"chat_respond: replied to {space_name} ({len(reply)} chars)",
        )
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"chat_respond: send reply failed — {exc}",
            "WARNING",
        )

    return state


# ── Reactive cross-domain routing nodes ──────────────────────────────────────

#: Minimum gross-margin ratio below which Beacon ROI analysis is triggered.
_LOW_MARGIN_THRESHOLD: float = 0.20


def market_watchdog(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """
    React to a STOCK_INSUFFICIENT signal from Foreman by dispatching an
    urgent sourcing-research task to Scout.

    The original message payload is forwarded intact so Scout can read
    ``sku``, ``quantity_on_hand``, and any other context Foreman attached.
    Nexus-Prime wraps it in a ``MessageType.ALERT`` with
    ``alert_type = "stock_insufficient"`` — the form Scout's monitor already
    expects (see agents/scout/orchestrator.py _plan()).
    """
    from tools.pubsub import publish

    project_id = state.get("project_id", "")
    task_id = state.get("task_id", str(uuid.uuid4()))

    msg = state.get("incoming_message")
    if msg is None:
        return state

    forwarded_payload = {**(msg.payload or {}), "alert_type": "stock_insufficient"}
    sku = forwarded_payload.get("sku", "unknown")

    alert = A2AMessage(
        source_agent="nexus-prime",
        target_agent="scout",
        project_id=project_id,
        task_id=task_id,
        message_type=MessageType.ALERT,
        priority=4,
        payload=forwarded_payload,
    )
    try:
        publish("agent.scout.events", alert)
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"market_watchdog: STOCK_INSUFFICIENT for sku={sku!r} — Scout dispatched",
        )
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"market_watchdog: publish to Scout failed — {exc}",
            "ERROR",
        )

    return state


def roi_optimizer(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """
    React to a DEAL_CLOSED signal from Pursuit by checking gross margin.

    If the reported margin is below ``_LOW_MARGIN_THRESHOLD`` (default 20 %),
    dispatch a ``MessageType.ALERT`` with ``alert_type = "low_margin"`` to
    Beacon so it can analyse whether the marketing source is driving
    unprofitable leads.

    Expected payload keys from Pursuit:
        deal_id (str), lead_source (str), revenue (float), cogs (float)
    """
    from tools.pubsub import publish

    project_id = state.get("project_id", "")
    task_id = state.get("task_id", str(uuid.uuid4()))

    msg = state.get("incoming_message")
    if msg is None:
        return state

    payload = msg.payload or {}
    deal_id: str = payload.get("deal_id", "unknown")
    lead_source: str = payload.get("lead_source", "unknown")
    revenue: float = float(payload.get("revenue", 0.0) or 0.0)
    cogs: float = float(payload.get("cogs", 0.0) or 0.0)

    margin: float = ((revenue - cogs) / revenue) if revenue > 0 else 0.0

    if margin >= _LOW_MARGIN_THRESHOLD:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"roi_optimizer: deal_id={deal_id!r} margin={margin:.1%} — above threshold, no action",
        )
        return state

    alert = A2AMessage(
        source_agent="nexus-prime",
        target_agent="beacon",
        project_id=project_id,
        task_id=task_id,
        message_type=MessageType.ALERT,
        priority=3,
        payload={
            **payload,
            "alert_type": "low_margin",
            "margin_pct": round(margin * 100, 2),
            "threshold_pct": round(_LOW_MARGIN_THRESHOLD * 100, 2),
        },
    )
    try:
        publish("agent.beacon.events", alert)
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"roi_optimizer: deal_id={deal_id!r} lead_source={lead_source!r} "
            f"margin={margin:.1%} below threshold — Beacon dispatched",
        )
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"roi_optimizer: publish to Beacon failed — {exc}",
            "ERROR",
        )

    return state


# ── Graph assembly ────────────────────────────────────────────────────────────


def _infra_provision_node(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """LangGraph node adapter for handle_infra_provision().

    Extracts proposal_id, approved flag, and space_name from the incoming
    message payload and calls the async handler synchronously via asyncio.
    """
    import asyncio

    msg = state.get("incoming_message")
    if msg is None:
        return state

    from models import MessageType

    payload = msg.payload or {}
    proposal_id: str = payload.get("proposal_id", "")
    approved: bool = msg.message_type == MessageType.INFRA_PROVISION_APPROVED
    approved_by: str = payload.get("approved_by", "")
    space_name: str = payload.get("space_name", "")
    project_id: str = state.get("project_id", "")

    try:
        asyncio.get_event_loop().run_until_complete(
            handle_infra_provision(
                project_id=project_id,
                proposal_id=proposal_id,
                approved=approved,
                approved_by=approved_by,
                space_name=space_name,
            )
        )
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            proposal_id or "infra-provision",
            f"_infra_provision_node failed: {exc}",
            "ERROR",
        )
    return state


def handle_approval_request(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """
    Handles an APPROVAL_REQUEST from any domain agent (published to agent/approvals/events).

    The sending agent's _park() has already written the Agent_Approvals row and computed
    the code_sha256 hash.  This node notifies the owner via Google Chat card so they can
    review and approve or reject directly from the Agent_Approvals sheet.

    Args:
        state: Nexus-Prime working memory with incoming_message populated.

    Returns:
        Updated working memory (unchanged; this is a notification-only node).
    """
    from config import get_settings
    from tools.google_chat import send_approval_card

    msg = state.get("incoming_message")
    if msg is None:
        return state

    payload: dict = msg.payload or {}
    proposal_id: str = payload.get("proposal_id", msg.task_id or "")
    agent_id: str = msg.source_agent or "unknown"
    issue_summary: str = payload.get("description", payload.get("issue", ""))[:280]
    priority: int = getattr(msg, "priority", 3)

    try:
        settings = get_settings()
        owner_space: str = getattr(settings.chat, "owner_space", "") or ""
        if not owner_space:
            _log_cloud(
                "nexus-prime",
                state["project_id"],
                "task",
                state.get("task_id", ""),
                "handle_approval_request: owner_space not configured — Chat card skipped",
                "WARNING",
            )
            return state
        send_approval_card(
            space_name=owner_space,
            proposal_id=proposal_id,
            agent_id=agent_id,
            issue_summary=issue_summary,
            proposed_action=(
                f"Agent {agent_id} requests approval. "
                "Review the Agent_Approvals sheet to approve or reject."
            )[:280],
            priority=priority,
            cost_usd=0.0,
        )
        _log_cloud(
            "nexus-prime",
            state["project_id"],
            "task",
            state.get("task_id", ""),
            f"handle_approval_request: Chat card sent for proposal {proposal_id} from {agent_id}",
            "INFO",
        )
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            state["project_id"],
            "task",
            state.get("task_id", ""),
            f"handle_approval_request: Chat card failed — {exc}",
            "WARNING",
        )
    return state


def build_nexus_prime_graph() -> Any:
    """
    Assemble and compile the Nexus-Prime StateGraph per
    GAOS-Nexus-Prime-Spec.md §3.3.
    """
    graph: StateGraph = StateGraph(NexusPrimeWorkingMemory)

    graph.add_node("boot", boot)
    graph.add_node("monitor", monitor)
    graph.add_node("think", think)
    graph.add_node("chat_respond", chat_respond)
    graph.add_node("diagnose", diagnose)
    graph.add_node("propose_gate", propose_gate)
    graph.add_node("knowledge_review", knowledge_review)
    graph.add_node("promote", promote)
    graph.add_node("init_project", init_project)
    graph.add_node("notify_agents", notify_agents)
    graph.add_node("conflict_resolve", conflict_resolve)
    graph.add_node("park_or_broadcast", park_or_broadcast)
    graph.add_node("record", record)
    graph.add_node("vision_blueprint", vision_blueprint)
    graph.add_node("iterate_plan", iterate_plan)
    graph.add_node("handle_skill_request", handle_skill_request)
    graph.add_node("market_watchdog", market_watchdog)
    graph.add_node("roi_optimizer", roi_optimizer)
    graph.add_node("handle_infra_provision", _infra_provision_node)
    graph.add_node("handle_approval_request", handle_approval_request)

    graph.set_entry_point("boot")
    graph.add_edge("boot", "monitor")

    # monitor is the source of all conditional edges — route() is a pure routing
    # function (returns str) and must NOT be registered as a node (nodes must return dict).
    graph.add_conditional_edges(
        "monitor",
        route,
        {
            "think": "think",
            "diagnose": "diagnose",
            "knowledge_review": "knowledge_review",
            "init_project": "init_project",
            "conflict_resolve": "conflict_resolve",
            "promote": "promote",
            "park_or_broadcast": "park_or_broadcast",
            "record": "record",
            "vision_blueprint": "vision_blueprint",
            "iterate_plan": "iterate_plan",
            "handle_skill_request": "handle_skill_request",
            "handle_infra_provision": "handle_infra_provision",
            "market_watchdog": "market_watchdog",
            "roi_optimizer": "roi_optimizer",
            "handle_approval_request": "handle_approval_request",
        },
    )

    # think routes to diagnose, knowledge_review, chat_respond, or record
    graph.add_conditional_edges(
        "think",
        _route_from_think,
        {
            "diagnose": "diagnose",
            "knowledge_review": "knowledge_review",
            "chat_respond": "chat_respond",
            "record": "record",
        },
    )

    graph.add_edge("diagnose", "propose_gate")
    graph.add_edge("propose_gate", "record")
    graph.add_edge("knowledge_review", "record")
    graph.add_edge("chat_respond", "record")
    graph.add_edge("promote", "record")
    graph.add_edge("init_project", "notify_agents")
    graph.add_edge("notify_agents", "record")
    graph.add_edge("conflict_resolve", "record")
    graph.add_edge("park_or_broadcast", "record")
    graph.add_edge("vision_blueprint", "record")
    graph.add_edge("iterate_plan", "record")
    graph.add_edge("handle_skill_request", "record")
    graph.add_edge("handle_infra_provision", "record")
    graph.add_edge("handle_approval_request", "record")
    graph.add_edge("market_watchdog", "record")
    graph.add_edge("roi_optimizer", "record")
    graph.add_edge("record", END)

    return graph.compile(checkpointer=MemorySaver())


# ── ADK Agent class ───────────────────────────────────────────────────────────

try:
    from google.adk.agents import Agent as _BaseAgent

    _HAS_ADK = True
except ImportError:
    _HAS_ADK = False


if _HAS_ADK:

    class NexusPrimeAgent(_BaseAgent):  # type: ignore[misc]
        """
        Tier 1 Root Agent — ADK wrapper for Nexus-Prime.

        Entry point for Cloud Run HTTP invocations. Decodes the Pub/Sub
        push envelope, runs the compiled StateGraph, and returns the
        outcome summary.
        """

        name: str = "nexus-prime"
        description: str = (
            "Root AOS manager. Routes domain tasks to Tier 2 orchestrators, "
            "owns the Approval Gate, and governs self-evolution."
        )
        model: Any = ""  # type: ignore[assignment] — ADK accepts str at runtime
        instruction: Any = ""  # type: ignore[assignment]
        tools: list = []
        _graph: Any = None  # Compiled LangGraph StateGraph, set in __init__

        def __init__(self, **data: Any) -> None:
            from config import get_settings

            settings = get_settings()
            data["model"] = settings.models.DEEP_MODEL
            data["instruction"] = _load_identity_file("nexus-prime")
            super().__init__(**data)
            self._graph = build_nexus_prime_graph()

        async def run(self, agent_input: Any) -> Any:
            """
            Process one Pub/Sub push message.
            `agent_input` is the raw HTTP request body (dict) from Cloud Run.
            """
            from models import AgentOutput

            initial_state: NexusPrimeWorkingMemory = {  # type: ignore[assignment]
                "task_id": str(uuid.uuid4()),
                "project_id": "",
                "current_objective": "Processing incoming event",
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
                "_started_at": time.time(),
                "active_blueprints": {},
                "blueprint_constraints": [],
                "_next_node": "record",
                "monologue_frame": None,
            }

            try:
                final_state = await self._graph.ainvoke(
                    {**initial_state, "_raw_incoming": agent_input},
                    config={"configurable": {"thread_id": initial_state["task_id"]}},
                )
                status = "failed" if final_state.get("hard_stop_triggered") else "success"
            except Exception as exc:
                _log_cloud("nexus-prime", "", "task", initial_state["task_id"], str(exc), "ERROR")
                status = "failed"
                final_state = initial_state

            return AgentOutput(
                task_id=final_state.get("task_id", initial_state["task_id"]),
                project_id=final_state.get("project_id", ""),
                agent_id="nexus-prime",
                status=status,
                result={},
                cost_usd=final_state.get("cost_usd", 0.0),
            )

else:
    # Fallback stub when google-adk is not installed (CI / unit tests)
    class NexusPrimeAgent:  # type: ignore[no-redef]
        """Fallback: google-adk not available."""

        name = "nexus-prime"
        _graph = None

        def __init__(self, **_: Any) -> None:
            self._graph = build_nexus_prime_graph()

        async def run(self, agent_input: Any) -> Any:
            from models import AgentOutput

            initial_state: NexusPrimeWorkingMemory = {  # type: ignore[assignment]
                "task_id": str(uuid.uuid4()),
                "project_id": "",
                "current_objective": "Processing incoming event",
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
                "_started_at": time.time(),
                "active_blueprints": {},
                "blueprint_constraints": [],
                "_next_node": "record",
                "monologue_frame": None,
            }
            try:
                final_state = await self._graph.ainvoke(  # type: ignore[union-attr]
                    {**initial_state, "_raw_incoming": agent_input},
                    config={"configurable": {"thread_id": initial_state["task_id"]}},
                )
                status = "failed" if final_state.get("hard_stop_triggered") else "success"
            except Exception as exc:
                _log_cloud("nexus-prime", "", "task", initial_state["task_id"], str(exc), "ERROR")
                status = "failed"
                final_state = initial_state
            return AgentOutput(
                task_id=final_state.get("task_id", initial_state["task_id"]),
                project_id=final_state.get("project_id", ""),
                agent_id="nexus-prime",
                status=status,
                result={},
                cost_usd=final_state.get("cost_usd", 0.0),
            )


# Module-level compiled graph (used directly in Cloud Run handler if not via ADK)
nexus_prime_graph = build_nexus_prime_graph()


# ── handle_infra_plan ─────────────────────────────────────────────────────────


async def handle_infra_plan(
    project_id: str,
    space_name: str = "",
) -> dict[str, Any]:
    """Build an infrastructure diff manifest and send a Chat approval card.

    Called by ``POST /infra-provision`` (CLI trigger or CI/CD pipeline).
    Not a graph node — runs outside the LangGraph state machine.

    Steps:
      1. Resolve nexus-prime Cloud Run URL and SA email.
      2. Build the diff manifest via ``build_manifest()``.
      3. If no changes: send a plain-text "nothing to do" message and return.
      4. Write an ApprovalProposal row to Agent_Approvals (status=INFRA_PENDING).
      5. Send the infra proposal Chat card to the owner's space.

    Args:
        project_id: Active GAOS project ID.
        space_name: Override Chat space. Falls back to settings.chat.owner_space.

    Returns:
        Dict with ``proposal_id``, ``change_count``, and ``has_changes``.
    """
    import google.auth
    from googleapiclient.discovery import build as gapi_build

    from config import get_settings
    from tools.google_chat import send_infra_proposal_card, send_message
    from tools.google_sheets import append_row, init_sheets_client
    from tools.infra_provision import build_manifest

    settings = get_settings()
    owner_space = space_name or settings.chat.owner_space

    region = settings.gcp.region
    sa_email = f"nexus-prime-sa@{project_id}.iam.gserviceaccount.com"

    # Resolve the live Cloud Run URL for nexus-prime.
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    run_client = gapi_build("run", "v2", credentials=creds)
    svc_name = f"projects/{project_id}/locations/{region}/services/nexus-prime"
    try:
        svc = run_client.projects().locations().services().get(name=svc_name).execute()
        nexus_url = svc.get("uri", "")
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            "infra-plan",
            f"Could not resolve nexus-prime URL: {exc}",
            "WARNING",
        )
        nexus_url = ""

    task_id = str(uuid.uuid4())
    manifest = build_manifest(
        project_id=project_id,
        region=region,
        nexus_url=nexus_url,
        sa_email=sa_email,
    )

    if not manifest.has_changes:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            "infra-plan: no changes needed — all resources already up to date.",
        )
        if owner_space:
            send_message(owner_space, "✅ Infrastructure check complete — nothing to change.")
        return {"proposal_id": None, "change_count": 0, "has_changes": False}

    # Write proposal row to Agent_Approvals.
    init_sheets_client(project_id)
    from models import ApprovalProposal, ApprovalStatus

    proposal = ApprovalProposal(
        id=manifest.proposal_id,
        agent_id="infra-provisioner",
        issue="Infrastructure changes required — scheduler jobs, BQ tables, or secrets.",
        trigger_reason="Infra diff detected uncommitted resources",
        proposed_code=manifest.to_json(),  # manifest stored in proposed_code for retrieval
        status=ApprovalStatus.PENDING,
    )
    row = proposal.to_sheet_row()
    append_row("Agent_Approvals", list(row.values()), project_id)

    # Build human-readable change lines for the card.
    change_lines = [e.human_description for e in manifest.actionable]
    irreversible_warning = ""
    if manifest.has_irreversible:
        irreversible_warning = (
            "Creating BigQuery tables is permanent storage. "
            "New tables cannot be auto-deleted if they already contain data. "
            "If in doubt, reject and review the proposal_id in Agent_Approvals."
        )

    if owner_space:
        send_infra_proposal_card(
            space_name=owner_space,
            proposal_id=manifest.proposal_id,
            change_lines=change_lines,
            irreversible_warning=irreversible_warning,
        )

    _log_cloud(
        "nexus-prime",
        project_id,
        "task",
        task_id,
        f"infra-plan: card sent for proposal {manifest.proposal_id} "
        f"({len(manifest.actionable)} changes).",
    )
    return {
        "proposal_id": manifest.proposal_id,
        "change_count": len(manifest.actionable),
        "has_changes": True,
    }


# ── handle_infra_provision ────────────────────────────────────────────────────


async def handle_infra_provision(
    project_id: str,
    proposal_id: str,
    approved: bool,
    approved_by: str = "",
    space_name: str = "",
) -> dict[str, Any]:
    """Apply or reject an infrastructure change proposal.

    Called when the owner taps Approve or Reject on the infra proposal Chat card.
    Triggered via INFRA_PROVISION_APPROVED / INFRA_PROVISION_REJECTED MessageType
    routed from ``nexus_prime_route()`` in the graph.

    Approval path:
      1. Read manifest JSON from Agent_Approvals.proposed_code.
      2. Apply changes: secrets → BQ tables → scheduler jobs.
      3. Run targeted health checks on changed resources.
      4. If health check fails: rollback applied changes, send failure card.
      5. Update Agent_Approvals row status (Approved or Rejected).
      6. Send plain-language result card to owner.

    Rejection path:
      1. Update Agent_Approvals row status to Rejected.
      2. Send confirmation message to owner.

    Args:
        project_id:  Active GAOS project ID.
        proposal_id: UUID matching the row in Agent_Approvals.
        approved:    True if the owner approved; False if rejected.
        approved_by: Owner's email address from the Chat callback.
        space_name:  Chat space to send result card to.

    Returns:
        Dict with ``status``, ``proposal_id``, and optional ``rollback_notes``.
    """
    from config import get_settings
    from tools.google_chat import send_message
    from tools.google_sheets import get_all_records, init_sheets_client, update_row
    from tools.infra_provision import (
        InfraManifest,
        apply_manifest,
        rollback_manifest,
        run_health_checks,
    )

    settings = get_settings()
    owner_space = space_name or settings.chat.owner_space
    task_id = str(uuid.uuid4())
    init_sheets_client(project_id)

    # ── Find the proposal row ─────────────────────────────────────────────────
    rows = get_all_records("Agent_Approvals", project_id)
    proposal_row: dict[str, Any] | None = None
    row_number: int | None = None
    for i, row in enumerate(rows, start=2):  # row 1 = header
        if str(row.get("ID", "")) == proposal_id:
            proposal_row = row
            row_number = i
            break

    if proposal_row is None:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"handle_infra_provision: proposal {proposal_id} not found in Agent_Approvals",
            "ERROR",
        )
        if owner_space:
            send_message(
                owner_space,
                f"⚠️ Could not find infrastructure proposal {proposal_id[:8]}… "
                "It may have expired. Run the provisioner again.",
            )
        return {"status": "error", "proposal_id": proposal_id}

    # ── Rejection path ────────────────────────────────────────────────────────
    if not approved:
        if row_number is not None:
            update_row(
                "Agent_Approvals",
                row_number,
                {"Status": "Rejected", "Approved By": approved_by},
                project_id,
            )
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"infra-provision rejected by {approved_by}: proposal {proposal_id}",
        )
        if owner_space:
            send_message(
                owner_space,
                "❌ Infrastructure changes rejected. No changes have been made.",
            )
        return {"status": "rejected", "proposal_id": proposal_id}

    # ── Approval path — parse manifest ────────────────────────────────────────
    raw_manifest = proposal_row.get("Proposed Code", "") or proposal_row.get("proposed_code", "")
    try:
        manifest = InfraManifest.from_json(raw_manifest)
    except Exception as exc:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            f"handle_infra_provision: could not parse manifest JSON: {exc}",
            "ERROR",
        )
        if owner_space:
            send_message(
                owner_space,
                "⚠️ Could not read the infrastructure change details. "
                "The proposal may be malformed. No changes were made.",
            )
        return {"status": "error", "proposal_id": proposal_id}

    # Mark in-progress.
    if row_number is not None:
        update_row(
            "Agent_Approvals",
            row_number,
            {"Status": "Approved", "Approved By": approved_by},
            project_id,
        )

    # ── Apply ─────────────────────────────────────────────────────────────────
    apply_result = apply_manifest(manifest)

    _log_cloud(
        "nexus-prime",
        project_id,
        "task",
        task_id,
        f"infra-provision apply: {len(apply_result.applied)} applied, "
        f"{len(apply_result.failed)} failed",
    )

    # ── Health check ──────────────────────────────────────────────────────────
    all_passed, check_notes = run_health_checks(manifest)

    # ── Rollback if health check failed ───────────────────────────────────────
    rollback_notes: list[str] = []
    if not all_passed:
        _log_cloud(
            "nexus-prime",
            project_id,
            "task",
            task_id,
            "infra-provision health checks FAILED — triggering rollback",
            "ERROR",
        )
        rollback_notes = rollback_manifest(manifest, apply_result)
        if row_number is not None:
            update_row("Agent_Approvals", row_number, {"Status": "Needs Revision"}, project_id)

        if owner_space:
            fail_lines = "\n".join(f"• {n}" for n in check_notes if n.startswith("❌"))
            rb_lines = "\n".join(f"  {n}" for n in rollback_notes)
            send_message(
                owner_space,
                f"⚠️ Infrastructure changes were applied but health checks failed. "
                f"GAOS attempted to roll back automatically.\n\n"
                f"Failed checks:\n{fail_lines}\n\n"
                f"Rollback actions:\n{rb_lines}\n\n"
                "Review the Agent_Approvals tab for details.",
            )
        return {
            "status": "rolled_back",
            "proposal_id": proposal_id,
            "rollback_notes": rollback_notes,
        }

    # ── Success ───────────────────────────────────────────────────────────────
    _log_cloud(
        "nexus-prime",
        project_id,
        "task",
        task_id,
        f"infra-provision complete: proposal {proposal_id} — all checks passed.",
    )
    if owner_space:
        applied_summary = "\n".join(f"• {line}" for line in apply_result.applied)
        send_message(
            owner_space,
            f"✅ Infrastructure changes applied successfully.\n\n{applied_summary}\n\n"
            "All health checks passed. Grafana will show live data within 5 minutes.",
        )
    return {"status": "applied", "proposal_id": proposal_id}
