"""
agents/beacon/orchestrator.py — Tier 2 Marketing Orchestrator

Beacon monitors campaign performance, ad spend, and marketing ROI.
It owns the Marketing, Sales Graphs, and Ad Response/Spend tabs and
proposes strategy changes via the Approval Gate.

Construction spec: Docs/GAOS-Agent-Spec.md
Identity file:     Docs/agents/beacon.md
Master spec:       Docs/GAOS-Manager-Spec.md §1.2
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from agents import (
    _call_model,
    _load_identity_file,
    _log_cloud,
    _write_heartbeat,
    utcnow_iso,
)
from models import A2AMessage, AgentWorkingMemory, MessageType

# ── Domain constants ──────────────────────────────────────────────────────────

_AGENT_ID = "beacon"
_SHEET_TAB = "Marketing"
_OUTBOUND_TOPIC = "agent.beacon.events"
_INBOUND_TOPICS = [
    "agent.nexus-prime.events",
    "agent.pursuit.events",
    "agent.scout.events",
    "agent.approvals.events",
]


def _local() -> str:
    from config import get_settings

    return get_settings().models.LOCAL_MODEL


# ── Graph nodes ───────────────────────────────────────────────────────────────


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

    # Step 4: Pub/Sub — ensure outbound topic exists
    try:
        ensure_topic_exists(_OUTBOUND_TOPIC)
    except Exception:
        pass

    try:
        state["memory_context"] = load_domain_memory(agent_id=_AGENT_ID, project_id=pid)
    except Exception:
        pass

    try:
        state["episodic_cache"] = {"recent": query_episodic(_AGENT_ID, pid, "marketing", limit=5)}
    except Exception:
        pass

    _write_heartbeat(_AGENT_ID, pid, "IDLE", "Boot complete", 0, "", _SHEET_TAB)
    return state


def _plan(state: AgentWorkingMemory) -> AgentWorkingMemory:
    from tools.google_sheets import get_all_records

    state["step_count"] = state.get("step_count", 0) + 1
    pid = state["project_id"]
    pending_items: list[dict] = []

    try:
        rows = get_all_records(_SHEET_TAB, pid)
        flagged = [
            r
            for r in rows
            if float(r.get("roas", 1.0) or 1.0) < 1.0
            or str(r.get("status", "")).lower() == "review"
        ]
        pending_items = flagged[:10]
    except Exception:
        pass

    msg = state.get("incoming_message")
    if msg and msg.message_type in (MessageType.TASK_HANDOFF, MessageType.BROADCAST):
        pending_items.append(msg.payload or {})
    elif msg and msg.message_type == MessageType.ALERT:
        a_payload = msg.payload or {}
        if a_payload.get("alert_type") == "low_margin":
            # Nexus-Prime detected a low-margin deal close — prioritise lead-source ROI analysis
            pending_items.insert(
                0,
                {
                    "task_type": "lead_source_roi_analysis",
                    "lead_source": a_payload.get("lead_source", "unknown"),
                    "deal_id": a_payload.get("deal_id", ""),
                    "margin_pct": a_payload.get("margin_pct", 0.0),
                    "reason": "low_margin_deal_closed",
                },
            )
        else:
            _log_cloud(
                _AGENT_ID,
                pid,
                "task",
                state.get("task_id", ""),
                f"_plan: unrecognized alert_type={a_payload.get('alert_type')!r} — no action taken",
                "WARNING",
            )

    prompt = (
        f"Marketing campaign plan.\nFlagged: {pending_items[:5]}\n"
        f"Memory keys: {list(state.get('memory_context', {}).keys())[:5]}\n"
        'Top 3 tasks as JSON: [{"task_type": str, "item": dict}]'
    )
    resp = _call_model(prompt, model=_local(), parse_json=True)
    state["cost_usd"] = state.get("cost_usd", 0.0) + resp.cost_usd
    tasks = resp.data if isinstance(resp.data, list) else []
    state["current_objective"] = f"Processing {len(tasks)} marketing task(s)"
    state["sub_task_results"] = []
    state["messages"].append({"role": "assistant", "content": resp.text, "tasks": tasks})
    return state


def _dispatch(state: AgentWorkingMemory) -> AgentWorkingMemory:
    planned = state["messages"][-1].get("tasks", []) if state.get("messages") else []

    for task in planned[:5]:
        task_type = task.get("task_type", "unknown")
        item = task.get("item", {})
        result: dict[str, Any] = {"task_type": task_type, "status": "skipped", "output": {}}
        try:
            import importlib

            mod = importlib.import_module(f"agents.beacon.tasks.{task_type}")
            from models import AgentInput

            out = mod.run(
                AgentInput(
                    task_id=str(uuid.uuid4()),
                    project_id=state["project_id"],
                    instruction=f"Process: {task_type}",
                    context=item,
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
        except Exception:
            pass

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
    except Exception:
        pass

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
    from tools.pubsub import publish

    pid = state["project_id"]
    proposal_id = str(uuid.uuid4())
    state["parked_proposals"].append(proposal_id)
    try:
        publish(
            _OUTBOUND_TOPIC,
            A2AMessage(
                source_agent=_AGENT_ID,
                target_agent="nexus-prime",
                project_id=pid,
                task_id=state.get("task_id", proposal_id),
                message_type=MessageType.TASK_HANDOFF,
                priority=3,
                payload={"proposal_id": proposal_id},
            ),
        )
    except Exception:
        pass
    _write_heartbeat(
        _AGENT_ID,
        pid,
        "PARKED",
        f"Parked {proposal_id}",
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
    except Exception:
        pass
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


def build_beacon_graph() -> Any:
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
    ]:
        graph.add_node(name, fn)
    graph.set_entry_point("boot")
    graph.add_edge("boot", "plan")
    graph.add_edge("plan", "dispatch")
    graph.add_edge("dispatch", "collect")
    graph.add_conditional_edges(
        "collect", _should_escalate, {"escalate": "escalate", "report": "report"}
    )
    graph.add_edge("report", "write_playbook")
    graph.add_edge("write_playbook", END)
    graph.add_edge("park", END)
    graph.add_edge("resume", "plan")
    graph.add_edge("escalate", END)
    return graph.compile(checkpointer=MemorySaver())


# ── ADK Agent class ───────────────────────────────────────────────────────────

try:
    from google.adk.agents import Agent as _BaseAgent

    _HAS_ADK = True
except ImportError:
    _HAS_ADK = False


if _HAS_ADK:

    class BeaconAgent(_BaseAgent):  # type: ignore[misc]
        name: str = _AGENT_ID
        description: str = "Marketing orchestrator — campaign performance, ad spend, ROI."
        model: str = ""
        instruction: str = ""
        tools: list = []

        def __init__(self, **data: Any) -> None:
            from config import get_settings

            data["model"] = get_settings().models.LOCAL_MODEL
            data["instruction"] = _load_identity_file(_AGENT_ID)
            super().__init__(**data)
            self._graph = build_beacon_graph()

        async def run(self, agent_input: Any) -> Any:
            from models import AgentOutput

            initial = _initial_state(agent_input)
            try:
                final = await self._graph.ainvoke(
                    initial, config={"configurable": {"thread_id": initial["task_id"]}}
                )
                status = "failed" if final.get("hard_stop_triggered") else "success"
            except Exception as exc:
                _log_cloud(_AGENT_ID, "", "task", initial["task_id"], str(exc), "ERROR")
                status = "failed"
                final = initial
            return AgentOutput(
                task_id=final["task_id"],
                project_id=final["project_id"],
                agent_id=_AGENT_ID,
                status=status,
                result={},
                cost_usd=final.get("cost_usd", 0.0),
            )

else:

    class BeaconAgent:  # type: ignore[no-redef]
        name = _AGENT_ID

        def __init__(self, **_: Any) -> None:
            self._graph = build_beacon_graph()

        async def run(self, agent_input: Any) -> Any:
            from models import AgentOutput

            initial = _initial_state(agent_input)
            try:
                final = await self._graph.ainvoke(
                    initial, config={"configurable": {"thread_id": initial["task_id"]}}
                )
                status = "failed" if final.get("hard_stop_triggered") else "success"
            except Exception as exc:
                _log_cloud(self.name, "", "task", initial["task_id"], str(exc), "ERROR")
                status = "failed"
                final = initial
            return AgentOutput(
                task_id=final["task_id"],
                project_id=final["project_id"],
                agent_id=self.name,
                status=status,
                result={},
                cost_usd=final.get("cost_usd", 0.0),
            )


def _initial_state(agent_input: Any) -> AgentWorkingMemory:
    from models import AgentInput

    pid = agent_input.project_id if isinstance(agent_input, AgentInput) else ""
    tid = agent_input.task_id if isinstance(agent_input, AgentInput) else str(uuid.uuid4())
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


beacon_graph = build_beacon_graph()
