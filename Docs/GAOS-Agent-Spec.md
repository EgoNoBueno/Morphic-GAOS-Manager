# GAOS Agent Construction Specification

**Morphic-G AOS** — Agent Construction Requirements for Orchestrator Agents (Tier 2) and Sub-Agents (Tier 3)

> This document defines the mandatory construction requirements every agent in the system must satisfy before it is considered complete and deployable. It is the counterpart to the master specification (`GAOS-Manager-Spec.md`) and is referenced from Section 1.3 of that document.
>
> **Importing external agents or tools?** Before applying this checklist, the skill must first pass all required gates in `GAOS-Skill-Compliance-Spec.md`. That document defines the security, supply chain, and integration review process for code sourced outside this repository. The §8 checklists below are the *final* step — not the first.
>
> **Identity files** (per-agent persona, back story, goal, desired result, guardrails) follow the template in `GAOS-Manager-Spec.md` §18.2 and live in `Docs/agents/<name>.md`. This document covers the *engineering* requirements — how an agent is built, wired, and tested.

---

## 1. Agent Tiers — Summary of Obligations

| Requirement | Tier 1 (Nexus-Prime) | Tier 2 (Orchestrator) | Tier 3 (Sub-Agent) |
|-------------|---------------------|-----------------------|--------------------|
| Identity file (`Docs/agents/`) | ✗ governed by `GAOS-Manager-Spec.md` §1 | ✅ Required | ✅ Required |
| ADK `Agent` class | ✅ | ✅ | ✅ |
| Pydantic input/output schemas | ✅ | ✅ | ✅ |
| Pub/Sub outbound topic | ✅ | ✅ | ✗ reports to orchestrator |
| Pub/Sub inbound subscription | ✅ (all topics) | ✅ (nexus + cross-domain per §10) | ✗ receives work via function call |
| LangGraph state machine | ✅ | ✅ | ✗ stateless task runner |
| `project_id` scoping | ✅ | ✅ | ✅ inherited from parent |
| Sheet Write access | Global (control plane) | Own domain tab only | ✗ orchestrator writes on their behalf |
| Memory Bank (read/write) | ✅ (business rules, global context) | ✅ (domain knowledge) | ✗ reads via orchestrator |
| Code Execution sandbox | ✅ (evolution tasks) | ✅ (evolution tasks) | ✗ |
| Approval Gate proposals | ✅ | ✅ | ✗ escalates to orchestrator |
| Static analysis gate before deploy | ✅ | ✅ | ✅ (if producing code) |
| Model selection rule | DEEP_MODEL | context-dependent (see §5) | LOCAL_MODEL first |
| Human-visible on dashboard | ✅ | ✅ | ✗ |

---

## 2. Universal Requirements (All Tiers)

Every agent — regardless of tier — must satisfy all requirements in this section before it is considered complete.

### 2.1 ADK Agent Class

All agents are implemented as Google ADK `Agent` subclasses. The constructor must declare:

```python
from google.adk import Agent
from pydantic import BaseModel

class MyAgent(Agent):
    name: str = "agent_name"          # lowercase snake_case, unique across the system
    description: str = "..."          # one sentence; shown in ADK registry
    model: str                        # resolved from settings.yaml — never hardcoded
    instruction: str                  # system prompt = identity file contents + task context + result desired
    tools: list                       # explicit list — no wildcard tool access
```

`model` must be assigned from `settings.yaml` via one of the three aliases (`LOCAL_MODEL`, `FAST_MODEL`, `DEEP_MODEL`). Hardcoded Gemini version strings are forbidden — see `GAOS-Manager-Spec.md` §11.1.

### 2.2 Pydantic Input / Output Schemas

Every agent must define typed schemas for what it accepts and what it returns. No `dict` or `Any` typed inputs at agent boundaries.

```python
from typing import Literal

class AgentInput(BaseModel):
    task_id: str           # UUID — links all log entries for one task
    project_id: str        # Project namespace — never omit
    instruction: str       # Natural language task description
    context: dict          # Structured context relevant to the task
    # NOTE: concrete agent subclasses should replace `context: dict` with a
    # typed sub-model (e.g., `context: LedgerTaskContext`) to satisfy the
    # no-bare-dict rule at their own boundary.

class AgentOutput(BaseModel):
    task_id: str
    project_id: str
    agent_id: str          # Matches Agent.name
    status: Literal["success", "escalated", "failed"]
    result: dict           # Task-specific output — replace with typed sub-model
    cost_usd: float        # Accumulated model cost for this task
    timestamp: datetime
```

### 2.3 `project_id` Scoping

Every agent action — Sheet writes, Pub/Sub publishes, Cloud Logging entries, Memory Bank reads/writes — must include the `project_id`. No agent may read or write data from a namespace it was not dispatched to.

**Enforcement:** The `project_id` is passed in `AgentInput` and must be forwarded to every downstream call. An agent that drops `project_id` from a sub-call is a bug.

### 2.4 Logging Standard

All agents write structured log entries to Cloud Logging using the labels defined in `GAOS-Manager-Spec.md` §13.2. Minimum required labels on every entry:

```
agent_id:     "<agent_name>"
project_id:   "<project_slug>"
log_type:     "task" | "evolution_task" | "escalation" | "security"
task_id:      "<uuid>"
```

Local (`LOCAL_MODEL`) agents produce a plain-text summary line in addition to the structured entry, for the weekly Ollama summarization job.

### 2.5 Cost Tracking

Every agent must accumulate `cost_usd` for each model call made during a task and return the total in `AgentOutput`. All three model aliases (`LOCAL_MODEL`, `FAST_MODEL`, and `DEEP_MODEL`) currently return `cost_usd = 0.0` — per-call cost calculation is not implemented. Ollama is free (local); Gemini charges are tracked at the GCP billing level (see `GAOS-Manager-Spec.md` §9.4) rather than per-call. `tokens_used` is still tracked in `ModelResponse` for usage monitoring. Re-evaluate per-call tracking if finer-grained cost attribution is needed.

---

## 3. Tier 2 — Orchestrator Agent Requirements

Orchestrator agents (Ledger, Beacon, Pursuit, Foreman, Steward, Scout) are the human-visible layer. Each must satisfy all of §2 plus the following.

### 3.1 Identity File

A complete `Docs/agents/<name>.md` file following the template in `GAOS-Manager-Spec.md` §18.2. The file is prepended verbatim to the agent's `instruction` field on every ADK session. It must be populated before the agent is deployed.

**Minimum content checklist:**
- [ ] Persona (first-person, one sentence)
- [ ] Single measurable primary Goal
- [ ] 3+ Objectives (ongoing recurring tasks)
- [ ] Resources table (Sheet tab, Pub/Sub topics with access type)
- [ ] Specification (what decisions this agent owns; what it does *not* touch)
- [ ] Do / Don't Guardrails (minimum: the three universal Don'ts below)
- [ ] Escalation Rules table (minimum 3 conditions)
- [ ] Knowledge Sources list

**Universal Don'ts (must appear in every orchestrator identity file):**
1. Never approve your own proposals.
2. Never write to Sheet tabs owned by another orchestrator.
3. Never call `DEEP_MODEL` for tasks classifiable as logging, formatting, or summarization.

### 3.2 LangGraph State Machine

Orchestrators manage long-running, stateful workflows. Each orchestrator must declare an explicit LangGraph graph with named nodes for each major workflow stage.

```python
from langgraph.graph import StateGraph
# Full state schema is AgentWorkingMemory — defined in GAOS-Memory-Spec.md §3.
# Import and use the shared TypedDict; do not redefine it here.
from tools.memory import AgentWorkingMemory

# Required nodes (minimum).
# You must also: set_entry_point(), define edges between nodes, then call compile().
graph = StateGraph(AgentWorkingMemory)
graph.add_node("plan",      plan_node)       # decide what to do next
graph.add_node("dispatch",  dispatch_node)   # spawn Tier 3 sub-agent tasks
graph.add_node("collect",   collect_node)    # gather sub-agent results
graph.add_node("report",    report_node)     # write to Sheet + Pub/Sub
graph.add_node("park",      park_node)       # park task pending approval
graph.add_node("resume",    resume_node)     # resume after Pub/Sub push
graph.add_node("escalate",  escalate_node)   # publish ESCALATION to Nexus-Prime
graph.set_entry_point("plan")
# ... add_edge() / add_conditional_edges() calls ...
compiled_graph = graph.compile(checkpointer=memory_checkpointer)
```

The `park` → `resume` cycle is the human-in-the-loop pattern for the Approval Gate. The orchestrator must **never block** waiting for an approval; it parks and moves on (see `GAOS-Manager-Spec.md` §3.B).

### 3.3 Pub/Sub Wiring

Each orchestrator owns one outbound topic and must subscribe to Nexus-Prime's broadcast topic and any cross-domain topics required by its workflow policies (see `GAOS-Manager-Spec.md` §10).

| Resource | Pattern |
|----------|---------|
| Outbound topic | `agent/<name>/events` |
| Subscription to Nexus-Prime | `agent/nexus-prime/events` |
| Cross-domain subscriptions | As defined in `GAOS-Manager-Spec.md` §10.1 |

> **Topic naming note:** The display form uses `/` as the separator (e.g. `agent/beacon/events`). GCP Pub/Sub topic names cannot contain `/`, so `tools/pubsub.py` replaces slashes with dots when building the resource path (e.g. `agent.beacon.events`). The GAOS business `project_id` (e.g. `acme-retail`) is *not* part of the topic name — it travels inside `A2AMessage.project_id`. The GCP project that owns the topic is resolved from `settings.GCP_PROJECT_ID` at publish time.

All published messages must use the `A2AMessage` schema from `GAOS-Manager-Spec.md` §10.2, including `project_id`.

### 3.4 Dashboard Heartbeat

The orchestrator must write a status row to its designated Sheet tab at the end of every work cycle. Minimum fields:

| Field | Value |
|-------|-------|
| `timestamp` | ISO 8601 |
| `agent_id` | Agent name |
| `project_id` | Active project slug |
| `status` | `IDLE` / `WORKING` / `PARKED` / `ESCALATED` / `ERROR` |
| `current_objective` | One-line description of what it is doing right now |
| `open_proposals` | Count of parked proposals awaiting human approval |
| `last_error` | Most recent error message, or blank |

This row is what the CEO dashboard reads. It must be present within **60 seconds** of the agent starting a new cycle.

### 3.5 Approval Gate Integration

When an orchestrator identifies a task requiring human approval:

1. Build a complete proposal row (fields defined in `GAOS-Manager-Spec.md` §14, including `code_sha256` if code is attached).
2. Write the row to `Agent_Approvals` via `tools/google_sheets.py`.
3. Publish an `APPROVAL_REQUEST` (`message_type`) to `agent/approvals/events` targeting `nexus-prime`.
4. Call `park_task(task_id)` on the LangGraph state — do not wait.
5. On receiving the Pub/Sub resume event (`APPROVAL_RESULT`), call `resume_task(task_id)` and continue.

No orchestrator may bypass the Approval Gate for Priority-3, 4, or 5 proposals.

### 3.6 Self-Evolution Capability

Orchestrators must be capable of entering the Write-Test-Refine loop (see `GAOS-Manager-Spec.md` §13) when they encounter a capability gap. The orchestrator must:

- Implement all loop constraints from `GAOS-Manager-Spec.md` §13.1 (iteration cap, TTL, cost cap, no-progress detector).
- Emit an `EvolutionTaskOutcome` log entry on every exit (success or constraint trigger) per `GAOS-Manager-Spec.md` §13.2.
- Never deploy evolved code without first sending a Priority-4 proposal through the Approval Gate.

### 3.7 Model Selection Rules

| Task Type | Required Model |
|-----------|---------------|
| Logging, formatting, summarizing, field extraction | `LOCAL_MODEL` |
| API response parsing, moderate reasoning, search results | `FAST_MODEL` |
| Approval Gate proposals, system re-architecture, conflict resolution | `DEEP_MODEL` |
| Write-Test-Refine — code drafting | `FAST_MODEL` (iterations 1–3), `DEEP_MODEL` (iterations 4–5) |

Deviating from this table requires a justification comment in the code.

---

## 4. Tier 3 — Sub-Agent (Task Agent) Requirements

Sub-agents are stateless, single-purpose task runners spawned by an orchestrator. They do not interact with the human dashboard, do not publish to Pub/Sub, and do not have an Approval Gate. They satisfy all of §2 plus the following.

### 4.1 Identity File

A complete `Docs/agents/<name>.md` file is still required for Tier 3 agents, but the template is simplified. Required sections: **Persona**, **Goal**, **Specification**, **Guardrails**. Escalation Rules and Pub/Sub Resources are not required (Tier 3 escalates by returning `status: "escalated"` in `AgentOutput`).

### 4.2 Stateless Design

Sub-agents must be **stateless** — no LangGraph graph, no parked tasks. They receive an `AgentInput`, complete the task, and return an `AgentOutput`. If the task cannot be completed within one call (e.g., external API is unavailable), they return `status: "escalated"` and the orchestrator decides whether to retry, park, or escalate further.

```python
async def run(self, agent_input: AgentInput) -> AgentOutput:
    # NOTE: parameter is named `agent_input`, not `input`, to avoid shadowing
    # the Python built-in.
    try:
        result = await self._do_work(agent_input)
        return AgentOutput(status="success", result=result, ...)
    except RecoverableError as e:
        return AgentOutput(status="escalated", result={"reason": str(e)}, ...)
    except Exception as e:
        return AgentOutput(status="failed", result={"error": str(e)}, ...)
```

Sub-agents must **not** raise unhandled exceptions to the orchestrator. All errors must be caught and returned as typed `AgentOutput`.

### 4.3 Tool Scope

Sub-agents have the narrowest possible tool scope. Tools must be declared explicitly; no tool is inherited from the parent orchestrator unless explicitly passed in the `tools` list.

```python
# Example: Invoice Parser (Tier 3 under Ledger)
class InvoiceParser(Agent):
    tools = [
        read_sheet_range,      # read-only; no write access
        extract_with_llm,      # LOCAL_MODEL call for field extraction
    ]
    # NOT in tools: write_sheet_row, publish_pubsub, exec_code, ...
```

A sub-agent that needs write access to the Sheet must return its result to the orchestrator; the orchestrator writes.

### 4.4 Model Selection

Sub-agents use `LOCAL_MODEL` by default. They may escalate to `FAST_MODEL` only when:
- The local model is unavailable (fallback per `GAOS-Manager-Spec.md` §5), **or**
- The task explicitly requires web-current knowledge (e.g., search result parsing).

Sub-agents must **never** call `DEEP_MODEL`.

### 4.5 `project_id` Inheritance

Sub-agents receive `project_id` from the orchestrator's `AgentInput`. They must pass it through to every tool call. They cannot operate across multiple `project_id` values in a single invocation.

---

## 5. Model Selection

Every agent selects its model via a `settings.yaml` alias — never a hardcoded version string. The three aliases and their intended uses:

| Alias | Default value | When to use |
|-------|--------------|-------------|
| `LOCAL_MODEL` | `ollama/llama3` | Logging, formatting, summarisation, data classification — any task where cloud LLM quality is not required. Zero API cost. |
| `FAST_MODEL` | `gemini-2.5-flash` | Routing decisions, structured data extraction, tasks needing current knowledge but not deep reasoning. Low cost. |
| `DEEP_MODEL` | `gemini-2.5-pro` | Complex multi-step reasoning, cross-domain synthesis, final approval gate analysis. Higher cost — use sparingly. |

**Tier defaults** (from the obligations table in §1):
- Tier 1 (Nexus-Prime): `DEEP_MODEL`
- Tier 2 (Orchestrators): context-dependent — use `LOCAL_MODEL` for routine work, `FAST_MODEL` for domain decisions, `DEEP_MODEL` for evolution loop final analysis
- Tier 3 (Sub-Agents): `LOCAL_MODEL` first; escalate to `FAST_MODEL` only if output quality requires it

**`LOCAL_MODEL` with web access:** Pass `web_access=True` to `_call_model()` when the task references real-world current data (prices, events, competitor activity) and Gemini-quality reasoning is not required. See `GAOS-Tools-Spec.md` §10 for the full design. Do not use `web_access=True` with `FAST_MODEL` or `DEEP_MODEL`.

**Fallback behaviour:** If Ollama is unreachable, `_call_model()` automatically falls back to `LOCAL_MODEL_FALLBACK` (`gemini-2.5-flash`). Agents do not need to handle this — it is transparent.

---

## 6. Naming Conventions

| Element | Convention | Example |
|---------|-----------|---------|
| Agent Python class | `PascalCase` | `InvoiceParser`, `BeaconAgent` |
| Agent `name` field | `snake_case` | `invoice_parser`, `beacon` |
| Identity file | `kebab-case.md` (but name matches `Agent.name`) | `invoice-parser.md`, `beacon.md` |
| Pub/Sub topic | `agent/<snake_name>/events` (display); `agent.<snake_name>.events` (GCP name) | `agent/beacon/events` |
| LangGraph graph variable | `<name>_graph` | `beacon_graph` |
| Pydantic schemas | `<ClassName>Input`, `<ClassName>Output` | `BeaconInput`, `BeaconOutput` |
| File location — Tier 2 | `agents/<name>/orchestrator.py` | `agents/beacon/orchestrator.py` |
| File location — Tier 3 | `agents/<name>/tasks/<task_name>.py` | `agents/ledger/tasks/invoice_parser.py` |

---

## 7. Agent Boot Sequence

When an orchestrator agent starts (either on Cloud Run invocation or on Nexus-Prime dispatch), it must execute these steps in order before processing any task:

1. **Load identity file** — read `Docs/agents/<name>.md`; set as `instruction` prefix. The file must be bundled into the container image at build time (copy `Docs/agents/` into the image) — the `Docs/` directory is not available at Cloud Run runtime otherwise.
2. **Load `settings.yaml`** — resolve model aliases (`LOCAL_MODEL`, `FAST_MODEL`, `DEEP_MODEL`).
3. **Load secrets** — call `get_secret()` for all secrets in the agent's inventory; fail fast if any are missing.
4. **Read Project Registry** — fetch active `project_id` list from the Sheet; reject work for unknown or paused projects.
5. **Connect Pub/Sub** — verify outbound topic exists; create if absent (idempotent).
6. **Write IDLE heartbeat** — post initial status row to the dashboard tab.
7. **Begin event loop** — subscribe to inbound Pub/Sub topics and start processing.

If any step fails, the agent must log a `STARTUP_FAILURE` security event and exit cleanly (no partial state).

---

## 8. Per-Agent Completion Checklist

An agent is **not complete** until every item below is checked. This checklist must be satisfied before the agent is added to the Implementation Checklist in `GAOS-Manager-Spec.md` §17.

### Tier 2 Orchestrator Checklist

- [ ] Identity file (`Docs/agents/<name>.md`) complete — all 8 required sections present
- [ ] ADK `Agent` class created — `name`, `model` (alias), `instruction`, `tools` declared
- [ ] Pydantic `Input` and `Output` schemas defined — no `dict` or `Any` typed fields
- [ ] LangGraph graph declared — minimum 7 nodes: `plan`, `dispatch`, `collect`, `report`, `park`, `resume`, `escalate`
- [ ] Pub/Sub outbound topic created and named per §6 convention
- [ ] Pub/Sub inbound subscription to Nexus-Prime + required cross-domain topics
- [ ] `A2AMessage` schema used for all published messages (including `project_id`)
- [ ] Dashboard heartbeat writes at end of every cycle
- [ ] Approval Gate integration: `park`/`resume` cycle implemented; no busy-waits
- [ ] Self-evolution: loop constraints (§13.1), outcome logging (§13.2), and Approval Gate proposal for code deploy
- [ ] Model selection: all `DEEP_MODEL` calls justified in comments; `LOCAL_MODEL` used for all routine tasks
- [ ] Cost accumulation: `cost_usd` tracked and returned in `AgentOutput`
- [ ] Cloud Logging labels applied to all log entries
- [ ] Boot sequence (§7) implemented
- [ ] All secrets accessed via `get_secret()`; no hardcoded credentials
- [ ] `project_id` forwarded to every tool call; no cross-project data access
- [ ] Unit tests pass (see §9)

### Tier 3 Sub-Agent Checklist

- [ ] Identity file (`Docs/agents/<name>.md`) complete — Persona, Goal, Specification, Guardrails present
- [ ] ADK `Agent` class created — `name`, `model` (LOCAL_MODEL), `instruction`, `tools` declared
- [ ] Pydantic `Input` and `Output` schemas defined
- [ ] Stateless design — no LangGraph; all errors returned via `AgentOutput.status`
- [ ] Tool scope minimised — only tools required for the single task
- [ ] No `DEEP_MODEL` calls anywhere in agent code or tools
- [ ] `project_id` inherited from orchestrator and forwarded to all tool calls
- [ ] Cloud Logging labels applied
- [ ] All secrets accessed via `get_secret()`
- [ ] Unit tests pass (see §9)

---

## 9. Testing Requirements

All agents must pass the following tests before they are considered deployable.

### 9.1 Unit Tests (All Tiers)

| # | Test | Pass Condition |
|---|------|----------------|
| U1 | Valid `AgentInput` produces typed `AgentOutput` | No `dict`/`Any` fields; `status` is one of the expected values |
| U2 | `project_id` appears in every downstream tool call | Verified via mock assertions on each tool |
| U3 | All model calls use the alias from `settings.yaml` | No literal Gemini version string in code |
| U4 | Missing secret causes `STARTUP_FAILURE` log + clean exit | Agent does not continue with a `None` secret value |
| U5 | Unknown `project_id` returns `status: "failed"` | Agent does not process work for unlisted projects |

### 9.2 Tier 2 Integration Tests

| # | Test | Pass Condition |
|---|------|----------------|
| I1 | Boot sequence completes — IDLE heartbeat appears in Sheet within 60 s | Row present in dashboard tab with `status = IDLE` |
| I2 | Orchestrator parks a Priority-4 task and publishes `TASK_HANDOFF` to Pub/Sub | Parked task is in LangGraph state; message appears on topic |
| I3 | Orchestrator resumes correctly when approval event arrives via Pub/Sub | Task ID matched; execution continues from parked state |
| I4 | Self-evolution loop: iteration cap triggers at iteration 5 | Loop does not start iteration 6; `EvolutionTaskOutcome` logged with `stopping_constraint = iteration_cap` |
| I5 | No-progress detector fires when error fingerprint repeats | Loop stops on iteration N; `stopping_constraint = no_progress` |
| I6 | Cross-domain message from subscribed topic is processed | Correct handler invoked; `project_id` preserved in response |

### 9.3 Static Analysis Gate (Code-Producing Agents)

Any agent that autonomously writes Python (Write-Test-Refine loop) must demonstrate:

| # | Test | Pass Condition |
|---|------|----------------|
| S1 | Agent-generated code containing `os.system(...)` is blocked before deploy | `BLOCKED_STATIC` status on proposal row |
| S2 | Agent-generated code containing `import requests` is blocked | `Unapproved import: requests` in security log |
| S3 | Agent-generated code using only allowlisted imports passes Gate 2 | Proceeds to Gate 3 (Vertex deploy) |
| S4 | Manually edited col H after submission is caught at deploy | `CODE_HASH_MISMATCH`, Priority-5 alert published |

---

## 10. Reference Index

| Topic | Location |
|-------|----------|
| 3-tier agent hierarchy | `GAOS-Manager-Spec.md` §1.1 |
| Domain orchestrator definitions (Ledger, Beacon, etc.) | `GAOS-Manager-Spec.md` §1.2 |
| Event-driven Approval Gate | `GAOS-Manager-Spec.md` §3.B |
| Model version aliases + versioning policy | `GAOS-Manager-Spec.md` §11, §11.1 |
| A2A message schema + Pub/Sub topic topology | `GAOS-Manager-Spec.md` §10 |
| Self-evolution loop constraints | `GAOS-Manager-Spec.md` §13.1 |
| Evolution task logging schema | `GAOS-Manager-Spec.md` §13.2 |
| Approval Gate architecture + `Agent_Approvals` columns | `GAOS-Manager-Spec.md` §14 |
| Agent identity file template | `GAOS-Manager-Spec.md` §18.2 |
| Code injection prevention (gates, blocklist, allowlist) | `GAOS-Manager-Spec.md` §15.4 |
| Secrets management | `GAOS-Manager-Spec.md` §15.1 |
| Ollama / Local model fallback | `GAOS-Manager-Spec.md` §5 |
| Cost estimates | `GAOS-Manager-Spec.md` §9.4 |
| Data retention + BigQuery archive | `GAOS-Manager-Spec.md` §9.5 |
| Multi-project namespace (`project_id`) | `GAOS-Manager-Spec.md` §2 (Project Registry) |
| Nexus-Prime construction requirements | `GAOS-Nexus-Prime-Spec.md` |
| Agent behavioral identity + `think` node spec | `GAOS-Persona-Spec.md` |
| Infrastructure provisioning | `GAOS-Deploy-Spec.md` |
| Importing external skills — review process | `GAOS-Skill-Compliance-Spec.md` |
