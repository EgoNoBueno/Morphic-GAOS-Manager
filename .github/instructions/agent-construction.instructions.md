---
description: "Use when editing agent orchestrators or sub-agent task files. Enforces GAOS-Agent-Spec.md construction requirements: tier rules, Pydantic schemas, cost tracking, and identity files."
applyTo: "agents/{beacon,foreman,ledger,pursuit,scout,steward}/**/*.py"
---
# Agent Construction Rules

Rules specific to files under `agents/`. General coding rules (model aliases,
`project_id`, secrets, `_log_cloud`, exception handling) are in
`copilot-instructions.md` and apply everywhere — they are not repeated here.

## Cost Tracking — Every `_call_model()` Must Accumulate

Every `_call_model()` return **must** be followed by cost accumulation:

```python
resp = _call_model(prompt, model=model)
state["cost_usd"] += resp.cost_usd  # REQUIRED — do not skip
```

If the call is inside a `try` block, accumulate **before** any `except` can
bypass it:

```python
try:
    resp = _call_model(prompt, model=model)
    state["cost_usd"] += resp.cost_usd  # ← here, not after the try/except
    result = process(resp)
except SomeError:
    ...
```

`AgentOutput` must include the final `cost_usd`. If it returns `0.0` on a path
that made model calls, that is a bug.

## Pydantic Schema Boundaries

Agent `run()` methods must accept typed `AgentInput` (or a subclass) and return
typed `AgentOutput` (or a subclass). Forbidden at these boundaries:
- `agent_input: Any` → use the typed input class
- `result: dict` → define a typed result model
- `context: dict` → define a typed context model

## Tier-Specific Rules

**Tier 2** (orchestrator files in `agents/<name>/orchestrator.py`):
- LangGraph graph must include all 7 required nodes: `plan`, `dispatch`,
  `collect`, `report`, `park`, `resume`, `escalate`
- All 7 nodes must be **reachable** via edges — unreachable nodes are dead code
- `park` node must never busy-wait; use Pub/Sub resume
- Dashboard heartbeat write at end of every work cycle

**Tier 3** (task files in `agents/<name>/tasks/*.py`):
- Stateless — no LangGraph, no parked tasks
- All errors caught and returned as `AgentOutput(status="escalated"|"failed")`
- `LOCAL_MODEL` by default — `FAST_MODEL` is allowed only when `LOCAL_MODEL` is
  unavailable (fallback) or the task explicitly requires web-current knowledge
  (e.g., search result parsing). See GAOS-Agent-Spec.md §4.4 for the two permitted
  conditions. `DEEP_MODEL` is never permitted in Tier 3.
- No direct Sheet writes — return data to orchestrator

## Identity File

Every agent under `agents/` must have a corresponding `Docs/agents/<name>.md`
before deploy. If the identity file is missing, flag it.
