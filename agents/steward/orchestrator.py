from __future__ import annotations

import hashlib
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from agents import (
    _call_model,
    _load_identity_file,
    _log_cloud,
    _run_evolution_loop,
    _write_heartbeat,
    utcnow_iso,
)
from models import (
    A2AMessage,
    AgentInput,
    AgentOutput,
    AgentWorkingMemory,
    ApprovalProposal,
    MessageType,
)

# ── Domain constants ──────────────────────────────────────────────────────────

_AGENT_ID = "steward"
_SHEET_TAB = "Logs"
_OUTBOUND_TOPIC = "agent.steward.events"
_INBOUND_TOPICS = ["agent.nexus-prime.events", "agent.approvals.events"]

# Compliance tasks due within this window are flagged urgent
_COMPLIANCE_LEAD_DAYS = 14
# Onboarding tasks overdue beyond this are flagged for escalation
_ONBOARDING_OVERDUE_DAYS = 5


def _local() -> str:
    from config import get_settings

    return get_settings().models.LOCAL_MODEL


def _fast() -> str:
    from config import get_settings

    return get_settings().models.FAST_MODEL


# ── Boot ──────────────────────────────────────────────────────────────────────


def _boot(state: AgentWorkingMemory) -> AgentWorkingMemory:
    import sys

    from config import get_settings
    from tools.google_sheets import init_sheets_client
    from tools.memory import load_domain_memory, query_episodic
    from tools.project_registry import load_project_registry
    from tools.pubsub import ensure_topic_exists
    from tools.secrets import SecretAccessDenied, SecretNotFoundError, get_secret

    settings = get_settings()
    pid = state.get("project_id") or settings.GCP_PROJECT_ID
    state["project_id"] = pid
    state.setdefault("_started_at", time.time())
    for k, v in [
        ("cost_usd", 0.0),
        ("step_count", 0),
        ("tokens_used", 0),
        ("hard_stop_triggered", False),
        ("evolution_triggered", False),
        ("messages", []),
        ("sub_task_results", []),
        ("parked_proposals", []),
        ("error_history", []),
        ("observation_buffer", []),
        ("memory_context", {}),
        ("episodic_cache", {}),
        ("iteration_count", 0),
        ("current_objective", "Booting"),
    ]:
        state.setdefault(k, v)

    # Step 2: secrets — fail fast
    try:
        get_secret("GEMINI_API_KEY", pid)
    except (SecretNotFoundError, SecretAccessDenied) as exc:
        _log_cloud(_AGENT_ID, pid, "security", "boot", f"STARTUP_FAILURE: {exc}", "CRITICAL")
        sys.exit(1)

    # Step 2.5: init Sheets client before registry read (project_registry uses get_all_records)
    try:
        init_sheets_client(pid)
    except Exception as _sheets_exc:
        _log_cloud(
            _AGENT_ID,
            pid,
            "task",
            "boot",
            f"boot: init_sheets_client failed — {_sheets_exc}",
            "ERROR",
        )

    # Step 3: Project Registry validation — registry read failure is fatal
    try:
        active_ids = {p.project_id for p in load_project_registry(pid)}
    except Exception as exc:
        _log_cloud(
            _AGENT_ID,
            pid,
            "security",
            "boot",
            f"STARTUP_FAILURE: registry read failed: {exc}",
            "CRITICAL",
        )
        sys.exit(1)
    if pid not in active_ids and pid != settings.GCP_PROJECT_ID:
        _log_cloud(
            _AGENT_ID,
            pid,
            "security",
            "boot",
            f"STARTUP_FAILURE: unknown project_id '{pid}'",
            "CRITICAL",
        )
        sys.exit(1)

    # Step 4 / Step 5: Pub/Sub — ensure outbound and all inbound topics exist
    for _topic in [_OUTBOUND_TOPIC, *_INBOUND_TOPICS]:
        try:
            ensure_topic_exists(_topic)
        except Exception as exc:
            _log_cloud(
                _AGENT_ID,
                pid,
                "task",
                "boot",
                f"ensure_topic_exists({_topic!r}) failed: {exc}",
                "WARNING",
            )
    try:
        state["memory_context"] = load_domain_memory(agent_id=_AGENT_ID, project_id=pid)
    except Exception as exc:
        _log_cloud(_AGENT_ID, pid, "task", "boot", f"load_domain_memory failed: {exc}", "WARNING")
    try:
        state["episodic_cache"] = {"recent": query_episodic(_AGENT_ID, pid, "admin", limit=5)}
    except Exception as exc:
        _log_cloud(_AGENT_ID, pid, "task", "boot", f"query_episodic failed: {exc}", "WARNING")

    _write_heartbeat(_AGENT_ID, pid, "IDLE", "Boot complete", 0, "", _SHEET_TAB)
    return state


# ── Plan ──────────────────────────────────────────────────────────────────────


def _plan(state: AgentWorkingMemory) -> AgentWorkingMemory:
    from tools.google_sheets import get_all_records

    state["step_count"] = state.get("step_count", 0) + 1
    pid = state["project_id"]
    pending_items: list[dict] = []
    now = datetime.now(tz=UTC)
    deadline_cutoff = now + timedelta(days=_COMPLIANCE_LEAD_DAYS)
    overdue_cutoff = now - timedelta(days=_ONBOARDING_OVERDUE_DAYS)

    msg = state.get("incoming_message")
    if msg and msg.message_type == MessageType.EVOLUTION_REQUEST:
        state["evolution_triggered"] = True
        state["current_objective"] = (
            f"Evolution: {(msg.payload or {}).get('description', 'capability gap')[:80]}"
        )
        state["sub_task_results"] = []
        state["messages"].append(
            {"role": "system", "content": "EVOLUTION_REQUEST received — skipping normal planning"}
        )
        return state

    try:
        rows = get_all_records(_SHEET_TAB, pid)
        for r in rows:
            row_type = str(r.get("type", "")).lower()
            due_raw = r.get("due_date") or r.get("deadline")
            created_raw = r.get("created_at") or r.get("date")

            if row_type == "compliance" and due_raw:
                try:
                    due = datetime.fromisoformat(str(due_raw)).replace(tzinfo=UTC)
                    if now < due <= deadline_cutoff:
                        pending_items.append({**r, "urgency": "compliance_deadline"})
                except ValueError:
                    pass

            elif (
                row_type == "onboarding"
                and r.get("status") in ("Pending", "In Progress")
                and created_raw
            ):
                try:
                    created = datetime.fromisoformat(str(created_raw)).replace(tzinfo=UTC)
                    if created <= overdue_cutoff:
                        pending_items.append({**r, "urgency": "onboarding_overdue"})
                except ValueError:
                    pass
        pending_items = pending_items[:10]
    except Exception as exc:
        _log_cloud(
            _AGENT_ID,
            pid,
            "task",
            state.get("task_id", ""),
            f"_plan: sheet read failed: {exc}",
            "WARNING",
        )

    msg = state.get("incoming_message")
    if msg and msg.message_type in (MessageType.TASK_HANDOFF, MessageType.BROADCAST):
        pending_items.append(msg.payload or {})

    summary = [
        {
            k: v
            for k, v in r.items()
            if k in ("type", "urgency", "subject", "due_date", "assignee", "status", "priority")
        }
        for r in pending_items[:5]
    ]
    prompt = (
        f"Admin/HR plan. Urgent items: {summary}\n"
        f"Memory keys: {list(state.get('memory_context', {}).keys())[:5]}\n"
        "Known task_type values: coordinate_calendar, send_reminder, "
        "drive_maintenance (scan Drive Inbound/ and classify unorganised files), other.\n"
        'Top 3 tasks as JSON: [{"task_type": str, "item": dict, "needs_calendar": bool}]'
    )
    resp = _call_model(prompt, model=_fast(), parse_json=True)
    state["cost_usd"] = state.get("cost_usd", 0.0) + resp.cost_usd
    tasks = resp.data if isinstance(resp.data, list) else []
    state["current_objective"] = f"Processing {len(tasks)} admin task(s)"
    state["sub_task_results"] = []
    state["messages"].append({"role": "assistant", "content": resp.text, "tasks": tasks})
    return state


# ── Dispatch / Collect / Report / Park / Resume / Escalate ────────────────────


def _dispatch(state: AgentWorkingMemory) -> AgentWorkingMemory:
    planned = state["messages"][-1].get("tasks", []) if state.get("messages") else []
    for task in planned[:5]:
        task_type = task.get("task_type", "unknown")
        result: dict[str, Any] = {"task_type": task_type, "status": "skipped", "output": {}}

        # Any task touching Google Calendar must go through the Approval Gate (Priority-2)
        if task.get("needs_calendar"):
            result["status"] = "pending_approval"
            result["output"] = {"reason": "calendar_requires_approval", "task": task}
            state["sub_task_results"].append(result)
            continue

        try:
            import importlib

            mod = importlib.import_module(f"agents.steward.tasks.{task_type}")
            from models import AgentInput

            out = mod.run(
                AgentInput(
                    task_id=str(uuid.uuid4()),
                    project_id=state["project_id"],
                    instruction=f"Process: {task_type}",
                    context=task.get("item", {}),
                )
            )
            result = {
                "task_type": task_type,
                "status": out.status,
                "output": out.result,
                "cost_usd": out.cost_usd,
            }
            state["cost_usd"] = state.get("cost_usd", 0.0) + out.cost_usd
        except (ModuleNotFoundError, AttributeError):
            result["status"] = "skipped"
        except Exception as exc:
            result["status"] = "escalated"
            result["output"] = {"error": str(exc)}
            state["error_history"].append(str(exc)[:200])
        state["sub_task_results"].append(result)
    return state


def _collect(state: AgentWorkingMemory) -> AgentWorkingMemory:
    results = state.get("sub_task_results", [])
    # Calendar approval requests (Priority-2)
    pending_approval = [r for r in results if r.get("status") == "pending_approval"]
    # Drive move proposals from Archivist (Priority-3)
    drive_approval = [
        r
        for r in results
        if r.get("task_type") == "drive_maintenance"
        and isinstance(r.get("output"), dict)
        and r["output"].get("requires_approval")
    ]
    escalated = [r for r in results if r.get("status") == "escalated"]
    needs_park = bool(pending_approval) or bool(drive_approval)
    approval_count = len(pending_approval) + len(drive_approval)
    state["messages"].append(
        {
            "role": "system",
            "content": (
                f"Cycle: {len(results)} tasks, {len(escalated)} escalated, "
                f"{approval_count} pending approval."
            ),
            "escalated": escalated,
            "needs_park": needs_park,
        }
    )
    return state


def _should_escalate(state: AgentWorkingMemory) -> str:
    if state.get("evolution_triggered"):
        return "evolve"
    last = state.get("messages", [{}])[-1]
    if last.get("escalated"):
        return "escalate"
    if last.get("needs_park"):
        return "park"
    return "report"


def _report(state: AgentWorkingMemory) -> AgentWorkingMemory:
    from tools.google_sheets import batch_append_rows
    from tools.pubsub import publish

    pid = state["project_id"]
    results = state.get("sub_task_results", [])
    rows_to_write = [
        {
            "timestamp": utcnow_iso(),
            "agent_id": _AGENT_ID,
            "type": "admin",
            "task_type": r.get("task_type", ""),
            "status": r.get("status", ""),
            "summary": str(r.get("output", ""))[:500],
        }
        for r in results
        if r.get("status") == "success"
    ]
    if rows_to_write:
        try:
            batch_append_rows(_SHEET_TAB, rows_to_write, pid)
        except Exception as exc:
            _log_cloud(
                _AGENT_ID,
                pid,
                "task",
                state.get("task_id", ""),
                f"_report: sheet write failed: {exc}",
                "WARNING",
            )
    try:
        publish(
            _OUTBOUND_TOPIC,
            A2AMessage(
                source_agent=_AGENT_ID,
                target_agent="nexus-prime",
                project_id=pid,
                task_id=state.get("task_id", str(uuid.uuid4())),
                message_type=MessageType.STATUS_UPDATE,
                priority=1,
                payload={"status": "WORKING", "cost_usd": state.get("cost_usd", 0.0)},
            ),
        )
    except Exception as exc:
        _log_cloud(
            _AGENT_ID,
            pid,
            "task",
            state.get("task_id", ""),
            f"_report: STATUS_UPDATE publish failed: {exc}",
            "WARNING",
        )
    _write_heartbeat(
        _AGENT_ID,
        pid,
        "IDLE",
        state.get("current_objective", "Cycle complete"),
        len(state.get("parked_proposals", [])),
        "",
        _SHEET_TAB,
    )
    return state


def _park(state: AgentWorkingMemory) -> AgentWorkingMemory:
    """
    Write to Agent_Approvals and publish APPROVAL_REQUEST.

    Handles two proposal types:
    - Priority-2: calendar API interactions (needs_calendar tasks)
    - Priority-3: Drive file move proposals from drive_maintenance tasks

    (GAOS-Agent-Spec.md §3.5)
    """
    from tools.google_sheets import append_row
    from tools.pubsub import publish

    pid = state["project_id"]
    results = state.get("sub_task_results", [])
    task_id = state.get("task_id", "")

    # ── Priority-2: calendar tasks ────────────────────────────────────────
    calendar_tasks = [r for r in results if r.get("status") == "pending_approval"]
    if calendar_tasks:
        proposed_content = str(calendar_tasks)[:4000]
        sha256 = hashlib.sha256(proposed_content.encode()).hexdigest()
        proposal = ApprovalProposal(
            agent_id=_AGENT_ID,
            issue="Calendar API interaction requires human approval",
            trigger_reason=f"{len(calendar_tasks)} calendar task(s) pending approval",
            proposed_code=proposed_content,
            total_cost_usd=state.get("cost_usd", 0.0),
            code_sha256=sha256,
        )
        try:
            append_row("Agent_Approvals", proposal.to_sheet_row(), pid)
        except Exception as exc:
            _log_cloud(
                _AGENT_ID,
                pid,
                "task",
                task_id,
                f"_park: Agent_Approvals write failed (calendar): {exc}",
                "ERROR",
            )
        state["parked_proposals"].append(proposal.id)
        try:
            publish(
                "agent/approvals/events",
                A2AMessage(
                    source_agent=_AGENT_ID,
                    target_agent="nexus-prime",
                    project_id=pid,
                    task_id=state.get("task_id", proposal.id),
                    message_type=MessageType.APPROVAL_REQUEST,
                    priority=2,
                    payload={
                        "proposal_id": proposal.id,
                        "code_sha256": sha256,
                        "description": "Calendar API interaction requires approval.",
                    },
                ),
            )
        except Exception as exc:
            _log_cloud(
                _AGENT_ID,
                pid,
                "task",
                task_id,
                f"_park: APPROVAL_REQUEST publish failed (calendar): {exc}",
                "ERROR",
            )

    # ── Priority-3: Drive file move proposals (Archivist output) ─────────
    drive_tasks = [
        r
        for r in results
        if r.get("task_type") == "drive_maintenance"
        and isinstance(r.get("output"), dict)
        and r["output"].get("requires_approval")
    ]
    if drive_tasks:
        move_count = sum(
            len((r.get("output") or {}).get("approved_moves", [])) for r in drive_tasks
        )
        proposed_content = str(drive_tasks)[:4000]
        sha256 = hashlib.sha256(proposed_content.encode()).hexdigest()
        drive_proposal = ApprovalProposal(
            agent_id=_AGENT_ID,
            issue="Drive file migration requires human approval",
            trigger_reason=f"{move_count} file move(s) proposed by Archivist",
            proposed_code=proposed_content,
            total_cost_usd=state.get("cost_usd", 0.0),
            code_sha256=sha256,
        )
        try:
            append_row("Agent_Approvals", drive_proposal.to_sheet_row(), pid)
        except Exception as exc:
            _log_cloud(
                _AGENT_ID,
                pid,
                "task",
                task_id,
                f"_park: Agent_Approvals write failed (drive): {exc}",
                "ERROR",
            )
        state["parked_proposals"].append(drive_proposal.id)
        # Cache the move proposals keyed by proposal_id so _resume can execute
        # them after the owner approves (Rule 2: project_id preserved through all calls)
        state.setdefault("_drive_move_cache", {})[drive_proposal.id] = drive_tasks  # type: ignore[typeddict-item]
        try:
            publish(
                "agent/approvals/events",
                A2AMessage(
                    source_agent=_AGENT_ID,
                    target_agent="nexus-prime",
                    project_id=pid,
                    task_id=task_id or drive_proposal.id,
                    message_type=MessageType.APPROVAL_REQUEST,
                    priority=3,
                    payload={
                        "proposal_id": drive_proposal.id,
                        "code_sha256": sha256,
                        "description": f"Archivist proposed {move_count} file move(s). Review before execution.",
                    },
                ),
            )
        except Exception as exc:
            _log_cloud(
                _AGENT_ID,
                pid,
                "task",
                task_id,
                f"_park: APPROVAL_REQUEST publish failed (drive): {exc}",
                "ERROR",
            )

    _write_heartbeat(
        _AGENT_ID,
        pid,
        "PARKED",
        f"{len(state['parked_proposals'])} proposal(s) pending approval",
        len(state["parked_proposals"]),
        "",
        _SHEET_TAB,
    )
    return state


def _execute_drive_moves(
    state: AgentWorkingMemory,
    proposal_id: str,
) -> tuple[int, int]:
    """Execute file moves for an approved Archivist proposal.

    Reads the cached drive_tasks for proposal_id from state, iterates
    approved_moves, and calls tools.drive.move_file() for each one.
    Failed moves are logged individually and counted — a partial failure
    does not abort the remaining moves.

    Args:
        state:       Steward working memory containing _drive_move_cache.
        proposal_id: The ApprovalProposal.id that was just approved.

    Returns:
        (succeeded, failed) counts.
    """
    from tools.drive import (
        DrivePermissionError,
        DriveWriteError,
        KnowledgeFileNotFoundError,
        move_file,
    )

    pid = state["project_id"]
    task_id = state.get("task_id", proposal_id)
    cache: dict = state.get("_drive_move_cache", {})  # type: ignore[typeddict-item]
    drive_tasks: list[dict] = cache.get(proposal_id, [])

    succeeded = 0
    failed = 0

    for task_result in drive_tasks:
        output = task_result.get("output") or {}
        approved_moves: list[dict] = output.get("approved_moves", [])

        for move in approved_moves:
            source_path: str = str(move.get("source_path", "")).strip()
            destination_path: str = str(move.get("destination_path", "")).strip()

            if not source_path or not destination_path:
                _log_cloud(
                    _AGENT_ID,
                    pid,
                    "task",
                    task_id,
                    "_execute_drive_moves: skipping move with empty source or destination",
                    "WARNING",
                )
                failed += 1
                continue

            # Split destination_path into folder + filename
            dest_parts = destination_path.rstrip("/").rsplit("/", 1)
            dest_folder = dest_parts[0] if len(dest_parts) == 2 else ""
            new_name = dest_parts[-1]

            try:
                move_file(source_path, dest_folder, pid, new_name=new_name)
                _log_cloud(
                    _AGENT_ID,
                    pid,
                    "task",
                    task_id,
                    f"_execute_drive_moves: '{source_path}' → '{destination_path}'",
                    "INFO",
                )
                succeeded += 1
            except (KnowledgeFileNotFoundError, DriveWriteError, DrivePermissionError) as exc:
                _log_cloud(
                    _AGENT_ID,
                    pid,
                    "task",
                    task_id,
                    f"_execute_drive_moves: failed to move '{source_path}': {exc}",
                    "ERROR",
                )
                failed += 1

    return succeeded, failed


def _resume(state: AgentWorkingMemory) -> AgentWorkingMemory:
    msg = state.get("incoming_message")
    if msg and msg.message_type == MessageType.APPROVAL_RESULT:
        payload = msg.payload or {}
        proposal_id: str = payload.get("proposal_id", "")
        approval_status: str = payload.get("status", "")

        state["parked_proposals"] = [
            p for p in state.get("parked_proposals", []) if p != proposal_id
        ]

        # Execute approved Drive file moves
        cache: dict = state.get("_drive_move_cache", {})  # type: ignore[typeddict-item]
        if approval_status == "Approved" and proposal_id in cache:
            pid = state["project_id"]
            task_id = state.get("task_id", proposal_id)
            succeeded, failed = _execute_drive_moves(state, proposal_id)
            _log_cloud(
                _AGENT_ID,
                pid,
                "task",
                task_id,
                f"_resume: drive moves complete — {succeeded} succeeded, {failed} failed",
                "INFO",
            )

    return state


def _escalate(state: AgentWorkingMemory) -> AgentWorkingMemory:
    from tools.pubsub import publish

    pid = state["project_id"]
    last_error = (state.get("error_history") or ["Unknown error"])[-1]
    try:
        publish(
            _OUTBOUND_TOPIC,
            A2AMessage(
                source_agent=_AGENT_ID,
                target_agent="nexus-prime",
                project_id=pid,
                task_id=state.get("task_id", str(uuid.uuid4())),
                message_type=MessageType.ESCALATION,
                priority=3,
                payload={"description": last_error, "error_fingerprint": last_error[:64]},
            ),
        )
    except Exception as exc:
        _log_cloud(
            _AGENT_ID,
            pid,
            "task",
            state.get("task_id", ""),
            f"_escalate: ESCALATION publish failed: {exc}",
            "WARNING",
        )
    _write_heartbeat(
        _AGENT_ID,
        pid,
        "ESCALATED",
        last_error[:100],
        len(state.get("parked_proposals", [])),
        last_error[:200],
        _SHEET_TAB,
    )
    return state


# ── Graph assembly ────────────────────────────────────────────────────────────


def _should_park_after_write(state: AgentWorkingMemory) -> str:
    """Route to park if any sub-task result flagged requires_approval; otherwise END."""
    needs_approval = any(
        isinstance(r.get("output"), dict) and r["output"].get("requires_approval")
        for r in state.get("sub_task_results", [])
    )
    return "park" if needs_approval else END


def _evolve(state: AgentWorkingMemory) -> AgentWorkingMemory:
    """
    Write-Test-Refine loop (GAOS-Manager-Spec.md §13).
    Triggered when EVOLUTION_REQUEST message received in _plan.
    Runs the loop, logs EvolutionTaskOutcome, and submits a Priority-4 proposal.
    """
    from tools.google_sheets import append_row
    from tools.pubsub import publish

    pid = state["project_id"]
    msg = state.get("incoming_message")
    payload = (msg.payload or {}) if msg else {}
    issue = payload.get("description", "Capability gap: no description provided")
    context_str = payload.get("context", state.get("current_objective", ""))

    evo = _run_evolution_loop(issue=issue, agent_id=_AGENT_ID, context=context_str)
    state["cost_usd"] = state.get("cost_usd", 0.0) + evo["cost_usd"]
    state["iteration_count"] = state.get("iteration_count", 0) + evo["iterations"]

    _log_cloud(
        _AGENT_ID,
        pid,
        "evolution_task",
        state.get("task_id", ""),
        (
            f"EvolutionTaskOutcome agent={_AGENT_ID} "
            f"iterations={evo['iterations']} "
            f"stopping_constraint={evo['stopping_constraint']}"
        ),
        "INFO",
    )

    if evo["stopping_constraint"] != "success":
        return state  # Loop did not converge — no proposal submitted

    proposed_code = evo["code"]
    sha256 = hashlib.sha256(proposed_code.encode()).hexdigest()
    proposal = ApprovalProposal(
        agent_id=_AGENT_ID,
        issue=issue,
        trigger_reason="EVOLUTION_REQUEST",
        proposed_code=proposed_code,
        iterations_run=evo["iterations"],
        total_cost_usd=evo["cost_usd"],
        code_sha256=sha256,
    )
    try:
        append_row("Agent_Approvals", proposal.to_sheet_row(), pid)
    except Exception as exc:
        _log_cloud(
            _AGENT_ID,
            pid,
            "task",
            state.get("task_id", ""),
            f"_evolve: Agent_Approvals write failed: {exc}",
            "ERROR",
        )

    state["parked_proposals"].append(proposal.id)
    try:
        publish(
            "agent/approvals/events",
            A2AMessage(
                source_agent=_AGENT_ID,
                target_agent="nexus-prime",
                project_id=pid,
                task_id=state.get("task_id", proposal.id),
                message_type=MessageType.APPROVAL_REQUEST,
                priority=4,
                payload={"proposal_id": proposal.id, "code_sha256": sha256},
            ),
        )
    except Exception as exc:
        _log_cloud(
            _AGENT_ID,
            pid,
            "task",
            state.get("task_id", ""),
            f"_evolve: APPROVAL_REQUEST publish failed: {exc}",
            "ERROR",
        )
    return state


def _write_playbook(state: AgentWorkingMemory) -> AgentWorkingMemory:
    """
    Write a playbook to Drive if this task was triggered by VISION_SUBMITTED.
    No-op for all other message types.
    Phase 2.5 — GAOS-Memory-Spec.md §7.4 + write_playbook completion requirement.
    """
    msg = state.get("incoming_message")
    if not (msg and msg.message_type == MessageType.VISION_SUBMITTED):
        return state

    from models import PlaybookDoc
    from tools.drive import write_playbook as _drive_write_playbook

    pid = state["project_id"]
    try:
        payload = msg.payload or {}
        doc = PlaybookDoc(
            title=payload.get("vision_title", "Untitled Playbook"),
            domain=_AGENT_ID,
            owner_agent=_AGENT_ID,
            project_id=pid,
            created_from_vision=msg.task_id or "",
        )
        results = state.get("sub_task_results", [])
        body = (
            f"## Objective\n{payload.get('vision_text', '')}\n\n"
            f"## Agents Involved\n- {_AGENT_ID}\n\n"
            "## Milestones\n"
            + "\n".join(
                f"- {r.get('task_type', '?')}: {r.get('status', '?')}" for r in results[:10]
            )
            + "\n\n## Constraints\n_None recorded._\n\n## Open Questions\n_None._\n"
        )
        _drive_write_playbook(doc, body, pid)
        state["messages"].append(
            {
                "role": "system",
                "content": f"Playbook written: {doc.title}",
            }
        )
    except Exception as exc:
        _log_cloud(
            _AGENT_ID,
            pid,
            "task",
            state.get("task_id", ""),
            f"write_playbook failed: {exc}",
            "WARNING",
        )
    return state


def build_steward_graph() -> Any:
    graph: StateGraph = StateGraph(AgentWorkingMemory)
    for name, fn in [
        ("boot", _boot),
        ("plan", _plan),
        ("dispatch", _dispatch),
        ("collect", _collect),
        ("report", _report),
        ("write_playbook", _write_playbook),
        ("park", _park),
        ("resume", _resume),
        ("escalate", _escalate),
        ("evolve", _evolve),
    ]:
        graph.add_node(name, fn)
    graph.set_entry_point("boot")
    graph.add_edge("boot", "plan")
    graph.add_edge("plan", "dispatch")
    graph.add_edge("dispatch", "collect")
    graph.add_conditional_edges(
        "collect",
        _should_escalate,
        {"escalate": "escalate", "report": "report", "park": "park", "evolve": "evolve"},
    )
    graph.add_edge("report", "write_playbook")
    graph.add_conditional_edges(
        "write_playbook", _should_park_after_write, {"park": "park", END: END}
    )
    graph.add_edge("park", END)
    graph.add_edge("resume", "plan")
    graph.add_edge("escalate", END)
    graph.add_edge("evolve", END)
    return graph.compile(checkpointer=MemorySaver())


# ── ADK Agent class ───────────────────────────────────────────────────────────

try:
    from google.adk.agents import Agent as _BaseAgent

    _HAS_ADK = True
except ImportError:
    _HAS_ADK = False


if _HAS_ADK:

    class StewardAgent(_BaseAgent):  # type: ignore[misc]
        name: str = _AGENT_ID
        description: str = "Admin & HR orchestrator — compliance, calendar, onboarding."
        model: str = ""
        instruction: str = ""
        tools: list = []

        def __init__(self, **data: Any) -> None:
            from config import get_settings

            data["model"] = get_settings().models.LOCAL_MODEL
            data["instruction"] = _load_identity_file(_AGENT_ID)
            super().__init__(**data)
            self._graph = build_steward_graph()

        async def run(self, agent_input: AgentInput) -> AgentOutput:
            initial = _initial_state(agent_input)
            try:
                final = await self._graph.ainvoke(
                    initial, config={"configurable": {"thread_id": initial["task_id"]}}
                )
                status = "failed" if final.get("hard_stop_triggered") else "success"
            except Exception as exc:
                _log_cloud(
                    _AGENT_ID, initial["project_id"], "task", initial["task_id"], str(exc), "ERROR"
                )
                status = "failed"
                final = initial
            return AgentOutput(
                task_id=final["task_id"],
                project_id=final["project_id"],
                agent_id=_AGENT_ID,
                status=status,
                result={
                    "tasks_processed": len(final.get("sub_task_results", [])),
                    "parked_proposals": len(final.get("parked_proposals", [])),
                    "objective": final.get("current_objective", ""),
                },
                cost_usd=final.get("cost_usd", 0.0),
            )

else:

    class StewardAgent:  # type: ignore[no-redef]
        name = _AGENT_ID

        def __init__(self, **_: Any) -> None:
            self._graph = build_steward_graph()

        async def run(self, agent_input: AgentInput) -> AgentOutput:
            initial = _initial_state(agent_input)
            try:
                final = await self._graph.ainvoke(
                    initial, config={"configurable": {"thread_id": initial["task_id"]}}
                )
                status = "failed" if final.get("hard_stop_triggered") else "success"
            except Exception as exc:
                _log_cloud(
                    self.name, initial["project_id"], "task", initial["task_id"], str(exc), "ERROR"
                )
                status = "failed"
                final = initial
            return AgentOutput(
                task_id=final["task_id"],
                project_id=final["project_id"],
                agent_id=self.name,
                status=status,
                result={
                    "tasks_processed": len(final.get("sub_task_results", [])),
                    "parked_proposals": len(final.get("parked_proposals", [])),
                    "objective": final.get("current_objective", ""),
                },
                cost_usd=final.get("cost_usd", 0.0),
            )


def _initial_state(agent_input: AgentInput) -> AgentWorkingMemory:
    pid = agent_input.project_id
    tid = agent_input.task_id
    return {  # type: ignore[return-value]
        "task_id": tid,
        "project_id": pid,
        "current_objective": "Starting",
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
        "_started_at": time.time(),
    }


steward_graph = build_steward_graph()
