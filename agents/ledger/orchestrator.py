"""
agents/ledger/orchestrator.py — Tier 2 Accounting Orchestrator

Ledger tracks all money in/out of the business: invoices, expenses,
reconciliation, and P&L summaries. It owns the `Accounting` Sheet tab
and proposes payments via the Approval Gate.

Construction spec: Docs/GAOS-Agent-Spec.md
Identity file:     Docs/agents/ledger.md
Master spec:       Docs/GAOS-Manager-Spec.md §1.2
"""

from __future__ import annotations

import hashlib
import time
import uuid
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

_AGENT_ID = "ledger"
_SHEET_TAB = "Accounting"
_OUTBOUND_TOPIC = "agent.ledger.events"
_INBOUND_TOPICS = [
    "agent.nexus-prime.events",
    "agent.pursuit.events",
    "agent.foreman.events",
    "agent.approvals.events",
]


# ── Helper: model aliases ─────────────────────────────────────────────────────


def _local() -> str:
    from config import get_settings

    return get_settings().models.LOCAL_MODEL


def _fast() -> str:
    from config import get_settings

    return get_settings().models.FAST_MODEL


def _deep() -> str:
    from config import get_settings

    return get_settings().models.DEEP_MODEL


# ── Graph nodes ───────────────────────────────────────────────────────────────


def _boot(state: AgentWorkingMemory) -> AgentWorkingMemory:
    """Step 1-7 of the agent boot sequence (GAOS-Agent-Spec.md §6)."""
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
    state.setdefault("cost_usd", 0.0)
    state.setdefault("step_count", 0)
    state.setdefault("tokens_used", 0)
    state.setdefault("hard_stop_triggered", False)
    state.setdefault("evolution_triggered", False)
    state.setdefault("messages", [])
    state.setdefault("sub_task_results", [])
    state.setdefault("parked_proposals", [])
    state.setdefault("error_history", [])
    state.setdefault("observation_buffer", [])
    state.setdefault("memory_context", {})
    state.setdefault("episodic_cache", {})
    state.setdefault("iteration_count", 0)
    state.setdefault("current_objective", "Booting")

    # Step 2: secrets — fail fast
    try:
        get_secret("GEMINI_API_KEY", pid)
    except (SecretNotFoundError, SecretAccessDenied) as exc:
        _log_cloud(
            _AGENT_ID,
            pid,
            "security",
            state.get("task_id", "boot"),
            f"STARTUP_FAILURE: {exc}",
            "CRITICAL",
        )
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
            state.get("task_id", "boot"),
            f"STARTUP_FAILURE: registry read failed: {exc}",
            "CRITICAL",
        )
        sys.exit(1)
    if pid not in active_ids and pid != settings.GCP_PROJECT_ID:
        _log_cloud(
            _AGENT_ID,
            pid,
            "security",
            state.get("task_id", "boot"),
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
        state["episodic_cache"] = {"recent": query_episodic(_AGENT_ID, pid, "accounting", limit=5)}
    except Exception as exc:
        _log_cloud(_AGENT_ID, pid, "task", "boot", f"query_episodic failed: {exc}", "WARNING")

    # Step 7: Write IDLE heartbeat
    _write_heartbeat(_AGENT_ID, pid, "IDLE", "Boot complete", 0, "", _SHEET_TAB)
    _log_cloud(_AGENT_ID, pid, "task", state.get("task_id", "boot"), "Boot complete")
    return state


def _plan(state: AgentWorkingMemory) -> AgentWorkingMemory:
    """
    Decide what to do next. Inspects the Accounting tab for pending work
    (overdue invoices, uncategorised expenses, month-end triggers) and
    formulates a work plan for this cycle.
    """
    from tools.google_sheets import get_all_records

    state["step_count"] = state.get("step_count", 0) + 1
    pid = state["project_id"]
    pending_items: list[dict] = []

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
        overdue = [r for r in rows if r.get("status") == "Overdue"]
        uncategorised = [r for r in rows if r.get("status") == "Uncategorised"]
        pending_items = overdue + uncategorised
    except Exception as exc:
        _log_cloud(
            _AGENT_ID,
            pid,
            "task",
            state.get("task_id", ""),
            f"_plan: sheet read failed: {exc}",
            "WARNING",
        )

    # Also honour incoming Pub/Sub message
    msg = state.get("incoming_message")
    if msg and msg.message_type == MessageType.TASK_HANDOFF:
        pending_items.append(msg.payload or {})
    elif msg and msg.message_type == MessageType.BROADCAST:
        # Broadcast from Nexus-Prime (e.g. PROJECT_INITIALIZED) — reload registry
        pending_items.append({"action": msg.payload.get("action", ""), "from": "broadcast"})

    prompt = (
        f"Accounting agent work plan.\n"
        f"Pending items ({len(pending_items)}): {pending_items[:10]}\n"
        f"Memory context keys: {list(state.get('memory_context', {}).keys())[:5]}\n"
        "List the top 3 tasks to dispatch this cycle as JSON array of "
        '{"task_type": str, "item": dict} objects.'
    )
    resp = _call_model(prompt, model=_local(), parse_json=True)
    state["cost_usd"] = state.get("cost_usd", 0.0) + resp.cost_usd

    tasks = resp.data if isinstance(resp.data, list) else []
    state["current_objective"] = f"Processing {len(tasks)} accounting task(s)"
    state["sub_task_results"] = []
    state["messages"].append({"role": "assistant", "content": resp.text, "tasks": tasks})
    return state


def _dispatch(state: AgentWorkingMemory) -> AgentWorkingMemory:
    """
    Attempt to call Tier 3 task agents for each planned task.
    Falls back to a direct in-process result if task modules are absent.
    """
    planned = state["messages"][-1].get("tasks", []) if state.get("messages") else []

    for task in planned[:5]:  # safety cap
        task_type = task.get("task_type", "unknown")
        item = task.get("item", {})
        result: dict[str, Any] = {"task_type": task_type, "status": "skipped", "output": {}}

        try:
            # Tasks live in agents/ledger/tasks/ — import dynamically
            mod_name = f"agents.ledger.tasks.{task_type}"
            import importlib

            mod = importlib.import_module(mod_name)
            from models import AgentInput

            inp = AgentInput(
                task_id=str(uuid.uuid4()),
                project_id=state["project_id"],
                instruction=f"Process: {task_type}",
                context=item,
            )
            out: AgentOutput = mod.run(inp)
            result = {
                "task_type": task_type,
                "status": out.status,
                "output": out.result,
                "cost_usd": out.cost_usd,
            }
            state["cost_usd"] = state.get("cost_usd", 0.0) + out.cost_usd
        except (ModuleNotFoundError, AttributeError):
            # Task agent not yet deployed — log and continue
            result["status"] = "skipped"
            result["output"] = {"reason": f"Task module '{task_type}' not found"}
        except Exception as exc:
            result["status"] = "escalated"
            result["output"] = {"error": str(exc)}
            state["error_history"].append(str(exc)[:200])

        state["sub_task_results"].append(result)

    return state


def _collect(state: AgentWorkingMemory) -> AgentWorkingMemory:
    """Aggregate sub-agent results and decide whether to escalate."""
    results = state.get("sub_task_results", [])
    escalated = [r for r in results if r.get("status") == "escalated"]

    # Calculate cycle cost for logging. State increment handled immediately in _dispatch.
    sub_task_cost = sum(r.get("cost_usd", 0.0) for r in results)

    state["messages"].append(
        {
            "role": "system",
            "content": f"Cycle results: {len(results)} tasks, {len(escalated)} escalated. Sub-task cost: ${sub_task_cost:.4f}",
            "escalated": escalated,
        }
    )
    return state


def _should_escalate(state: AgentWorkingMemory) -> str:
    if state.get("evolution_triggered"):
        return "evolve"
    last = state.get("messages", [{}])[-1]
    if last.get("escalated") or state.get("hard_stop_triggered"):
        return "escalate"
    return "report"


def _report(state: AgentWorkingMemory) -> AgentWorkingMemory:
    """
    Write completed results to the Accounting tab and publish status
    heartbeat to Nexus-Prime.
    """
    from tools.google_sheets import batch_append_rows
    from tools.pubsub import publish

    pid = state["project_id"]
    results = state.get("sub_task_results", [])
    rows_to_write = [
        {
            "timestamp": utcnow_iso(),
            "agent_id": _AGENT_ID,
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

    heartbeat = A2AMessage(
        source_agent=_AGENT_ID,
        target_agent="nexus-prime",
        project_id=pid,
        task_id=state.get("task_id", str(uuid.uuid4())),
        message_type=MessageType.STATUS_UPDATE,
        priority=1,
        payload={
            "status": "WORKING",
            "tasks_completed": len([r for r in results if r.get("status") == "success"]),
            "cost_usd": state.get("cost_usd", 0.0),
        },
    )

    try:
        publish(_OUTBOUND_TOPIC, heartbeat)
    except Exception as exc:
        _log_cloud(
            _AGENT_ID,
            pid,
            "task",
            state.get("task_id", "unknown"),
            f"publish to {_OUTBOUND_TOPIC} failed (heartbeat): {exc}",
            "ERROR",
        )
        raise

    open_proposals = len(state.get("parked_proposals", []))
    _write_heartbeat(
        _AGENT_ID,
        pid,
        "IDLE",
        state.get("current_objective", "Cycle complete"),
        open_proposals,
        "",
        _SHEET_TAB,
    )
    return state


def _park(state: AgentWorkingMemory) -> AgentWorkingMemory:
    """
    Submit a Priority-3 Approval Gate proposal for sub-task results requiring
    human sign-off, then park this task cycle. (GAOS-Agent-Spec.md §3.5)
    """
    from tools.google_sheets import append_row
    from tools.pubsub import publish

    pid = state["project_id"]
    results = state.get("sub_task_results", [])
    approval_results = [
        r
        for r in results
        if isinstance(r.get("output"), dict) and r["output"].get("requires_approval")
    ]
    proposed_content = str(approval_results)[:4000]
    sha256 = hashlib.sha256(proposed_content.encode()).hexdigest()
    proposal = ApprovalProposal(
        agent_id=_AGENT_ID,
        issue="Accounting change requires human approval",
        trigger_reason=f"{len(approval_results)} task(s) flagged requires_approval",
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
            state.get("task_id", ""),
            f"_park: Agent_Approvals write failed: {exc}",
            "ERROR",
        )
        return state

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
                priority=3,
                payload={
                    "proposal_id": proposal.id,
                    "code_sha256": sha256,
                    "cost_usd": state.get("cost_usd", 0.0),
                },
            ),
        )
    except Exception as exc:
        _log_cloud(
            _AGENT_ID,
            pid,
            "task",
            state.get("task_id", ""),
            f"_park: APPROVAL_REQUEST publish failed: {exc}",
            "ERROR",
        )
    _write_heartbeat(
        _AGENT_ID,
        pid,
        "PARKED",
        f"Parked proposal {proposal.id}",
        len(state["parked_proposals"]),
        "",
        _SHEET_TAB,
    )
    return state


def _resume(state: AgentWorkingMemory) -> AgentWorkingMemory:
    """Resume a parked task when an approval event arrives via Pub/Sub."""
    msg = state.get("incoming_message")
    if msg and msg.message_type == MessageType.APPROVAL_RESULT:
        proposal_id = (msg.payload or {}).get("proposal_id", "")
        approval_status = (msg.payload or {}).get("status", "")
        state["parked_proposals"] = [
            p for p in state.get("parked_proposals", []) if p != proposal_id
        ]
        state["messages"].append(
            {
                "role": "system",
                "content": f"Resumed: proposal={proposal_id} status={approval_status}",
            }
        )
    return state


def _escalate(state: AgentWorkingMemory) -> AgentWorkingMemory:
    """Publish ESCALATION to Nexus-Prime."""
    from tools.pubsub import publish

    pid = state["project_id"]
    last_error = (state.get("error_history") or ["Unknown error"])[-1]
    escalation = A2AMessage(
        source_agent=_AGENT_ID,
        target_agent="nexus-prime",
        project_id=pid,
        task_id=state.get("task_id", str(uuid.uuid4())),
        message_type=MessageType.ESCALATION,
        priority=3,
        payload={
            "description": last_error,
            "error_fingerprint": last_error[:64],
            "tasks_attempted": len(state.get("sub_task_results", [])),
            "cost_usd": state.get("cost_usd", 0.0),
        },
    )
    try:
        publish(_OUTBOUND_TOPIC, escalation)
    except Exception as exc:
        _log_cloud(
            _AGENT_ID,
            pid,
            "task",
            state.get("task_id", "unknown"),
            f"publish to {_OUTBOUND_TOPIC} failed (escalation): {exc}",
            "ERROR",
        )
        raise

    _write_heartbeat(
        _AGENT_ID,
        pid,
        "ESCALATED",
        f"Escalated: {last_error[:100]}",
        len(state.get("parked_proposals", [])),
        last_error[:200],
        _SHEET_TAB,
    )
    return state


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

    # Never deploy without Approval Gate — Priority-4 proposal (§3.6)
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
        return state

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
                payload={
                    "proposal_id": proposal.id,
                    "code_sha256": sha256,
                    "cost_usd": state.get("cost_usd", 0.0),
                },
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


# ── Graph assembly ────────────────────────────────────────────────────────────


def build_ledger_graph() -> Any:
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
        {"escalate": "escalate", "report": "report", "evolve": "evolve"},
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

    class LedgerAgent(_BaseAgent):  # type: ignore[misc]
        name: str = _AGENT_ID
        description: str = "Accounting orchestrator — invoices, expenses, reconciliation, P&L."
        model: str = ""
        instruction: str = ""
        tools: list = []

        def __init__(self, **data: Any) -> None:
            from config import get_settings

            data["model"] = get_settings().models.LOCAL_MODEL
            data["instruction"] = _load_identity_file(_AGENT_ID)
            super().__init__(**data)
            self._graph = build_ledger_graph()

        async def run(self, agent_input: AgentInput) -> AgentOutput:
            initial = _initial_state(agent_input)
            try:
                final = await self._graph.ainvoke(
                    initial,
                    config={"configurable": {"thread_id": initial["task_id"]}},
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

    class LedgerAgent:  # type: ignore[no-redef]
        name = _AGENT_ID

        def __init__(self, **_: Any) -> None:
            self._graph = build_ledger_graph()

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


ledger_graph = build_ledger_graph()
