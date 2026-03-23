from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
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

_AGENT_ID = "scout"
_SHEET_TAB = "Research Products"
_OUTBOUND_TOPIC = "agent.scout.events"
_INBOUND_TOPICS = [
    "agent.nexus-prime.events",
    "agent.foreman.events",
    "agent.approvals.events",
]


def _fast() -> str:
    from config import get_settings

    return get_settings().models.FAST_MODEL


def _local() -> str:
    from config import get_settings

    return get_settings().models.LOCAL_MODEL


# ── Boot ──────────────────────────────────────────────────────────────────────


def _boot(state: AgentWorkingMemory) -> AgentWorkingMemory:
    import sys

    from config import get_settings
    from tools.memory import load_domain_memory, query_episodic
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

    try:
        get_secret("GEMINI_API_KEY", pid)
    except (SecretNotFoundError, SecretAccessDenied) as exc:
        _log_cloud(_AGENT_ID, pid, "security", "boot", f"STARTUP_FAILURE: {exc}", "CRITICAL")
        sys.exit(1)

    try:
        ensure_topic_exists(_OUTBOUND_TOPIC)
    except Exception:
        pass
    try:
        state["memory_context"] = load_domain_memory(agent_id=_AGENT_ID, project_id=pid)
    except Exception:
        pass
    try:
        state["episodic_cache"] = {"recent": query_episodic(_AGENT_ID, pid, "research", limit=5)}
    except Exception:
        pass

    _write_heartbeat(_AGENT_ID, pid, "IDLE", "Boot complete", 0, "", _SHEET_TAB)
    return state


# ── Plan ──────────────────────────────────────────────────────────────────────


def _plan(state: AgentWorkingMemory) -> AgentWorkingMemory:
    from tools.google_sheets import get_all_records

    state["step_count"] = state.get("step_count", 0) + 1
    pid = state["project_id"]
    pending_items: list[dict] = []

    try:
        rows = get_all_records(_SHEET_TAB, pid)
        # Products needing fresh research: stale or flagged for review
        pending_items = [
            r
            for r in rows
            if r.get("research_status") in ("Stale", "Review Needed", "New")
            or not r.get("last_researched_at")
        ][:10]
    except Exception:
        pass

    msg = state.get("incoming_message")
    # Stockout alert from Foreman → trigger urgent sourcing research
    if msg and msg.message_type == MessageType.ALERT:
        payload = msg.payload or {}
        if payload.get("alert_type") == "stock_insufficient":
            pending_items.insert(
                0,
                {
                    "research_status": "Urgent",
                    "task_type": "sourcing_research",
                    "sku": payload.get("sku", ""),
                    "reason": "stockout_alert",
                },
            )
    elif msg and msg.message_type in (MessageType.TASK_HANDOFF, MessageType.BROADCAST):
        pending_items.append(msg.payload or {})

    # FAST_MODEL for trend routing per identity spec
    summary = [
        {
            k: v
            for k, v in r.items()
            if k
            in ("sku", "product_name", "research_status", "task_type", "reason", "competitor_alert")
        }
        for r in pending_items[:5]
    ]
    prompt = (
        f"Research and sourcing plan. Items to investigate: {summary}\n"
        f"Memory keys: {list(state.get('memory_context', {}).keys())[:5]}\n"
        'Top 3 tasks as JSON: [{"task_type": str, "item": dict, "priority": int}]'
    )
    resp = _call_model(prompt, model=_fast(), parse_json=True)
    state["cost_usd"] = state.get("cost_usd", 0.0) + resp.cost_usd
    tasks = resp.data if isinstance(resp.data, list) else []
    state["current_objective"] = f"Researching {len(tasks)} item(s)"
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

            mod = importlib.import_module(f"agents.scout.tasks.{task_type}")
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

    # Publish research findings to Beacon (market signals) and Foreman (sourcing)
    findings = [r for r in state["sub_task_results"] if r.get("status") == "success"]
    if findings:
        try:
            from tools.pubsub import publish

            publish(
                _OUTBOUND_TOPIC,
                A2AMessage(
                    source_agent=_AGENT_ID,
                    target_agent="broadcast",
                    project_id=state["project_id"],
                    task_id=state.get("task_id", str(uuid.uuid4())),
                    message_type=MessageType.BROADCAST,
                    priority=2,
                    payload={"research_findings": [r["output"] for r in findings]},
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
            "type": "research",
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
    """Write a playbook to Drive if this task originated from VISION_SUBMITTED."""
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


# ── Discover (RESEARCH_MANDATE handler) ──────────────────────────────────────


def _discover(state: AgentWorkingMemory) -> AgentWorkingMemory:
    """
    Handles RESEARCH_MANDATE messages. Runs a recursive web search loop up to
    max_search_depth levels and max_queries_per_mandate total Google Custom
    Search queries. Stores all raw results in sub_task_results and any findings
    corroborated across ≥5 independent sources in observation_buffer.

    Corroboration: after all results are collected, a single FAST_MODEL call
    groups them into distinct findings and counts how many independent source
    domains corroborate each finding. Only findings with source_count ≥ 5 and
    confidence ≥ 0.70 are flagged for KNOWLEDGE_INJECTION.

    Scout identity spec: RESEARCH_MANDATE → _discover → _inject_knowledge.
    """
    from config import get_settings
    from tools.google_search import GoogleSearchError, research_topic

    msg = state.get("incoming_message")
    if msg is None:
        return state

    settings = get_settings()
    pid = state["project_id"]
    payload = msg.payload or {}
    topic = payload.get("topic", "")
    mandate_id = msg.task_id or str(uuid.uuid4())
    max_depth = settings.google_search.max_search_depth
    max_queries = settings.google_search.max_queries_per_mandate

    state["current_objective"] = f"Researching mandate: {topic[:60]}"
    all_results: list[dict[str, Any]] = []
    queries_used = 0

    # Depth 0: LLM generates initial queries from the mandate topic
    init_prompt = (
        f"Generate up to {min(5, max_queries)} precise Google search queries to "
        f'research the following topic for a business intelligence mandate:\n"\n{topic}\n"\n'
        f"Focus on: market size, trends, competitor activity, and customer sentiment.\n"
        f'Return JSON: [{{"query": str, "intent": str}}, ...]'
    )
    init_resp = _call_model(init_prompt, model=_fast(), parse_json=True)
    state["cost_usd"] = state.get("cost_usd", 0.0) + init_resp.cost_usd
    initial_queries_raw = init_resp.data if isinstance(init_resp.data, list) else []
    query_strings = [q.get("query", "") for q in initial_queries_raw if q.get("query")]

    for depth in range(max_depth):
        if not query_strings or queries_used >= max_queries:
            break

        remaining = max_queries - queries_used
        batch_q = query_strings[: min(5, remaining)]
        try:
            batch_results = research_topic(batch_q, pid, max_queries=remaining)
            queries_used += len(batch_q)
            all_results.extend(batch_results)
        except GoogleSearchError as exc:
            _log_cloud(
                _AGENT_ID,
                pid,
                "task",
                mandate_id,
                f"_discover depth {depth} error: {exc}",
                "WARNING",
            )
            break

        # Generate follow-up queries from batch findings (not on last depth)
        if depth < max_depth - 1 and queries_used < max_queries and batch_results:
            snippets = [r.get("snippet", "")[:120] for r in batch_results[:6]]
            follow_prompt = (
                f"Based on these research findings about '{topic}':\n"
                + "\n".join(f"- {s}" for s in snippets if s)
                + f"\nGenerate {min(5, max_queries - queries_used)} deeper follow-up "
                f"search queries to uncover more specific competitive intelligence.\n"
                f'Return JSON: [{{"query": str}}, ...]'
            )
            follow_resp = _call_model(follow_prompt, model=_fast(), parse_json=True)
            state["cost_usd"] = state.get("cost_usd", 0.0) + follow_resp.cost_usd
            follow_raw = follow_resp.data if isinstance(follow_resp.data, list) else []
            query_strings = [q.get("query", "") for q in follow_raw if q.get("query")]
        else:
            break

    # Corroboration detection: LLM groups all snippets into distinct findings
    # and counts independent source domains supporting each one.
    corroborated: list[dict[str, Any]] = []
    if len(all_results) >= 5:
        snippet_lines = "\n".join(
            f"{i + 1}. [{r.get('title', '')}] {r.get('snippet', '')[:150]}"
            for i, r in enumerate(all_results[:30])
        )
        corr_prompt = (
            f"From these {len(all_results)} research results about '{topic}':\n"
            f"{snippet_lines}\n\n"
            f"Identify distinct factual findings that appear in 5 or more independent "
            f"source domains. For each corroborated finding write a clear declarative "
            f"statement. Count source_count conservatively — only count distinct domains.\n"
            f'Return JSON: [{{"finding": str, "source_count": int, "sources": [str, ...]}}]'
        )
        corr_resp = _call_model(corr_prompt, model=_fast(), parse_json=True)
        state["cost_usd"] = state.get("cost_usd", 0.0) + corr_resp.cost_usd
        candidates = corr_resp.data if isinstance(corr_resp.data, list) else []
        corroborated = [
            c for c in candidates if isinstance(c, dict) and c.get("source_count", 0) >= 5
        ]

    # Persist all raw results for downstream reporting / Sheet writes
    state["sub_task_results"] = [
        {"task_type": "discovery", "status": "success", "output": r, "mandate_id": mandate_id}
        for r in all_results
    ]

    # Corroborated findings go into observation_buffer for _inject_knowledge
    state["observation_buffer"] = [
        {
            "content": c.get("finding", ""),
            "knowledge_type": "market_intel",
            "tags": [topic[:30], "market_intel", str(datetime.now(UTC).year)],
            "source_count": c.get("source_count", 5),
            "sources": c.get("sources", []),
            "mandate_id": mandate_id,
            "confidence": min(0.70 + (c.get("source_count", 5) - 5) * 0.02, 0.95),
        }
        for c in corroborated
    ]
    state["iteration_count"] = queries_used

    _log_cloud(
        _AGENT_ID,
        pid,
        "task",
        mandate_id,
        f"_discover: {queries_used} queries, {len(all_results)} results, "
        f"{len(corroborated)} corroborated findings",
        "INFO",
    )
    return state


# ── Inject Knowledge (KNOWLEDGE_INJECTION publisher) ─────────────────────────


def _inject_knowledge(state: AgentWorkingMemory) -> AgentWorkingMemory:
    """
    Publishes a KNOWLEDGE_INJECTION message to Nexus-Prime for each batch of
    corroborated findings from the discovery loop. Also optionally appends a
    Section E Market Intelligence summary to the Blueprint Doc when the mandate
    payload specifies a ``blueprint_doc_id``.

    Only fired after _discover when observation_buffer is non-empty.
    Scout identity spec: publish KNOWLEDGE_INJECTION for findings corroborated
    across ≥5 independent sources (confidence ≥ 0.70, tag ``market_intel``).
    """
    from tools.pubsub import publish

    pid = state["project_id"]
    msg = state.get("incoming_message")
    mandate_id = msg.task_id if msg else str(uuid.uuid4())
    payload = (msg.payload or {}) if msg else {}
    corroborated = state.get("observation_buffer", [])
    total_found = len(state.get("sub_task_results", []))

    if corroborated:
        try:
            publish(
                _OUTBOUND_TOPIC,
                A2AMessage(
                    source_agent=_AGENT_ID,
                    target_agent="nexus-prime",
                    project_id=pid,
                    task_id=mandate_id,
                    message_type=MessageType.KNOWLEDGE_INJECTION,
                    priority=2,
                    payload={
                        "mandate_id": mandate_id,
                        "topic": payload.get("topic", ""),
                        "findings": corroborated,
                        "total_results_found": total_found,
                        "knowledge_type": "market_intel",
                    },
                ),
                pid,
            )
        except Exception as exc:
            _log_cloud(
                _AGENT_ID,
                pid,
                "task",
                mandate_id,
                f"_inject_knowledge: publish failed: {exc}",
                "WARNING",
            )

    # Append Section E Market Intelligence to Blueprint Doc if requested
    blueprint_doc_id = payload.get("blueprint_doc_id", "")
    if blueprint_doc_id and state.get("sub_task_results"):
        try:
            from tools.google_docs import append_content

            today = datetime.now(UTC).date().isoformat()
            findings_summary = (
                f"\n\n## Section E — Market Intelligence (Scout Discovery, {today})\n\n"
            )
            if corroborated:
                for i, obs in enumerate(corroborated[:10], 1):
                    findings_summary += (
                        f"{i}. {obs.get('content', '')} "
                        f"(corroborated across {obs.get('source_count', 5)} "
                        f"independent sources)\n"
                    )
            else:
                findings_summary += "_No findings corroborated across ≥5 independent sources._\n"
            append_content(blueprint_doc_id, findings_summary, pid)
        except Exception as exc:
            _log_cloud(
                _AGENT_ID,
                pid,
                "task",
                mandate_id,
                f"_inject_knowledge: append_content failed: {exc}",
                "WARNING",
            )

    return state


# ── Routing helper ────────────────────────────────────────────────────────────


def _route_after_boot(state: AgentWorkingMemory) -> str:
    """
    Route RESEARCH_MANDATE messages to the _discover→_inject_knowledge path.
    All other messages follow the standard plan→dispatch→collect path.
    """
    msg = state.get("incoming_message")
    if msg and msg.message_type == MessageType.RESEARCH_MANDATE:
        return "discover"
    return "plan"


def build_scout_graph() -> Any:
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
        ("discover", _discover),
        ("inject_knowledge", _inject_knowledge),
    ]:
        graph.add_node(name, fn)
    graph.set_entry_point("boot")
    # RESEARCH_MANDATE → discover → inject_knowledge → write_playbook path
    graph.add_conditional_edges("boot", _route_after_boot, {"discover": "discover", "plan": "plan"})
    graph.add_edge("discover", "inject_knowledge")
    graph.add_edge("inject_knowledge", "write_playbook")
    # Standard research path
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

    class ScoutAgent(_BaseAgent):  # type: ignore[misc]
        name: str = _AGENT_ID
        description: str = (
            "Research orchestrator — market intelligence, competitor monitoring, sourcing."
        )
        model: str = ""
        instruction: str = ""
        tools: list = []

        def __init__(self, **data: Any) -> None:
            from config import get_settings

            data["model"] = get_settings().models.FAST_MODEL
            data["instruction"] = _load_identity_file(_AGENT_ID)
            super().__init__(**data)
            self._graph = build_scout_graph()

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

    class ScoutAgent:  # type: ignore[no-redef]
        name = _AGENT_ID

        def __init__(self, **_: Any) -> None:
            self._graph = build_scout_graph()

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

    incoming: A2AMessage | None = None
    pid = ""
    tid = str(uuid.uuid4())

    if isinstance(agent_input, AgentInput):
        pid = agent_input.project_id
        tid = agent_input.task_id
    elif isinstance(agent_input, dict):
        # Decode Pub/Sub push envelope when called from the Cloud Run handler
        try:
            from tools.pubsub import decode_push_message

            incoming = decode_push_message(agent_input)
            pid = incoming.project_id
            tid = incoming.task_id or str(uuid.uuid4())
        except Exception:
            pass

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
        "incoming_message": incoming,
        "messages": [],
        "hard_stop_triggered": False,
        "evolution_triggered": False,
        "_started_at": time.time(),
    }


scout_graph = build_scout_graph()
