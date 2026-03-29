"""
agents/foreman/orchestrator.py — Tier 2 Operations Orchestrator

Foreman manages order fulfillment, inventory levels, shipping coordination,
and vendor communications. It owns the Shipping and Receiving Sheet tab
and notifies Pursuit (fulfillment confirmed) and Ledger (fulfillment triggers AR).

Construction spec: Docs/GAOS-Agent-Spec.md
Identity file:     Docs/agents/foreman.md
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

_AGENT_ID = "foreman"
_SHEET_TAB = "Shipping and Receiving"
_OUTBOUND_TOPIC = "agent.foreman.events"
_INBOUND_TOPICS = [
    "agent.nexus-prime.events",
    "agent.pursuit.events",
    "agent.approvals.events",
]


def _local() -> str:
    from config import get_settings

    return get_settings().models.LOCAL_MODEL


# ── Boot ──────────────────────────────────────────────────────────────────────


def _boot(state: AgentWorkingMemory) -> AgentWorkingMemory:
    import sys

    from config import get_settings
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
        state["episodic_cache"] = {"recent": query_episodic(_AGENT_ID, pid, "operations", limit=5)}
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
        # SKUs at or below reorder threshold, or shipment exceptions
        pending_items = [
            r
            for r in rows
            if r.get("status") in ("Exception", "Below Threshold", "Pending Reorder")
            or int(r.get("qty_on_hand") or 0) <= int(r.get("reorder_threshold") or 0)
        ][:10]
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

    prompt = (
        f"Operations fulfillment plan. Pending items: {pending_items[:5]}\n"
        f"Memory keys: {list(state.get('memory_context', {}).keys())[:5]}\n"
        'Top 3 tasks JSON: [{"task_type": str, "item": dict}]'
    )
    resp = _call_model(prompt, model=_local(), parse_json=True)
    state["cost_usd"] = state.get("cost_usd", 0.0) + resp.cost_usd
    tasks = resp.data if isinstance(resp.data, list) else []
    state["current_objective"] = f"Processing {len(tasks)} operations task(s)"
    state["sub_task_results"] = []
    state["messages"].append({"role": "assistant", "content": resp.text, "tasks": tasks})
    return state


# ── Dispatch / Collect / Report / Park / Resume / Escalate ────────────────────


def _dispatch(state: AgentWorkingMemory) -> AgentWorkingMemory:
    planned = state["messages"][-1].get("tasks", []) if state.get("messages") else []
    for task in planned[:5]:
        task_type = task.get("task_type", "unknown")
        result: dict[str, Any] = {"task_type": task_type, "status": "skipped", "output": {}}
        try:
            import importlib

            mod = importlib.import_module(f"agents.foreman.tasks.{task_type}")
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

    # Publish stockout events so Pursuit can suspend quoting
    for r in state["sub_task_results"]:
        if r.get("status") == "stockout":
            try:
                from tools.pubsub import publish

                publish(
                    _OUTBOUND_TOPIC,
                    A2AMessage(
                        source_agent=_AGENT_ID,
                        target_agent="broadcast",
                        project_id=state["project_id"],
                        task_id=state.get("task_id", str(uuid.uuid4())),
                        message_type=MessageType.ALERT,
                        priority=4,
                        payload={
                            "alert_type": "stock_insufficient",
                            "sku": r.get("output", {}).get("sku", ""),
                        },
                    ),
                )
            except Exception:
                pass
    return state


def _collect(state: AgentWorkingMemory) -> AgentWorkingMemory:
    results = state.get("sub_task_results", [])
    escalated = [r for r in results if r.get("status") == "escalated"]
    state["messages"].append(
        {
            "role": "system",
            "content": f"Cycle: {len(results)} tasks, {len(escalated)} escalated.",
            "escalated": escalated,
        }
    )
    return state


def _should_escalate(state: AgentWorkingMemory) -> str:
    if state.get("evolution_triggered"):
        return "evolve"
    return "escalate" if state.get("messages", [{}])[-1].get("escalated") else "report"


def _report(state: AgentWorkingMemory) -> AgentWorkingMemory:
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
        issue="Operations change requires human approval",
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
                payload={"proposal_id": proposal.id, "code_sha256": sha256},
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
    msg = state.get("incoming_message")
    if msg and msg.message_type == MessageType.APPROVAL_RESULT:
        pid = (msg.payload or {}).get("proposal_id", "")
        state["parked_proposals"] = [p for p in state.get("parked_proposals", []) if p != pid]
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


def build_foreman_graph() -> Any:
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

    class ForemanAgent(_BaseAgent):  # type: ignore[misc]
        name: str = _AGENT_ID
        description: str = "Operations orchestrator — inventory, fulfillment, shipping, vendors."
        model: str = ""
        instruction: str = ""
        tools: list = []

        def __init__(self, **data: Any) -> None:
            from config import get_settings

            data["model"] = get_settings().models.LOCAL_MODEL
            data["instruction"] = _load_identity_file(_AGENT_ID)
            super().__init__(**data)
            self._graph = build_foreman_graph()

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

    class ForemanAgent:  # type: ignore[no-redef]
        name = _AGENT_ID

        def __init__(self, **_: Any) -> None:
            self._graph = build_foreman_graph()

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


foreman_graph = build_foreman_graph()

# ---------------------------------------------------------------------------
# TODO (Phase 3): Implement Foreman orchestrator.
#
# Required per Docs/GAOS-Agent-Spec.md:
#   - ADK Agent subclass
#   - LangGraph StateGraph with nodes:
#       plan, dispatch, collect, report, park, resume, escalate
#   - Pub/Sub topic:  agent.foreman.events  (publish)
#   - Pub/Sub subscribe: agent.nexus-prime.events, agent.pursuit.events,
#                        agent.approvals.events
#   - Sheet write: Shipping and Receiving tab only
#   - Memory Bank: read/write (domain: operations)
#   - model = settings.models.LOCAL_MODEL for status formatting/tracking;
#             FAST_MODEL for vendor routing decisions
#   - Agent boot sequence (Docs/GAOS-Agent-Spec.md §6)
#
# Business-specific Tier 3 task agents go in agents/foreman/tasks/
# (gitignored — never committed to the public repo).
# Examples: inventory_monitor.py, shipment_tracker.py, vendor_notifier.py
# ---------------------------------------------------------------------------
