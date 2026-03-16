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

    graph.set_entry_point("boot")
    graph.add_edge("boot",    "monitor")
    graph.add_edge("monitor", "route")

    graph.add_conditional_edges(
        "route",
        route,
        {
            "diagnose":          "diagnose",
            "knowledge_review":  "knowledge_review",
            "init_project":      "init_project",
            "conflict_resolve":  "conflict_resolve",
            "promote":           "promote",
            "park_or_broadcast": "park_or_broadcast",
            "record":            "record",
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
    graph.add_edge("record",            END)

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
