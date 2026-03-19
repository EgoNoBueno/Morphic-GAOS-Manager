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
from typing import Any, Optional

from langgraph.graph import END, StateGraph
from langgraph.checkpoint.memory import MemorySaver

from agents import (
    ModelResponse,
    _call_model,
    _elapsed_seconds,
    _load_identity_file,
    _log_cloud,
    _run_evolution_loop,
    utcnow_date,
    utcnow_iso,
    validate_code_safety,
    _write_heartbeat,
)
from models import A2AMessage, AgentWorkingMemory, ApprovalProposal, MessageType

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
    pending_project_row: Optional[dict]
    new_project_id: Optional[str]

    # Evolution gate
    candidate_code: Optional[str]
    candidate_agent_id: Optional[str]
    candidate_sha256: Optional[str]
    safety_check_passed: bool

    # System health
    system_state_summary: dict
    last_ttl_sweep_at: Optional[str]

    # Internal timing
    _started_at: float

    # Vision workflow (Phase 2.5 Step 5)
    active_blueprints: dict           # maps blueprint_id → doc_id
    blueprint_constraints: list[dict] # active constraint stack per blueprint


# ── Constraint compaction threshold ──────────────────────────────────────────

_COMPACTION_THRESHOLD = 5


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
        "Return JSON: {\"suggests_code_change\": bool, \"rationale\": str, \"fix_summary\": str}"
    )


def _build_knowledge_review_prompt(candidate: dict, duplicates: list) -> str:
    return (
        f"Candidate knowledge entry:\n{candidate.get('content', '')}\n\n"
        f"Potential duplicates ({len(duplicates)}) in Memory Bank:\n"
        + "\n".join(f"  - {d}" for d in duplicates[:5])
        + "\n\nAssess confidence and duplication.\n"
        "Return JSON: {\"confidence\": float, \"is_duplicate\": bool, \"rationale\": str}"
    )


def _build_conflict_prompt(conflict: dict) -> str:
    return (
        f"Conflicting state for entity '{conflict.get('entity_key', 'unknown')}':\n"
        f"  Agent A ({conflict.get('agent_a', '?')}): {conflict.get('value_a', '')}\n"
        f"  Agent B ({conflict.get('agent_b', '?')}): {conflict.get('value_b', '')}\n\n"
        "Arbitrate which value is correct. Return JSON: "
        "{\"decision\": str, \"rationale\": str, \"winning_agent\": str}"
    )


# ── Private helpers ───────────────────────────────────────────────────────────

def _compute_priority(state: NexusPrimeWorkingMemory) -> int:
    msg = state.get("incoming_message")
    if msg is None:
        return 3
    return getattr(msg, "priority", 3)


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
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    result = service.files().copy(
        fileId=template_id,
        body={"name": f"[{new_pid}] AOS Dashboard"},
    ).execute()
    return result.get("id", "")


def _create_drive_folder(new_pid: str) -> str:
    """Create a Knowledge/ Drive folder for a new project. Returns folder path key."""
    from tools.drive import write_file
    from config import get_settings
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


def boot(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """
    Runs once at service startup. Initialises subscriptions, loads parked
    proposals from Agent_Approvals, and validates credentials.
    """
    from config import get_settings
    from tools.google_sheets import get_all_records
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
    pid = state.get("project_id", settings.GCP_PROJECT_ID)
    state["project_id"] = pid

    # Ensure all Pub/Sub topics exist (idempotent)
    for topic in settings.pubsub.all_topics:
        try:
            ensure_topic_exists(topic, pid)
        except Exception:
            pass  # Non-fatal; topic may already exist in a different project

    # Load parked proposals (store IDs only, consistent with list[str])
    try:
        all_proposals = get_all_records("Agent_Approvals", pid)
        state["parked_proposals"] = [
            r["ID"] for r in all_proposals
            if r.get("Status") in ("Pending", "Needs Revision") and r.get("ID")
        ]
    except Exception:
        pass

    # Load Project Registry for system state summary
    try:
        registry = get_all_records("Project Registry", pid)
        state["system_state_summary"] = {
            r["project_id"]: r["status"]
            for r in registry
            if r.get("project_id")
        }
    except Exception:
        pass

    _log_cloud("nexus-prime", pid, "task", state.get("task_id", "boot"), "Boot complete")
    return state


def monitor(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    """Decode incoming Pub/Sub push envelope from state and populate working memory."""
    from tools.pubsub import decode_push_message

    raw = state.get("_raw_incoming")  # type: ignore[typeddict-item]
    if not raw:
        state["step_count"] = state.get("step_count", 0) + 1
        return state
    msg = decode_push_message(raw)
    state["incoming_message"] = msg
    state["project_id"] = msg.project_id
    state["task_id"] = msg.task_id or str(uuid.uuid4())
    state["step_count"] = state.get("step_count", 0) + 1
    return state


def route(state: NexusPrimeWorkingMemory) -> str:
    """Pure routing — DEEP_MODEL not required; no I/O."""
    msg = state.get("incoming_message")
    if msg is None:
        return "record"

    routing_table = {
        MessageType.STATUS_UPDATE:       "record",
        MessageType.TASK_COMPLETE:       "record",
        MessageType.ESCALATION:          "diagnose",
        MessageType.EVOLUTION_REQUEST:   "diagnose",
        MessageType.APPROVAL_RESULT:     "_route_approval",
        MessageType.KNOWLEDGE_CANDIDATE: "knowledge_review",
        MessageType.BROADCAST:           "conflict_resolve",
        MessageType.NEW_PROJECT:         "init_project",
        MessageType.VISION_SUBMITTED:    "vision_blueprint",
        MessageType.PLAN_REVIEW:         "iterate_plan",
        MessageType.COMMENT_RECEIVED:    "iterate_plan",
        MessageType.SKILL_REQUEST:       "handle_skill_request",
    }
    return routing_table.get(msg.message_type, "record")


def _route_approval(state: NexusPrimeWorkingMemory) -> str:
    """Sub-router for APPROVAL_RESULT messages."""
    result = (state.get("incoming_message") or {})
    status = getattr(result, "payload", {}).get("status", "") if hasattr(result, "payload") else ""
    if status == "Approved":
        return "promote"
    if status == "Rejected":
        return "record"
    return "park_or_broadcast"


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
    row = proposal.to_sheet_row()

    try:
        append_row("Agent_Approvals", row, state["project_id"])
        # Store proposal ID (str) consistent with AgentWorkingMemory.parked_proposals
        state["parked_proposals"].append(proposal_id)
        state["candidate_sha256"] = sha256
        post_to_webhook(row, state["project_id"])
    except Exception as exc:
        _log_cloud(
            "nexus-prime", state["project_id"], "task",
            state.get("task_id", ""), f"propose_gate error: {exc}", "ERROR"
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
                tags=candidate.get("tags", []),
            )
            write_approved_memory(entry=entry, project_id=state["project_id"])
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
        _log_cloud("nexus-prime", state["project_id"], "security",
                   state.get("task_id", ""), f"promote: proposal {proposal_id} not found", "ERROR")
        return state

    # Hash verification — reject if tampered
    live_sha = hashlib.sha256((row.get("Proposed Code") or "").encode()).hexdigest()
    stored_sha = row.get("code_sha256", "")
    if live_sha != stored_sha:
        _log_cloud(
            "nexus-prime", state["project_id"], "security",
            state.get("task_id", ""),
            f"CODE_HASH_MISMATCH proposal={proposal_id}",
            "CRITICAL",
        )
        try:
            update_row("Agent_Approvals", proposal_id, {"Status": "Needs Revision"}, state["project_id"])
        except Exception:
            pass
        return state

    _trigger_sync_to_vertex(row, state)

    try:
        update_row("Agent_Approvals", proposal_id, {"Status": "Deployed"}, state["project_id"])
    except Exception:
        pass

    state["parked_proposals"] = [
        p for p in state.get("parked_proposals", []) if p != proposal_id
    ]
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
        _log_cloud("nexus-prime", pid, "task", state.get("task_id", ""),
                   f"init_project: sheet creation failed — {exc}", "ERROR")
        return state

    try:
        new_folder_id = _create_drive_folder(new_pid)
    except Exception as exc:
        _log_cloud("nexus-prime", pid, "task", state.get("task_id", ""),
                   f"init_project: drive folder creation failed — {exc}", "ERROR")
        return state

    try:
        update_row("Project Registry", row.get("ID", new_pid), {
            "status": "Active",
            "sheet_workbook_id": new_sheet_id,
            "drive_folder_id": new_folder_id,
        }, pid)
    except Exception:
        pass

    state["pending_project_row"] = {**row, "sheet_workbook_id": new_sheet_id, "drive_folder_id": new_folder_id}
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
            publish(f"agent.{agent}.events", broadcast, state["project_id"])
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
                publish(f"agent.{agent}.events", broadcast, state["project_id"])
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
        insert_row("aos_logs.task_outcomes", outcome)
    except Exception:
        pass

    heartbeat_text = _format_heartbeat(state)
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
        publish("agent.nexus-prime.events", heartbeat, state["project_id"])
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
      1. Summarize — LOCAL_MODEL weekly aggregate → observability_weekly (Mondays only)
      2. Archive   — aged rows moved to BigQuery cold storage
      3. Delete    — successfully archived rows deleted from Sheet
      4. Report    — one summary row appended to the Logs tab
      5. Alert     — ALERT published if any tab exceeds 25,000 rows

    Retention policy:
      Logs tab:        30 days → aos_logs.task_outcomes
      Error Logs tab:  30 days → aos_logs.evolution_tasks
      Agent_Approvals: 90 days (closed status only) → aos_logs.approval_history
    """
    from datetime import datetime, timezone, timedelta
    from config import get_settings
    from tools.google_sheets import (
        get_all_records,
        get_all_records_with_row_numbers,
        delete_rows as sheets_delete_rows,
        append_row,
    )
    from tools.bigquery import insert_rows as bq_insert_rows
    from tools.pubsub import publish

    settings = get_settings()
    now = datetime.now(timezone.utc)
    cutoff_30 = now - timedelta(days=30)
    cutoff_90 = now - timedelta(days=90)
    task_id = str(uuid.uuid4())
    stats: dict[str, int] = {}
    cost_usd = 0.0

    def _parse_ts(ts_str: str) -> datetime:
        """Parse ISO timestamp; returns epoch on failure so malformed rows stay."""
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            return datetime(1970, 1, 1, tzinfo=timezone.utc)

    # ── 1. Weekly observability summary (Mondays only) ────────────────────────
    if now.weekday() == 0:
        try:
            logs_week = get_all_records("Logs", project_id)
            err_week = get_all_records("Error Logs", project_id)
            week_cutoff = now - timedelta(days=7)
            recent_logs = [r for r in logs_week if _parse_ts(r.get("timestamp", "")) >= week_cutoff]
            recent_errs = [r for r in err_week if _parse_ts(r.get("timestamp", "")) >= week_cutoff]
            top_agents = list({r.get("agent_id", "?") for r in recent_logs[:50] if r.get("agent_id")})
            summary_prompt = (
                f"Summarize in one sentence: week ending {now.strftime('%Y-W%U')}, "
                f"{len(recent_logs)} log entries, {len(recent_errs)} error entries. "
                f"Active agents: {', '.join(top_agents[:5]) or 'none'}. "
                "Format: 'Week of <date>: <N> entries, <summary>.'"
            )
            resp = _call_model(summary_prompt, model=settings.models.LOCAL_MODEL)
            cost_usd += resp.cost_usd
            bq_insert_rows("aos_logs.observability_weekly", [{
                "week_label": now.strftime("%Y-W%U"),
                "log_count": len(recent_logs),
                "error_count": len(recent_errs),
                "summary": resp.text.strip()[:1000],
                "project_id": project_id,
                "created_at": now.isoformat(),
            }])
        except Exception as exc:
            _log_cloud("nexus-prime", project_id, "task", task_id,
                       f"archive weekly summary failed: {exc}", "WARNING")

    # ── 2+3. Archive Logs tab → task_outcomes ────────────────────────────────
    try:
        log_numbered = get_all_records_with_row_numbers("Logs", project_id)
        aged_logs = [(rn, r) for rn, r in log_numbered
                     if _parse_ts(r.get("timestamp", "")) < cutoff_30]
        if aged_logs:
            bq_rows = [{
                "task_id": task_id,
                "project_id": r.get("project_id", project_id),
                "agent_id": r.get("agent_id", ""),
                "task_type": r.get("level", "LOG"),
                "status": "archived",
                "error_fingerprint": "",
                "cost_usd": 0.0,
                "duration_seconds": 0.0,
                "timestamp": r.get("timestamp", ""),
                "log_date": r.get("timestamp", "")[:10],
            } for _, r in aged_logs]
            try:
                bq_insert_rows("aos_logs.task_outcomes", bq_rows)
                sheets_delete_rows("Logs", [rn for rn, _ in aged_logs], project_id)
                stats["Logs"] = len(aged_logs)
            except Exception as exc:
                _log_cloud("nexus-prime", project_id, "task", task_id,
                           f"archive Logs → BQ failed: {exc}", "ERROR")
                stats["Logs"] = 0
        else:
            stats["Logs"] = 0
    except Exception as exc:
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   f"archive Logs read failed: {exc}", "ERROR")
        stats["Logs"] = 0

    # ── Archive Error Logs tab → evolution_tasks ─────────────────────────────
    try:
        err_numbered = get_all_records_with_row_numbers("Error Logs", project_id)
        aged_errs = [(rn, r) for rn, r in err_numbered
                     if _parse_ts(r.get("timestamp", "")) < cutoff_30]
        if aged_errs:
            bq_rows = [{
                "task_id": str(uuid.uuid4()),
                "project_id": r.get("project_id", project_id),
                "agent_id": r.get("agent_id", ""),
                "error_type": r.get("error_type", ""),
                "error_message": r.get("message", "")[:1000],
                "iterations": 0,
                "stopping_constraint": "archived",
                "cost_usd": 0.0,
                "timestamp": r.get("timestamp", ""),
                "log_date": r.get("timestamp", "")[:10],
            } for _, r in aged_errs]
            try:
                bq_insert_rows("aos_logs.evolution_tasks", bq_rows)
                sheets_delete_rows("Error Logs", [rn for rn, _ in aged_errs], project_id)
                stats["Error Logs"] = len(aged_errs)
            except Exception as exc:
                _log_cloud("nexus-prime", project_id, "task", task_id,
                           f"archive Error Logs → BQ failed: {exc}", "ERROR")
                stats["Error Logs"] = 0
        else:
            stats["Error Logs"] = 0
    except Exception as exc:
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   f"archive Error Logs read failed: {exc}", "ERROR")
        stats["Error Logs"] = 0

    # ── Archive Agent_Approvals → approval_history ───────────────────────────
    _CLOSED_STATUSES = {"Approved", "Rejected", "Deployed"}
    try:
        approval_numbered = get_all_records_with_row_numbers("Agent_Approvals", project_id)
        aged_approvals = [
            (rn, r) for rn, r in approval_numbered
            if r.get("Status", "") in _CLOSED_STATUSES
            and _parse_ts(r.get("Timestamp", "")) < cutoff_90
        ]
        if aged_approvals:
            bq_rows = [{
                "proposal_id": r.get("ID", ""),
                "project_id": project_id,
                "agent_id": r.get("Agent ID", ""),
                "issue": r.get("Issue", "")[:500],
                "status": r.get("Status", ""),
                "approved_by": r.get("Approved By", ""),
                "approver_tier": int(r.get("Approver Tier", 0) or 0),
                "cost_usd": float(r.get("Total Cost USD", 0) or 0),
                "code_sha256": r.get("code_sha256", ""),
                "timestamp": r.get("Timestamp", ""),
                "log_date": r.get("Timestamp", "")[:10],
            } for _, r in aged_approvals]
            try:
                bq_insert_rows("aos_logs.approval_history", bq_rows)
                sheets_delete_rows("Agent_Approvals", [rn for rn, _ in aged_approvals], project_id)
                stats["Agent_Approvals"] = len(aged_approvals)
            except Exception as exc:
                _log_cloud("nexus-prime", project_id, "task", task_id,
                           f"archive Agent_Approvals → BQ failed: {exc}", "ERROR")
                stats["Agent_Approvals"] = 0
        else:
            stats["Agent_Approvals"] = 0
    except Exception as exc:
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   f"archive Agent_Approvals read failed: {exc}", "ERROR")
        stats["Agent_Approvals"] = 0

    # ── 4. Report ─────────────────────────────────────────────────────────────
    total_archived = sum(stats.values())
    report_msg = (
        f"NIGHTLY_ARCHIVE complete: {total_archived} rows archived. "
        + " | ".join(f"{tab}={n}" for tab, n in stats.items())
    )
    try:
        append_row("Logs", {
            "timestamp": now.isoformat(),
            "agent_id": "nexus-prime",
            "level": "ARCHIVE",
            "message": report_msg,
            "project_id": project_id,
        }, project_id)
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
                publish("agent.nexus-prime.events", alert, project_id)
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
    from datetime import datetime, timezone, timedelta
    from config import get_settings
    from tools.google_sheets import get_all_records
    from tools.google_chat import send_card

    settings = get_settings()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    task_id = str(uuid.uuid4())

    def _parse_ts(ts_str: str) -> datetime:
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            return datetime(1970, 1, 1, tzinfo=timezone.utc)

    # ── 1. Overnight Logs ────────────────────────────────────────────────────
    try:
        all_logs = get_all_records("Logs", project_id)
        overnight_logs = [r for r in all_logs if _parse_ts(r.get("timestamp", "")) >= cutoff]
    except Exception as exc:
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   f"daily-sync: failed to read Logs tab: {exc}", "WARNING")
        overnight_logs = []

    # ── 2. Overnight Error Logs ───────────────────────────────────────────────
    try:
        all_errors = get_all_records("Error Logs", project_id)
        overnight_errors = [r for r in all_errors if _parse_ts(r.get("timestamp", "")) >= cutoff]
    except Exception as exc:
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   f"daily-sync: failed to read Error Logs tab: {exc}", "WARNING")
        overnight_errors = []

    # ── 3. Pending approvals ─────────────────────────────────────────────────
    try:
        all_approvals = get_all_records("Agent_Approvals", project_id)
        pending = [r for r in all_approvals if r.get("status", "").lower() in ("pending", "")]
    except Exception as exc:
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   f"daily-sync: failed to read Agent_Approvals tab: {exc}", "WARNING")
        pending = []

    # ── 4. Compose briefing card ─────────────────────────────────────────────
    date_str = now.strftime("%A, %B %d, %Y")
    active_agents = sorted({r.get("agent_id", "") for r in overnight_logs if r.get("agent_id")})
    agent_list = ", ".join(active_agents[:5]) or "none"

    activity_section: dict = {
        "header": "Overnight Activity (last 24 h)",
        "widgets": [
            {"textParagraph": {
                "text": (
                    f"📋 <b>{len(overnight_logs)}</b> log "
                    f"{'entry' if len(overnight_logs) == 1 else 'entries'} "
                    f"across {len(active_agents)} agent(s): {agent_list}"
                ),
            }},
            {"textParagraph": {
                "text": (
                    f"{'⚠️' if overnight_errors else '✅'} "
                    f"<b>{len(overnight_errors)}</b> "
                    f"error{'s' if overnight_errors != 1 else ''} logged overnight"
                ),
            }},
        ],
    }

    pending_text = (
        f"🔔 <b>{len(pending)}</b> proposal(s) awaiting your approval"
        if pending
        else "✅ No pending approvals"
    )
    actions_section: dict = {
        "header": "Pending Actions",
        "widgets": [{"textParagraph": {"text": pending_text}}],
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
            "nexus-prime", project_id, "task", task_id,
            "daily-sync: chat.owner_space not configured — briefing card not sent",
            "WARNING",
        )
    else:
        try:
            send_card(owner_space, card)
        except Exception as exc:
            _log_cloud("nexus-prime", project_id, "task", task_id,
                       f"daily-sync: failed to send briefing card: {exc}", "WARNING")

    _log_cloud(
        "nexus-prime", project_id, "task", task_id,
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
        f"\"{vision_text}\"\n\n"
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
        f"C{i + 1}: \"{c.get('text', c.get('constraint_text', ''))}\""
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
    from tools.google_docs import create_document
    from tools.google_sheets import append_row
    from tools.google_chat import send_approval_card

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
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   "vision_blueprint: empty vision_text — skipping", "WARNING")
        return state

    settings = get_settings()

    # ── 1. Generate blueprint content via Gemini ──────────────────────────────
    prompt = _build_vision_prompt(vision_text, project_id)
    resp = _call_model(prompt, model=settings.models.DEEP_MODEL, parse_json=False)
    state["cost_usd"] = state.get("cost_usd", 0.0) + resp.cost_usd
    state["tokens_used"] = state.get("tokens_used", 0) + resp.tokens_used
    blueprint_content = resp.text.strip()

    # ── 2. Create the Google Doc ──────────────────────────────────────────────
    doc_title = f"Blueprint — {vision_text[:60]}"
    blueprint_id = task_id  # use task_id as stable blueprint identifier
    doc_id = ""
    doc_url = ""
    try:
        doc_id = create_document(
            title=doc_title,
            project_id=project_id,
            folder_id=settings.docs.blueprints_folder_id,
            initial_content=blueprint_content,
        )
        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   f"vision_blueprint: created doc {doc_id}")
    except Exception as exc:
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   f"vision_blueprint: doc creation failed: {exc}", "ERROR")

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
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   f"vision_blueprint: Sheet append failed: {exc}", "WARNING")

    # ── 5. Send approval card to owner's Chat space ───────────────────────────
    owner_space = settings.chat.owner_space if settings.chat.owner_space else space_name
    if owner_space:
        try:
            send_approval_card(
                space_name=owner_space,
                proposal_id=blueprint_id,
                agent_id="nexus-prime",
                issue_summary=f"New vision submitted by {submitted_by}: {vision_text[:120]}",
                proposed_action=f"Blueprint Doc created. Review and approve to proceed.",
                priority=3,
                cost_usd=state.get("cost_usd", 0.0),
                doc_url=doc_url,
            )
        except Exception as exc:
            _log_cloud("nexus-prime", project_id, "task", task_id,
                       f"vision_blueprint: Chat card failed: {exc}", "WARNING")

    _log_cloud("nexus-prime", project_id, "task", task_id,
               f"vision_blueprint complete: blueprint_id={blueprint_id} doc_id={doc_id}")
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
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   f"_run_compaction: BQ archive failed: {exc}", "WARNING")

    _log_cloud("nexus-prime", project_id, "task", task_id,
               f"_run_compaction: compacted {len(constraints)} constraints for blueprint {blueprint_id}")
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
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   "iterate_plan: empty constraint_text — skipping", "WARNING")
        return state

    constraints: list[dict] = list(state.get("blueprint_constraints") or [])  # type: ignore[assignment]
    constraints.append({
        "blueprint_id": blueprint_id,
        "text": constraint_text,
        "comment_author": comment_author,
        "comment_timestamp": comment_timestamp,
    })

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
            _log_cloud("nexus-prime", project_id, "task", task_id,
                       f"iterate_plan: append_content failed for doc {doc_id}: {exc}", "WARNING")

    _log_cloud("nexus-prime", project_id, "task", task_id,
               f"iterate_plan complete: blueprint_id={blueprint_id} "
               f"total_constraints={len(constraints)}")
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
    from config import get_settings
    from tools.google_docs import list_comments
    from tools.pubsub import publish

    get_settings()  # validate config is loadable
    task_id = str(uuid.uuid4())
    published = 0
    errors = 0

    # Retrieve active blueprints from the Project_Incubator tab
    incubator_rows: list[dict] = []
    try:
        from tools.google_sheets import get_all_records
        incubator_rows = get_all_records("Project_Incubator", project_id)
    except Exception as exc:
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   f"poll_comments: failed to read Project_Incubator: {exc}", "WARNING")

    active_docs = [
        (row.get("id", ""), row.get("doc_id", ""))
        for row in incubator_rows
        if row.get("doc_id") and row.get("status", "") not in ("Archived", "Rejected")
    ]

    for blueprint_id, doc_id in active_docs:
        try:
            comments = list_comments(doc_id=doc_id, project_id=project_id)
        except Exception as exc:
            _log_cloud("nexus-prime", project_id, "task", task_id,
                       f"poll_comments: list_comments failed for doc {doc_id}: {exc}", "WARNING")
            errors += 1
            continue

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
                publish(
                    "agent.nexus-prime.events",
                    msg,
                    project_id,
                )
                published += 1
            except Exception as exc:
                _log_cloud("nexus-prime", project_id, "task", task_id,
                           f"poll_comments: publish failed for comment {comment.get('id')}: {exc}",
                           "WARNING")
                errors += 1

    _log_cloud("nexus-prime", project_id, "task", task_id,
               f"poll_comments complete: {published} published, {errors} errors, "
               f"{len(active_docs)} docs polled")

    return {
        "docs_polled": len(active_docs),
        "comments_published": published,
        "errors": errors,
        "task_id": task_id,
    }


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
                _log_cloud("nexus-prime", project_id, "task", task_id,
                           f"handle_skill_request: update_row failed: {exc}", "WARNING")

            # Remove from parked list
            state["parked_proposals"] = [
                p for p in state.get("parked_proposals", []) if p != proposal_id
            ]

        # Determine requesting agent topic
        source_agent = msg.source_agent  # set to "google-chat" for card-click path
        # The actual requesting agent is stored in the payload when possible
        requesting_agent = payload.get("agent_id", source_agent)
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
                publish(topic, reply, project_id)
            except Exception as exc:
                _log_cloud("nexus-prime", project_id, "task", task_id,
                           f"handle_skill_request: publish Approved failed: {exc}", "WARNING")
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
                publish(topic, alert, project_id)
            except Exception as exc:
                _log_cloud("nexus-prime", project_id, "task", task_id,
                           f"handle_skill_request: publish Rejected failed: {exc}", "WARNING")

        _log_cloud("nexus-prime", project_id, "task", task_id,
                   f"handle_skill_request: resolution {status} for proposal={proposal_id} "
                   f"package={package_name} agent={requesting_agent}")
        return state

    # ── Inbound request path ──────────────────────────────────────────────────
    package_name: str = payload.get("package_name", "")
    agent_id: str = payload.get("agent_id", msg.source_agent)
    reason: str = payload.get("reason", "")
    pypi_url: str = payload.get("pypi_url", "")

    if not package_name:
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   "handle_skill_request: missing package_name — skipping", "WARNING")
        return state

    proposal_id = str(uuid.uuid4())

    # Write audit row to Agent_Approvals
    row = {
        "ID": proposal_id,
        "Agent ID": agent_id,
        "Issue": f"Skill import request: {package_name}",
        "Trigger Reason": reason[:500] if reason else "ModuleNotFoundError in Write-Test-Refine loop",
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
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   f"handle_skill_request: Sheet append failed: {exc}", "WARNING")

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
            _log_cloud("nexus-prime", project_id, "task", task_id,
                       f"handle_skill_request: Chat card failed: {exc}", "WARNING")
    else:
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   "handle_skill_request: chat.owner_space not configured — card not sent", "WARNING")

    _log_cloud("nexus-prime", project_id, "task", task_id,
               f"handle_skill_request: inbound request parked proposal_id={proposal_id} "
               f"package={package_name} agent={agent_id}")
    return state


# ── Graph assembly ────────────────────────────────────────────────────────────

def build_nexus_prime_graph() -> Any:
    """
    Assemble and compile the Nexus-Prime StateGraph per
    GAOS-Nexus-Prime-Spec.md §3.3.
    """
    graph: StateGraph = StateGraph(NexusPrimeWorkingMemory)

    graph.add_node("boot",              boot)
    graph.add_node("monitor",           monitor)
    graph.add_node("route",             route)
    graph.add_node("diagnose",          diagnose)
    graph.add_node("propose_gate",      propose_gate)
    graph.add_node("knowledge_review",  knowledge_review)
    graph.add_node("promote",           promote)
    graph.add_node("init_project",      init_project)
    graph.add_node("notify_agents",     notify_agents)
    graph.add_node("conflict_resolve",  conflict_resolve)
    graph.add_node("park_or_broadcast", park_or_broadcast)
    graph.add_node("record",            record)
    graph.add_node("vision_blueprint",  vision_blueprint)
    graph.add_node("iterate_plan",      iterate_plan)
    graph.add_node("handle_skill_request", handle_skill_request)

    graph.set_entry_point("boot")
    graph.add_edge("boot",    "monitor")
    graph.add_edge("monitor", "route")

    graph.add_conditional_edges(
        "route",
        route,
        {
            "diagnose":              "diagnose",
            "knowledge_review":      "knowledge_review",
            "init_project":          "init_project",
            "conflict_resolve":      "conflict_resolve",
            "promote":               "promote",
            "park_or_broadcast":     "park_or_broadcast",
            "record":                "record",
            "vision_blueprint":      "vision_blueprint",
            "iterate_plan":          "iterate_plan",
            "handle_skill_request":  "handle_skill_request",
        },
    )

    graph.add_edge("diagnose",          "propose_gate")
    graph.add_edge("propose_gate",      "record")
    graph.add_edge("knowledge_review",  "record")
    graph.add_edge("promote",           "record")
    graph.add_edge("init_project",      "notify_agents")
    graph.add_edge("notify_agents",     "record")
    graph.add_edge("conflict_resolve",  "record")
    graph.add_edge("park_or_broadcast", "record")
    graph.add_edge("vision_blueprint",     "record")
    graph.add_edge("iterate_plan",         "record")
    graph.add_edge("handle_skill_request", "record")
    graph.add_edge("record",               END)

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

            from models import AgentOutput
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


# Module-level compiled graph (used directly in Cloud Run handler if not via ADK)
nexus_prime_graph = build_nexus_prime_graph()
