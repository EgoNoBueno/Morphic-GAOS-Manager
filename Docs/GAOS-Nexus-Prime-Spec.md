# GAOS Nexus-Prime Construction Specification

**Agent Identity:** `nexus-prime`
**Tier:** 1 — Root Orchestrator
**Model:** `DEEP_MODEL` for all decision nodes; `LOCAL_MODEL` for status aggregation and heartbeat formatting
**Framework:** Google ADK + LangGraph `StateGraph`
**Pub/Sub:** Subscribes to ALL 7 domain topics + `agent.approvals.events`

> This document specifies the engineering construction requirements for Nexus-Prime. It is companion to `GAOS-Agent-Spec.md` (common patterns shared by all agents) and supplements the behavioral rules in `GAOS-Manager-Spec.md §1`. Read both before building.

---

## 1. Role and Scope

Nexus-Prime is the single Tier 1 root agent. It receives every message that any Tier 2 orchestrator publishes, manages the Approval Gate, initializes new project namespaces, resolves cross-domain conflicts, and maintains system integrity.

**Nexus-Prime is NOT a domain worker.** It does not perform accounting, sales, or marketing tasks. It does not answer user questions directly. Its job is to oversee the entire hierarchy and ensure that every action taken by any agent is bounded, approved (when required), and recorded.

### 1.1 Tier Obligations (from `GAOS-Agent-Spec.md §1`)

| Capability | Nexus-Prime |
|------------|-------------|
| Base framework | LangGraph `StateGraph` |
| Pub/Sub publish | `agent.nexus-prime.events` |
| Pub/Sub subscribe | ALL 8 topics |
| Sheet access | Global write; all tabs |
| Drive access | Read any Knowledge file; write any file post-approval |
| Memory Bank | Read any corpus; write any corpus (post-approval only) |
| Code Execution sandbox | Full access (sandboxed) |
| Approval Gate | Sole submitter of proposals to Gate; sole interpreter of human `Approved` signals |
| Model restriction | `DEEP_MODEL` for all decisions (no LOCAL_MODEL shortcuts for approvals or conflict resolution) |

### 1.2 Core Rules (from `GAOS-Manager-Spec.md §1`)

These rules are not guidelines — they are hard constraints the implementation must enforce in code:

1. **Diagnose before escalating.** When an orchestrator reports a failure, Nexus-Prime runs at least one diagnostic cycle (inspect error log, check project context, re-read Memory Bank) before writing an `Agent_Approvals` proposal.
2. **Oversee Write-Test-Refine.** Every code evolution task must pass the `validate_code_safety()` gate before it is even sent to the Approval Gate. Nexus-Prime is responsible for this gate even when the task originated in a domain orchestrator.
3. **Approval Gate is required for ALL code deployment.** No exception. Not even in Phase 1 testing. The `get_approval()` blocking call must be invoked and must return `Approved` before `syncSkillsToVertex()` is triggered.
4. **Project ID scoping.** Every action Nexus-Prime takes must carry a `project_id`. All Sheet writes, BigQuery inserts, Memory Bank reads/writes, and Pub/Sub messages must include `project_id`.
5. **Project initialization.** When a new row appears in `Project Registry` with `status = Pending`, Nexus-Prime must be the one to generate the Sheet workbook, Drive folder, and Pub/Sub topic prefix for that project, then set `status = Active`.

---

## 2. State Schema

Nexus-Prime's working memory extends the common `AgentWorkingMemory` TypedDict from `GAOS-Agent-Spec.md §5` with additional fields required by its broader scope.

```python
from typing import Any, Optional
from models import A2AMessage, ApprovalProposal

class NexusPrimeWorkingMemory(AgentWorkingMemory, total=False):
    # Inherited from AgentWorkingMemory:
    #   project_id: str
    #   task_id: str
    #   step_count: int
    #   tokens_used: int
    #   cost_usd: float
    #   incoming_message: Optional[A2AMessage]
    #   messages: list
    #   hard_stop_triggered: bool
    #   evolution_triggered: bool

    # Cross-domain synthesis context
    active_broadcasts: list[A2AMessage]     # Messages awaiting BROADCAST resolution
    conflict_queue: list[dict]              # Conflicting agent state pairs pending arbitration
    # Note: parked_proposals (list[str]) is inherited from AgentWorkingMemory.
    #       It stores proposal IDs only — not full row dicts.

    # Project initialization state (ephemeral per task)
    pending_project_row: Optional[dict]
    new_project_id: Optional[str]

    # Evolution gate state
    candidate_code: Optional[str]
    candidate_agent_id: Optional[str]
    candidate_sha256: Optional[str]
    safety_check_passed: bool

    # System-wide health tracking
    system_state_summary: dict              # Populated from heartbeats
    last_ttl_sweep_at: Optional[str]        # ISO timestamp of last TTL sweep

    # Vision workflow (Phase 2.5 Step 5)
    active_blueprints: dict                 # Maps blueprint_id → doc_id
    blueprint_constraints: list[dict]       # Active constraint stack across all blueprints
```

---

## 3. LangGraph Graph

### 3.1 Node Inventory

Nexus-Prime's `StateGraph` has 21 nodes. The router node determines which branch to execute based on message type. Added since original design: `market_watchdog`, `roi_optimizer` (Phase 3 reactive routing), `handle_infra_provision` (Phase 4 InfraProvisioner), `handle_approval_request` (Phase 4 — routes inbound `APPROVAL_REQUEST` messages from domain agents to a Chat card notification), `chat_respond` (conversational DM reply node — routes here from `think()` when `msg_type == CHAT_MESSAGE`).

```
┌────────────────────────────────────────────────────────────────┐
│                       Nexus-Prime Graph                        │
│                                                                │
│  [boot] ──► [monitor] ──► [route]                              │
│                               │                                │
│                           [think]                              │
│                               │                                │
│    ┌───────┬───────┬───────┬───────┬───────┬───────┐   │
│    │       │       │       │       │       │       │   │
│ [diag] [know] [init] [conf] [park] [vision] [iter]  │
│    │       │       │       │       │       │       │   │
│ [prop] [prom] [notif] ───────────────────────┘   │
│    │       │       │                                   │
│    └───────┴───────┘                                   │
│                    │                                   │
│               [record] ──► END                           │
└────────────────────────────────────────────────────────────────┘
```

Node abbreviations: diag=diagnose, know=knowledge\_review, init=init\_project, conf=conflict\_resolve, park=park\_or\_broadcast, vision=vision\_blueprint, iter=iterate\_plan, skill=handle\_skill\_request, prop=propose\_gate, prom=promote, notif=notify\_agents, think=think, chat=chat\_respond

### 3.2 Node Definitions

#### `boot`

Runs once at service startup. Initializes all subscriptions, loads parked proposals from `Agent_Approvals`, and validates service account credentials.

```python
def boot(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    from config import get_settings
    from tools.google_sheets import get_all_records
    from tools.pubsub import ensure_topic_exists

    settings = get_settings()
    pid = state.get("project_id", settings.GCP_PROJECT_ID)
    state["project_id"] = pid

    # Ensure all Pub/Sub topics exist (idempotent)
    for topic in settings.pubsub.all_topics:
        ensure_topic_exists(topic)

    # Load parked proposals — store IDs only (consistent with list[str])
    all_proposals = get_all_records("Agent_Approvals", pid)
    state["parked_proposals"] = [
        r["ID"] for r in all_proposals
        if r.get("Status") in ("Pending", "Needs Revision") and r.get("ID")
    ]

    # Load system state summary (all active projects from Project Registry)
    registry = get_all_records("Project Registry", pid)
    state["system_state_summary"] = {
        r["project_id"]: r["status"]
        for r in registry if r.get("project_id")
    }

    return state
```

#### `monitor`

Listens for incoming Pub/Sub push messages. Decodes the `A2AMessage` envelope and loads it into working memory. This is the only node that reads from external HTTP input.

```python
def monitor(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    from tools.pubsub import decode_push_message
    # The raw Pub/Sub push envelope is injected into state["_raw_incoming"]
    # by the HTTP entry point before the graph starts.
    raw = state.get("_raw_incoming")
    if not raw:
        state["step_count"] = state.get("step_count", 0) + 1
        return state  # TTL sweep or internal trigger — no incoming message
    msg = decode_push_message(raw)
    state["incoming_message"] = msg
    state["project_id"] = msg.project_id
    state["task_id"] = msg.task_id or str(uuid.uuid4())
    state["step_count"] = state.get("step_count", 0) + 1
    return state
```

#### `route`

Examines `incoming_message.message_type` and returns the next node name. This is a pure routing function — it makes no LLM calls and writes no data.

```python
def route(state: NexusPrimeWorkingMemory) -> str:
    msg = state["incoming_message"]
    if msg is None:
        return "record"   # TTL sweep or heartbeat with no action needed

    # Keys are MessageType enum members, not raw strings.
    routing_table = {
        MessageType.STATUS_UPDATE:       "record",           # Log and store; no action
        MessageType.TASK_COMPLETE:       "record",           # Log and store; no action
        MessageType.ESCALATION:          "think",            # think → diagnose
        MessageType.EVOLUTION_REQUEST:   "think",            # think → diagnose
        MessageType.APPROVAL_RESULT:     "_route_approval",  # Human responded to a proposal
        MessageType.KNOWLEDGE_CANDIDATE: "think",            # think → knowledge_review
        MessageType.BROADCAST:           "conflict_resolve", # Cross-domain state conflict
        MessageType.NEW_PROJECT:         "init_project",     # Project Registry change detected
        MessageType.VISION_SUBMITTED:    "vision_blueprint", # Owner vision → Blueprint Doc
        MessageType.PLAN_REVIEW:         "iterate_plan",     # Owner comment on Blueprint
        MessageType.COMMENT_RECEIVED:    "iterate_plan",     # Doc comment poll found new comment
        MessageType.SKILL_REQUEST:       "handle_skill_request",  # Agent requests package install approval
        # ── Phase 3 — Reactive cross-domain routing ────────────────────────────
        MessageType.STOCK_INSUFFICIENT:  "market_watchdog",       # Foreman stockout → dispatch Scout
        MessageType.DEAL_CLOSED:         "roi_optimizer",         # Pursuit deal closed → check margin → dispatch Beacon
        # ── Phase 4 — Approval Gate inbound notifications ──────────────────────
        MessageType.APPROVAL_REQUEST:    "handle_approval_request",  # Domain agent _park() → send Chat card
    }
    return routing_table.get(msg.message_type, "record")

# Approval sub-router — called via add_conditional_edges when APPROVAL_RESULT arrives
def _route_approval(state: NexusPrimeWorkingMemory) -> str:
    msg = state.get("incoming_message")
    status = msg.payload.get("status", "") if (msg and msg.payload) else ""
    if status == "Approved":
        return "promote"
    if status == "Rejected":
        return "record"   # Log rejection; no further action
    return "park_or_broadcast"
```

#### `think`

**Nexus-Prime only (Tier 1).** Mandatory pre-response reasoning node that runs after `route` and before every output-producing node (`diagnose`, `knowledge_review`). Uses `FAST_MODEL` with the Context Trio as the system prompt.

**Tactical mode trigger:** `incoming_message.priority >= 4`. This overrides any model-selected mode.

**Wiring:** `route` returns `"think"` for `ESCALATION`, `EVOLUTION_REQUEST`, and `KNOWLEDGE_CANDIDATE`. `think` stores `state["_next_node"]` and a `_route_from_think()` sub-router reads it via `add_conditional_edges`.

```python
def think(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    from config import get_settings
    from tools.bigquery import insert_row

    settings = get_settings()
    msg = state.get("incoming_message")
    project_id = state["project_id"]
    task_id = state.get("task_id", "")

    # Map message type → downstream node
    msg_type = msg.message_type if msg else None
    if msg_type in (MessageType.ESCALATION, MessageType.EVOLUTION_REQUEST):
        next_node = "diagnose"
    elif msg_type == MessageType.KNOWLEDGE_CANDIDATE:
        next_node = "knowledge_review"
    else:
        next_node = "record"
    state["_next_node"] = next_node

    priority = _compute_priority(state)

    # Tactical fast-path — skip the LLM call when priority >= 4; result is always "Tactical"
    if priority >= 4:
        data = {
            "response_mode": "Tactical",
            "knowledge_gap_detected": False,
            "knowledge_gap_description": "",
            "partial_result_available": False,
            "reasoning_summary": "High-priority message; Tactical mode applied without model call.",
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
            _log_cloud("nexus-prime", project_id, "task", task_id,
                       f"think: _call_model failed — {exc}", "WARNING")
            return state   # Fallback: skip MonologueFrame, don't block pipeline

        data = resp.data or {}
        data.setdefault("reasoning_summary", resp.text[:500])

        # Validate and normalise response_mode
        _VALID_MODES = {"Research", "Direct", "Reframe", "Tactical"}
        if data.get("response_mode") not in _VALID_MODES:
            data["response_mode"] = "Research" if data.get("knowledge_gap_detected") else "Direct"

    frame = MonologueFrame(
        task_id=task_id,
        project_id=project_id,
        knowledge_gap_detected=bool(data.get("knowledge_gap_detected", False)),
        knowledge_gap_description=str(data.get("knowledge_gap_description", "")),
        partial_result_available=bool(data.get("partial_result_available", False)),
        response_mode=data["response_mode"],
        reasoning_summary=str(data.get("reasoning_summary", resp.text[:500])),
        timestamp=utcnow_iso(),
    )
    state["monologue_frame"] = frame.model_dump()

    try:
        insert_row("aos_logs.monologue_frames", frame.model_dump(), project_id)
    except Exception as exc:
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   f"think: BQ insert failed — {exc}", "WARNING")

    return state
```

**MonologueFrame schema** (see `models/__init__.py`):

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | `str` | Links to the active task |
| `project_id` | `str` | Active project namespace |
| `knowledge_gap_detected` | `bool` | True if the model lacks context to act confidently |
| `knowledge_gap_description` | `str` | What specifically is missing |
| `partial_result_available` | `bool` | True if a partial answer can still be returned |
| `response_mode` | `Literal["Direct", "Reframe", "Research", "Tactical"]` | Strategic Architect mode |
| `reasoning_summary` | `str` | One-sentence rationale |
| `timestamp` | `str` | ISO 8601 from `utcnow_iso()` |

> ⚠️ **Warning — think node is Tier 1 only:** Do not add a `think` node to Tier 2 orchestrators. Their output patterns are audited periodically by Nexus-Prime's Weekly Review Loop instead. *(Weekly Review Loop specification to be added in a future phase.)*

##### Think node helpers

Three private helpers are called from `think()`. All three are defined in `agents/nexus_prime/orchestrator.py` except `_load_context_trio`, which is defined in `agents/__init__.py` and imported.

---

**`_compute_priority`**

```python
def _compute_priority(state: NexusPrimeWorkingMemory) -> int:
```

| | |
|---|---|
| **Input** | `state: NexusPrimeWorkingMemory` — the active working memory TypedDict |
| **Returns** | `int` in range `1`–`5` matching the `A2AMessage.priority` field constraint |
| **Default** | `3` ("P3 Alert") when `incoming_message` is `None` or the `priority` attribute is absent |

**Algorithm:**
1. Read `state.get("incoming_message")`.
2. If `None`, return `3`.
3. Return `getattr(msg, "priority", 3)` — the `priority` field on `A2AMessage` is validated `ge=1, le=5` at construction time, so the fallback only fires for malformed/partial messages outside the normal path.

**Edge cases:**

| Condition | Behaviour |
|--|--|
| No active message (TTL sweep, heartbeat) | Returns `3` |
| Message constructed without `priority` (test stubs, partial payloads) | Returns `3` via `getattr` fallback |
| `priority >= 4` | `think()` skips the LLM call and sets `response_mode = "Tactical"` |

**Side effects:** None — pure attribute read.

---

**`_load_context_trio`**

```python
def _load_context_trio() -> str:
```

Defined in `agents/__init__.py`. Reads the three Context Trio files from `Docs/` and returns them as a single concatenated string used as the `system_prompt` argument to `_call_model()` inside `think()`.

| | |
|---|---|
| **Inputs** | None |
| **Returns** | `str` — Markdown sections joined by `"\n\n---\n\n"`; empty string `""` if all files are missing |

**Files loaded (in order):**

| File | Role |
|--|--|
| `Docs/about-me.md` | The Compass — owner business context |
| `Docs/brand-voice.md` | The Persona — communication tone and standards |
| `Docs/working-preferences.md` | The Constitution — operational rules and preferences |

**Failure behaviour:** Each file is read independently. A missing file is silently skipped — no exception is raised. If all three are absent (e.g., unit tests running without a full repo mount), the function returns `""`, which `_call_model()` receives as an empty system prompt. The `think()` node continues normally.

**Side effects:** None — pure file reads; no writes, no caching, no GCP calls.

---

**`_build_think_prompt`**

```python
def _build_think_prompt(
    state: NexusPrimeWorkingMemory,
    msg: A2AMessage | None,
    priority: int,
) -> str:
```

| | |
|---|---|
| **Inputs** | `state` — active working memory (reserved for future use; no fields read currently); `msg` — the `A2AMessage` being processed, or `None`; `priority` — integer from `_compute_priority()` |
| **Returns** | `str` — a complete prompt string ready to pass as the first positional argument to `_call_model()` |

**Extraction rules:**

| Field | Source | Fallback when `msg` is `None` |
|--|--|--|
| Message type | `msg.message_type.value` (e.g., `"ESCALATION"`) | `"NONE"` |
| Source agent | `msg.source_agent` | `"unknown"` |
| Payload | `str(msg.payload or {})[:300]` — trimmed to 300 chars | `""` |

**Prompt structure:** The prompt instructs `FAST_MODEL` to answer four questions in a single JSON response:
1. Is there a knowledge gap preventing confident action? If so, what specifically is missing?
2. Is a partial result available despite the gap?
3. Which response mode applies (`"Direct"`, `"Reframe"`, `"Research"`, or `"Tactical"`)?
4. One-sentence reasoning summary.

**Expected JSON response schema:**

| Key | Type | Constraint |
|--|--|--|
| `knowledge_gap_detected` | `bool` | Required |
| `knowledge_gap_description` | `str` | Empty string if no gap |
| `partial_result_available` | `bool` | Required |
| `response_mode` | `str` | One of `"Direct"`, `"Reframe"`, `"Research"`, `"Tactical"` |
| `reasoning_summary` | `str` | One sentence |

`think()` validates `response_mode` against `_VALID_MODES` after the model call and falls back to `"Research"` (gap detected) or `"Direct"` (no gap) if the model returns an unrecognised value.

**Side effects:** None — pure string construction with no I/O.

**Edge case:** When `msg` is `None`, all extracted fields default to safe string literals and the prompt remains syntactically valid. `think()` will still set `_next_node = "record"` in this path (no active message → no ESCALATION/KNOWLEDGE_CANDIDATE routing).

---

#### `diagnose`

Runs when an orchestrator has escalated or requested evolution. Uses `DEEP_MODEL` to analyze the error fingerprint, search Memory Bank for similar past failures, and determine if self-repair is possible or if a human approval proposal is needed.

```python
def diagnose(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    from tools.google_sheets import find_rows
    from tools.memory import query_memory_bank

    msg = state.get("incoming_message")
    if msg is None:
        return state
    error_fp = msg.payload.get("error_fingerprint", "") if msg.payload else ""

    # Search Memory Bank for matching past failures
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

    # Determine next action using DEEP_MODEL
    prompt = _build_diagnosis_prompt(msg, similar, recent_errors)
    resp = _call_model(prompt, model=_model_for_node("diagnose"), parse_json=True)
    state["cost_usd"] = state.get("cost_usd", 0.0) + resp.cost_usd
    state["messages"].append({"role": "assistant", "content": resp.text})

    if resp.data.get("suggests_code_change"):
        # Run the Write-Test-Refine evolution loop inline
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

    return state
```

#### `propose_gate`

Only called after `diagnose` indicates code evolution is needed AND `validate_code_safety()` passes. Writes a proposal row to `Agent_Approvals` and registers it in `parked_proposals`.

```python
def propose_gate(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    from tools.google_sheets import append_row
    from tools.webhook_sender import post_to_webhook
    import hashlib, uuid

    candidate_code: str = state.get("candidate_code") or ""
    if not candidate_code:
        return state  # Nothing to propose; called without evolution output

    # Safety gate — hard stop if blocked by allowed_imports or blocklist
    safety = validate_code_safety(candidate_code)
    if not safety["passed"]:
        state["hard_stop_triggered"] = True
        _log_hard_stop(state, f"BLOCKED_STATIC: {safety['reason']}")
        return state

    sha256 = hashlib.sha256(candidate_code.encode()).hexdigest()
    proposal_id = str(uuid.uuid4())

    msg = state.get("incoming_message")
    payload = msg.payload if (msg and msg.payload) else {}
    agent_id_str: str = state.get("candidate_agent_id") or ""

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

    append_row("Agent_Approvals", row, state["project_id"])
    # Store proposal ID only (consistent with list[str])
    state["parked_proposals"].append(proposal_id)
    state["candidate_sha256"] = sha256

    post_to_webhook(row, state["project_id"])

    return state
```

#### `knowledge_review`

Evaluates a `KNOWLEDGE_CANDIDATE` message from a domain orchestrator. Uses `DEEP_MODEL` to check confidence, freshness, and uniqueness against the Memory Bank. On successful auto-promotion, mirrors the entry to the Knowledge Atlas Google Doc via `tools.memory_mirror.sync_to_atlas()`.

The LLM prompt (`_build_knowledge_review_prompt`) now explicitly asks whether the candidate **supersedes** an existing memory entry, returning a `supersedes_memory_id` field and a `supersession_reason` field in addition to `confidence` and `is_duplicate`. The `MemoryEntry` constructor sets `approved_at=datetime.now(UTC)` and `supersedes` from that field so both are always populated on auto-promoted entries. When `supersedes` is set, a structured `SUPERSESSION_AUDIT` log is written at `INFO` level before the Atlas mirror call, recording which `memory_id` was retired and the reason returned by the model.

```python
def knowledge_review(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
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
        # Auto-promote: write directly to Memory Bank, then mirror to Atlas
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
                approved_at=datetime.now(UTC),                          # always set
                supersedes=resp.data.get("supersedes_memory_id") or None,
                tags=candidate.get("tags", []),
            )
            write_approved_memory(entry=entry, project_id=state["project_id"])
            if entry.supersedes:
                supersession_reason = resp.data.get("supersession_reason") or "(no reason provided)"
                _log_cloud(
                    "nexus-prime", state["project_id"], "task",
                    state.get("task_id", ""),
                    f"SUPERSESSION_AUDIT: memory_id={entry.supersedes} retired by "
                    f"new entry (domain={entry.domain}). Reason: {supersession_reason}",
                    "INFO",
                )
            # Mirror to Knowledge Atlas — non-blocking; WARNING logged on failure
            try:
                from tools.memory_mirror import MemoryMirrorError, sync_to_atlas
                sync_to_atlas(entry, supersession_reason=supersession_reason)
            except MemoryMirrorError as mirror_exc:
                _log_cloud(
                    "nexus-prime", state["project_id"], "task",
                    state.get("task_id", ""),
                    f"knowledge_review: Atlas sync failed (non-fatal): {mirror_exc}",
                    "WARNING",
                )
        except Exception:
            _write_to_pending_knowledge(state, candidate, resp)
    else:
        # Uncertain — send to Pending_Knowledge for human review
        _write_to_pending_knowledge(state, candidate, resp)

    return state
```

**Prompt schema returned by `_build_knowledge_review_prompt`:**

```json
{
  "confidence": 0.92,
  "is_duplicate": false,
  "rationale": "New observation with no contradicting Memory Bank entry.",
  "supersedes_memory_id": null,
  "supersession_reason": null
}
```

`supersedes_memory_id` is the `memory_id` of an existing entry that this candidate refines or replaces (`null` if the candidate is purely additive). `supersession_reason` is a one-sentence explanation of why the old entry is being retired (e.g. `"Updated vendor terms override the 2024 policy"`). When `supersedes_memory_id` is set, `write_approved_memory` marks the old entry `active=False` and a `SUPERSESSION_AUDIT` event is logged at `INFO` before the Atlas mirror call. `sync_to_atlas` then appends a `⛔ SUPERSEDED` audit line to the Knowledge Atlas.

#### `promote`

Only called when `APPROVAL_RESULT` status is `Approved`. Verifies the SHA-256 hash matches the `Agent_Approvals` sheet, then calls `syncSkillsToVertex()` via the Apps Script webhook, and updates the proposal status to `Deployed`. Drive archiving is handled downstream by Apps Script — this node does not write to Memory Bank or Drive directly.

```python
def promote(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    from tools.google_sheets import find_row, update_row

    msg = state.get("incoming_message")
    if msg is None:
        return state
    proposal_id = msg.payload.get("proposal_id", "") if msg.payload else ""
    if not proposal_id:
        return state

    # Find the proposal row
    row = find_row("Agent_Approvals", "ID", proposal_id, state["project_id"])
    if row is None:
        _log_cloud("nexus-prime", state["project_id"], "security",
                   state.get("task_id", ""), f"promote: proposal {proposal_id} not found", "ERROR")
        return state

    # Hash must match — reject if tampered
    live_sha = hashlib.sha256((row.get("Proposed Code") or "").encode()).hexdigest()
    stored_sha = row.get("code_sha256", "")
    if live_sha != stored_sha:
        _log_cloud(
            "nexus-prime", state["project_id"], "security",
            state.get("task_id", ""),
            f"CODE_HASH_MISMATCH proposal={proposal_id}",
            "CRITICAL",
        )
        update_row("Agent_Approvals", proposal_id, {"Status": "Needs Revision"}, state["project_id"])
        return state

    # Trigger deployment via Apps Script syncSkillsToVertex
    _trigger_sync_to_vertex(row, state)

    # Update proposal status to Deployed
    update_row("Agent_Approvals", proposal_id, {"Status": "Deployed"}, state["project_id"])

    # Remove proposal ID from parked list (list[str])
    state["parked_proposals"] = [
        p for p in state.get("parked_proposals", []) if p != proposal_id
    ]

    return state
```

#### `init_project`

Called when a `NEW_PROJECT` message is received (or when `boot` detects a `Pending` row in `Project Registry`). Provisions all infrastructure for the new project namespace.

```python
def init_project(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    from tools.google_sheets import get_all_records, update_row

    registry = get_all_records("Project Registry", state["project_id"])
    pending_rows = [r for r in registry if r.get("status") == "Pending"]
    if not pending_rows:
        return state

    # Process one pending row per invocation to bound latency
    row = pending_rows[0]
    new_pid = row.get("project_id", "")
    if not new_pid:
        return state

    # 1. Clone the master Sheet workbook for this project namespace
    new_sheet_id = _create_sheet_workbook(new_pid)
    # 2. Create Knowledge/ subfolder in Drive
    new_folder_id = _create_drive_folder(new_pid)
    # 3. Pub/Sub topics are shared (project_id scoped in payload); no new topics needed
    # 4. Update Project Registry row to Active
    update_row("Project Registry", row.get("ID", new_pid), {
        "status": "Active",
        "sheet_workbook_id": new_sheet_id,
        "drive_folder_id": new_folder_id,
    }, state["project_id"])

    state["pending_project_row"] = {**row, "sheet_workbook_id": new_sheet_id, "drive_folder_id": new_folder_id}
    state["new_project_id"] = new_pid
    return state
```

#### `notify_agents`

Called after `init_project`. Publishes a `BROADCAST` message to all 7 domain orchestrator topics notifying them of the new project namespace.

```python
def notify_agents(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
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

    state.setdefault("active_broadcasts", []).append(broadcast)
    return state
```

#### `conflict_resolve`

Called when two orchestrators have published messages with contradictory state about the same entity (same `project_id` + same entity key, different values). Uses `DEEP_MODEL` to arbitrate and publishes a `BROADCAST` resolution.

```python
def conflict_resolve(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
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

        state.setdefault("active_broadcasts", []).append(broadcast)

    state["conflict_queue"] = []
    return state
```

#### `park_or_broadcast`

Called when an approval result is neither `Approved` nor `Rejected` (e.g., `Needs Revision`), or when a proposal cannot yet proceed. Updates the `Agent_Approvals` row status and keeps the proposal in `parked_proposals`.

```python
def park_or_broadcast(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
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
```

#### `record`

Final node. All paths terminate here. Writes to BigQuery `task_outcomes`, appends to `Error Logs` if hard stop was triggered, and publishes `STATUS_UPDATE` to `agent.nexus-prime.events`. Uses `LOCAL_MODEL` for formatting the summary text (cost optimization).

```python
def record(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    from tools.bigquery import insert_row
    from tools.pubsub import publish

    msg = state.get("incoming_message")
    outcome: dict = {
        "task_id": state.get("task_id", ""),
        "project_id": state.get("project_id", ""),
        "agent_id": "nexus-prime",
        "task_type": msg.message_type.value if msg else "TTL_SWEEP",
        "status": "hard_stop" if state.get("hard_stop_triggered") else "success",
        "error_fingerprint": (msg.payload or {}).get("error_fingerprint", "") if msg else "",
        "cost_usd": state.get("cost_usd", 0.0),
        "duration_seconds": _elapsed_seconds(dict(state)),
        "timestamp": utcnow_iso(),
        "log_date": utcnow_date(),
    }
    try:
        insert_row("aos_logs.task_outcomes", outcome)
    except Exception:
        pass

    # Publish heartbeat
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
```

#### `chat_respond`

Conversational response node. Routes here directly from `think()` when `msg_type == CHAT_MESSAGE`. `think()` itself fast-paths CHAT_MESSAGE (no LLM call — sets `response_mode="Direct"` and `next_node="chat_respond"` deterministically) so this is the only LLM call in the entire CHAT_MESSAGE path.

**What it does:** Loads the Context Trio as system prompt, appends Google Chat formatting rules (no Markdown), calls `_call_model()`, and delivers the reply via `send_reply_in_thread()` (preferred) or `send_threaded_reply()` (fallback for messages without a server-assigned thread name). All failures are caught and logged — the node never raises.

**Current model:** `LOCAL_MODEL` (Ollama). See §4.2 for the Ollama-first policy and re-enable checklist.

```python
def chat_respond(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    # Builds prompt from context_trio + chat_format_rules + user_text
    # Calls _call_model(prompt, model=settings.models.LOCAL_MODEL, system_prompt=...)
    # Sends reply via send_reply_in_thread / send_threaded_reply
```

**Failure behaviour:** If the model call fails, the node returns a fallback string (`"I'm having trouble processing your request right now."`) and logs the exception. If the Chat send fails, it is logged but does not propagate — `record` still runs.

> ⚠️ **Google Chat delivery abandoned — 2026-03-30:** After ~2 weeks of attempted integration, Google Chat was never able to deliver a single mobile message end-to-end. The node code is correct and remains in the graph (to serve future `CHAT_MESSAGE` sources), but the Google Chat webhook delivery path is not operational. Full failure history:
>
> 1. **Local 403 — missing `chat.bot` ADC scope:** `send_reply_in_thread()` raises `403 PERMISSION_DENIED` when run locally against real Chat. Expected — ADC does not hold the `https://www.googleapis.com/auth/chat.bot` scope. Workaround: deploy and test on Cloud Run only. Non-blocking but forced all integration testing to be remote.
>
> 2. **Cloud Run 500 — `CLOUD_RUN_URL` env var not set:** `_verify_chat_jwt()` validates the JWT `aud` claim against `CLOUD_RUN_URL`. The variable was not included in the initial deploy loop, causing every `/chat` POST to return `500`. Fixed manually via `gcloud run services update nexus-prime --set-env-vars "CLOUD_RUN_URL=https://nexus-prime-975461050387.us-central1.run.app,AGENT_NAME=nexus-prime"` → revision `00057`. The deploy spec has since been updated to include this step.
>
> 3. **Cloud Run 401 — `chat@system.gserviceaccount.com` missing from `roles/run.invoker`:** Google Chat delivers webhooks signed by the service account `chat@system.gserviceaccount.com`. This SA must be explicitly granted `roles/run.invoker` on the `nexus-prime` Cloud Run service — it is not covered by the `pubsub-push-sa` binding. Fixed via `gcloud run services add-iam-policy-binding nexus-prime --member="serviceAccount:chat@system.gserviceaccount.com" --role="roles/run.invoker"`.
>
> 4. **Google Chat retry exhaustion:** Google Chat attempts delivery 2–3 times within a short window (~30–60 seconds). If the service returns non-200 during those retries, Chat marks the delivery as permanently failed and never re-attempts — even after the underlying problem is fixed. The IAM fix (item 3) took ~2 minutes to propagate; Chat had already exhausted its retry budget by then. Subsequent messages also failed to deliver reliably with no clear error surfaced in the Chat UI.
>
> 5. **Stale Cloud Run image:** Prior to revision `00056`, Cloud Run was running a 6-day-old image (revision `00055-m7z`, deployed 2026-03-24). The local fix to raise `RuntimeError` on Ollama failure (disabling Gemini fallback) was not deployed, causing 49+ Gemini fallback invocations hitting 429 rate limits and silently failing every Chat response.
>
> 6. **Tunnel URL instability:** The `gaas-ollama.loca.lt` reserved subdomain is not guaranteed — loca.lt grants it on a best-effort basis. When the subdomain lapses, the tunnel runs on a random URL, `OLLAMA_HOST` in Secret Manager becomes stale, and Cloud Run hits a dead endpoint.
>
> **Outcome:** Google Chat is abandoned as the user-facing interface. The `chat_respond` node remains in the graph unchanged — it will serve `CHAT_MESSAGE` events from the Gmail polling path (see §X — Gmail integration). The `send_reply_in_thread()` / `send_threaded_reply()` calls will be replaced with `gmail_reply()` once that tool is implemented.

---

#### `vision_blueprint`

*(Phase 2.5 Step 5)* Handles `VISION_SUBMITTED` messages. Calls `DEEP_MODEL` to generate a structured Blueprint Doc from the owner's vision text, creates the Google Doc via `tools/google_docs.create_document()`, appends a row to the `Project_Incubator` Sheet tab, and sends an Approve/Reject Chat card to the owner's space (falling back to `settings.chat.owner_space` when `space_name` is absent from the payload). The `blueprint_id` (task_id) → `doc_id` mapping is stored in `active_blueprints`. All downstream failures (Docs API, Sheets, Chat) are caught and logged — the node never raises.

```python
def vision_blueprint(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    from tools.google_docs import create_document
    from tools.google_sheets import append_row

    msg = state.get("incoming_message")
    if msg is None:
        return state

    vision_text  = (msg.payload or {}).get("vision_text", "")
    blueprint_id = msg.task_id
    project_id   = state["project_id"]

    settings = get_settings()
    prompt = _build_vision_prompt(vision_text, project_id)
    resp = _call_model(prompt, model=settings.models.DEEP_MODEL, parse_json=False)
    state["cost_usd"] = state.get("cost_usd", 0.0) + resp.cost_usd
    blueprint_content = resp.text.strip()

    try:
        doc_id = create_document(
            title=f"Blueprint — {vision_text[:60]}",
            project_id=project_id,
            folder_id=settings.docs.blueprints_folder_id,
            initial_content=blueprint_content,
        )
        active: dict = state.get("active_blueprints") or {}
        active[blueprint_id] = doc_id
        state["active_blueprints"] = active
    except Exception as exc:
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   f"vision_blueprint: doc creation failed: {exc}", "ERROR")
        return state

    try:
        append_row("Project_Incubator",
                   {"id": blueprint_id, "vision_text": vision_text[:500],
                    "submitted_by": (msg.payload or {}).get("submitted_by", ""),
                    "doc_id": doc_id, "status": "Pending"},
                   project_id)
    except Exception as exc:
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   f"vision_blueprint: append_row failed: {exc}", "WARNING")

    try:
        from tools.google_chat import send_approval_card
        owner_space = (msg.payload or {}).get("space_name") or settings.chat.owner_space
        send_approval_card(
            space_name=owner_space,
            proposal_id=blueprint_id,
            agent_id="nexus-prime",
            issue_summary=f"Blueprint ready for review — doc_id: {doc_id}",
            proposed_action="Review the Blueprint Doc and approve or request changes.",
            priority=2,
            cost_usd=resp.cost_usd,
            reasoning_summary="",
        )
    except Exception as exc:
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   f"vision_blueprint: send_approval_card failed: {exc}", "WARNING")

    return state
```

#### `iterate_plan`

*(Phase 2.5 Step 5)* Handles `PLAN_REVIEW` and `COMMENT_RECEIVED` messages. Appends the new constraint to `blueprint_constraints`. When the count of constraints for the relevant blueprint reaches `_COMPACTION_THRESHOLD` (5), calls `_run_compaction()` which compresses them into one paragraph using `FAST_MODEL` and archives the originals to BigQuery `aos_logs.blueprint_constraints`; the list entry is then replaced with a single `"COMPACTED_CONSTRAINTS (from N reviewed comments)"` marker. Appends the new or compacted constraint text to the Blueprint Doc via `tools/google_docs.append_content()`.

```python
def iterate_plan(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    from tools.google_docs import append_content

    msg = state.get("incoming_message")
    if msg is None:
        return state

    blueprint_id = (msg.payload or {}).get("blueprint_id", msg.task_id)
    constraint   = (msg.payload or {}).get("constraint", "")
    project_id   = state["project_id"]

    state.setdefault("blueprint_constraints", []).append(
        {"blueprint_id": blueprint_id, "text": constraint}
    )

    relevant = [c for c in state["blueprint_constraints"]
                if c["blueprint_id"] == blueprint_id]

    if len(relevant) >= _COMPACTION_THRESHOLD:
        compacted = _run_compaction(state, blueprint_id, relevant)
        state["blueprint_constraints"] = [
            c for c in state["blueprint_constraints"]
            if c["blueprint_id"] != blueprint_id
        ]
        state["blueprint_constraints"].append(
            {"blueprint_id": blueprint_id,
             "text": f"COMPACTED_CONSTRAINTS (from {len(relevant)} reviewed comments): {compacted}"}
        )
        constraint = compacted

    doc_id = state.get("active_blueprints", {}).get(blueprint_id)
    if doc_id:
        try:
            append_content(doc_id, constraint, project_id)
        except Exception as exc:
            logger.warning("iterate_plan: append_content failed: %s", exc)

    return state
```

#### `handle_skill_request`

*(Phase 2.5)* Handles `SKILL_REQUEST` messages from domain orchestrators that encounter a `ModuleNotFoundError` on a library outside the import allowlist. Two sub-paths based on payload content:

**Inbound request** (`status` absent from payload): Posts a `send_skill_import_card()` Chat card to the owner's space, writes an audit row to `Agent_Approvals`, and parks the `proposal_id` in `state["parked_proposals"]`.

**Resolution** (`status: Approved | Rejected`): Updates the `Agent_Approvals` row, removes the `proposal_id` from `parked_proposals`. If Approved, re-publishes `SKILL_REQUEST` to the requesting agent so its Write-Test-Refine loop can resume. If Rejected, publishes `ALERT` to trigger a hard-stop on the requesting agent with reason `skill_request_rejected`.

```python
def handle_skill_request(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
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

    if status in ("Approved", "Rejected"):
        # Resolution path — update audit row and notify requesting agent
        proposal_id = payload.get("proposal_id", "")
        if proposal_id:
            try:
                update_row("Agent_Approvals", proposal_id,
                           {"Status": status, "Approved By": payload.get("approved_by", "")},
                           project_id)
            except Exception:
                pass
            state["parked_proposals"] = [
                p for p in state.get("parked_proposals", []) if p != proposal_id
            ]

        out_type = MessageType.SKILL_REQUEST if status == "Approved" else MessageType.ALERT
        try:
            publish(f"agent.{msg.source_agent}.events",
                    A2AMessage(source_agent="nexus-prime", target_agent=msg.source_agent,
                               project_id=project_id, task_id=task_id,
                               message_type=out_type, priority=3,
                               payload={**payload, "status": status}))
        except Exception:
            pass
    else:
        # Inbound request path — create audit row and send owner Chat card
        proposal_id = str(uuid.uuid4())
        row = {"ID": proposal_id, "Agent ID": msg.source_agent,
               "Issue": payload.get("package_name", "unknown"),
               "Status": "Pending", "Trigger Reason": "SKILL_REQUEST",
               "Timestamp": utcnow_iso()}
        try:
            append_row("Agent_Approvals", row, project_id)
            state["parked_proposals"].append(proposal_id)
            send_skill_import_card(
                space_name=get_settings().chat.owner_space,
                proposal_id=proposal_id,
                agent_id=msg.source_agent,
                package_name=payload.get("package_name", ""),
                reason=payload.get("reason", ""),
            )
        except Exception as exc:
            _log_cloud("nexus-prime", project_id, "task", task_id,
                       f"handle_skill_request error: {exc}", "ERROR")

    return state
```

#### `market_watchdog`

*(Phase 3)* Handles `STOCK_INSUFFICIENT` messages from Foreman. Forwards the full stockout payload to Scout as a `MessageType.ALERT` with `alert_type = "stock_insufficient"` and `priority = 4`. Scout's `_plan()` node matches on this alert type and front-queues a sourcing pivot ahead of all routine research tasks.

**Payload forwarded to Scout:** all fields from Foreman's original payload plus `alert_type = "stock_insufficient"`. Recommended fields from Foreman: `sku`, `quantity_on_hand`.

**No LLM call.** This is a pure event-routing node — it dispatches and returns immediately.

> ⚠️ **If publish fails:** The failure is logged at `ERROR` severity but does not re-raise. The Pub/Sub message that triggered this node will be acked regardless — Scout will not receive the dispatch. Monitor `market_watchdog: publish to Scout failed` in Cloud Logging if sourcings are missing.

---

#### `roi_optimizer`

*(Phase 3)* Handles `DEAL_CLOSED` messages from Pursuit. Computes gross margin as `(revenue - cogs) / revenue`. If margin is below `_LOW_MARGIN_THRESHOLD` (hardcoded `0.20` / 20%), publishes a `MessageType.ALERT` with `alert_type = "low_margin"` and `priority = 3` to Beacon's topic. Beacon's `_plan()` front-queues a `lead_source_roi_analysis` task.

**Expected payload from Pursuit:** `deal_id`, `lead_source`, `revenue`, `cogs`.

**Payload forwarded to Beacon:** all Pursuit fields plus `alert_type = "low_margin"`, `margin_pct` (rounded to 2dp), `threshold_pct` (20.0).

**Zero-revenue guard:** If `revenue == 0`, margin is treated as `0.0` (below threshold) — Beacon alert fires. No `ZeroDivisionError`.

**No LLM call.** Pure arithmetic dispatch node.

---

#### `handle_infra_provision`

*(Phase 4)* LangGraph node adapter for the APPLY phase of the [InfraProvisioner workflow](GAOS-Deploy-Spec.md#20-infrastructure-provisioner). Triggered by `INFRA_PROVISION_APPROVED` or `INFRA_PROVISION_REJECTED` messages published from the `/chat` CARD_CLICKED handler when the owner taps **Approve** or **Reject** on the infra proposal card.

**On APPROVED:** Reads the `InfraManifest` JSON from `Agent_Approvals.proposed_code` for the matching `proposal_id`. Calls `apply_manifest()` (secrets → BQ tables → Scheduler jobs in safe order). Runs `run_health_checks()` — if any check fails, calls `rollback_manifest()` and sends a failure result card to the owner's Chat space. On full success, sends a plain-language success card listing every applied change.

**On REJECTED:** Marks the proposal row status as `Rejected` and sends a brief rejection acknowledgement card.

**Imports used:** `tools.infra_provision.{apply_manifest, rollback_manifest, run_health_checks, InfraManifest}`, `tools.google_chat.send_infra_proposal_card`.

**No LLM call.** Pure imperative apply node.

> ⚠️ **Partial failure is non-fatal.** `apply_manifest()` processes each resource independently. A failed secret creation does not block BQ table creation. The node sends a result card listing both applied and failed items and only triggers rollback when `run_health_checks()` reports a failure.

---

#### `handle_approval_request`

*(Phase 4)* Handles `APPROVAL_REQUEST` messages published by any domain agent's `_park()` function to `agent/approvals/events`. The sending agent has already written the `Agent_Approvals` row and computed `code_sha256` — this node's sole responsibility is to notify the owner via Google Chat so they can approve or reject from the sheet.

**What it does:** Reads `proposal_id`, `agent_id`, and `description`/`issue` from `msg.payload`. Calls `send_approval_card()` to `settings.chat.owner_space`. Logs success or failure at `INFO`/`WARNING`. Does **not** re-write `Agent_Approvals`.

**If `owner_space` is empty:** Logs a `WARNING` and returns — no exception raised, graph continues to `record`.

**No LLM call.** Notification-only node.

> ⚠️ **Root cause fixed (2026-03-29):** Prior to this node, `APPROVAL_REQUEST` was absent from the `routing_table` in `route()` and fell through to `"record"` — the Chat card was never sent for any domain agent's approval request. The bug existed from the initial nexus-prime build. See WORKLOG `2026-03-29T` entry for full context.

---

### 3.3 Graph Assembly

```python
from langgraph.graph import StateGraph, END

def build_nexus_prime_graph() -> Any:
    graph = StateGraph(NexusPrimeWorkingMemory)

    graph.add_node("boot",                 boot)
    graph.add_node("monitor",              monitor)
    graph.add_node("route",                route)
    graph.add_node("think",                think)
    graph.add_node("diagnose",             diagnose)
    graph.add_node("propose_gate",         propose_gate)
    graph.add_node("knowledge_review",     knowledge_review)
    graph.add_node("promote",              promote)
    graph.add_node("init_project",         init_project)
    graph.add_node("notify_agents",        notify_agents)
    graph.add_node("conflict_resolve",     conflict_resolve)
    graph.add_node("park_or_broadcast",    park_or_broadcast)
    graph.add_node("record",               record)
    graph.add_node("vision_blueprint",     vision_blueprint)
    graph.add_node("iterate_plan",         iterate_plan)
    graph.add_node("handle_skill_request", handle_skill_request)
    graph.add_node("market_watchdog",      market_watchdog)   # Phase 3
    graph.add_node("roi_optimizer",        roi_optimizer)     # Phase 3
    graph.add_node("handle_infra_provision", _infra_provision_node)  # Phase 4
    graph.add_node("handle_approval_request", handle_approval_request)  # Phase 4

    graph.set_entry_point("boot")
    graph.add_edge("boot", "monitor")
    graph.add_edge("monitor", "route")

    # route() returns a node name string for each MessageType
    graph.add_conditional_edges(
        "route",
        route,
        {
            "think":                "think",
            "diagnose":             "diagnose",
            "knowledge_review":     "knowledge_review",
            "init_project":         "init_project",
            "conflict_resolve":     "conflict_resolve",
            "promote":              "promote",
            "park_or_broadcast":    "park_or_broadcast",
            "record":               "record",
            "vision_blueprint":     "vision_blueprint",
            "iterate_plan":         "iterate_plan",
            "handle_skill_request": "handle_skill_request",
            "market_watchdog":      "market_watchdog",    # Phase 3
            "roi_optimizer":        "roi_optimizer",      # Phase 3
            "handle_infra_provision": "handle_infra_provision",  # Phase 4
            "handle_approval_request": "handle_approval_request",  # Phase 4
        },
    )

    # think routes to diagnose, knowledge_review, or record based on message type
    graph.add_conditional_edges(
        "think",
        _route_from_think,
        {
            "diagnose":         "diagnose",
            "knowledge_review": "knowledge_review",
            "record":           "record",
        },
    )

    graph.add_edge("diagnose",             "propose_gate")
    graph.add_edge("propose_gate",         "record")
    graph.add_edge("knowledge_review",     "record")
    graph.add_edge("promote",              "record")
    graph.add_edge("init_project",         "notify_agents")
    graph.add_edge("notify_agents",        "record")
    graph.add_edge("conflict_resolve",     "record")
    graph.add_edge("park_or_broadcast",    "record")
    graph.add_edge("vision_blueprint",     "record")
    graph.add_edge("iterate_plan",         "record")
    graph.add_edge("handle_skill_request", "record")
    graph.add_edge("market_watchdog",       "record")   # Phase 3
    graph.add_edge("roi_optimizer",         "record")   # Phase 3
    graph.add_edge("handle_infra_provision", "record")  # Phase 4
    graph.add_edge("handle_approval_request", "record")  # Phase 4
    graph.add_edge("record",               END)

    return graph.compile(checkpointer=MemorySaver())
```

---

## 4. Hard Constraints and Guardrails

These are enforcement rules — not configurable behavior. Each must be verified in unit tests.

### 4.1 Things Nexus-Prime Cannot Do Unilaterally

| Action | Constraint | Enforcement |
|--------|-----------|-------------|
| Approve its own proposals | Never — must wait for human in `Authorized Approvers` list | `propose_gate` writes row; `promote` only fires on `APPROVAL_RESULT` from webhook, not from Nexus-Prime's own Pub/Sub messages |
| Modify domain orchestrator instruction files | Requires Tier 5 (owner) approval via Approval Gate | `candidate_agent_id` must match the agent being modified; owner-only approval for orchestrator identity files |
| Modify `Project Registry` status without provisioning | Must complete `init_project` sequence first (sheet, folder, topics) before setting `status = Active` | `init_project` node validates all three provisioning steps before calling `update_row` |
| Deploy code that fails the import allowlist | Hard stop — `validate_code_safety()` must return `passed = True` | `propose_gate` sets `hard_stop_triggered = True`, calls `_log_hard_stop`, and returns early — no row written to `Agent_Approvals` |
| Deploy code without SHA-256 match | Hard stop — `promote` verifies hash against both `code_sha256` field and live cell value | `promote` logs `CRITICAL` to Cloud Logging, sets proposal status back to `Needs Revision`, and returns without deploying |
| Perform actions without `project_id` | All tool calls require `project_id` parameter; tools raise `ValueError` if missing | Enforced in `tools/google_sheets.py`, `tools/pubsub.py`, `tools/bigquery.py` |

### 4.2 Model Selection Rules

**Current routing table (last updated 2026-03-30):**

| Node | Model alias | Rationale |
|------|-------------|----------|
| `diagnose`, `knowledge_review`, `conflict_resolve` | `DEEP_MODEL` | High-stakes decisions — full reasoning quality required |
| `record`, `chat_respond` | `LOCAL_MODEL` | Cost optimisation — formatting and conversational replies run on Ollama |
| `think`, `propose_gate`, `promote`, and all other nodes | `FAST_MODEL` | Lightweight routing / utility calls |

```python
DECISION_NODES = {"diagnose", "knowledge_review", "conflict_resolve"}
FORMAT_NODES   = {"record", "chat_respond"}  # Both use LOCAL_MODEL

def _model_for_node(node_name: str) -> str:
    if node_name in DECISION_NODES:
        return settings.models.DEEP_MODEL
    if node_name in FORMAT_NODES:
        return settings.models.LOCAL_MODEL
    return settings.models.FAST_MODEL
```

> ⚠️ **Note — Ollama-first mode (active as of 2026-03-30):** `chat_respond` uses `LOCAL_MODEL` instead of `FAST_MODEL` to avoid burning Gemini tokens while the Ollama localtunnel reliability is being stabilised. The original intent was `FAST_MODEL` for conversational responses; it was changed in commit `e4a67cb`. If `_call_model()` cannot reach Ollama, `chat_respond` returns a fallback string rather than silently escalating to Gemini — the Gemini fallback inside `_call_model_ollama()` was permanently disabled in a prior session.

**Re-enable premium (FAST_MODEL) for `chat_respond` — checklist:**

1. Confirm `OLLAMA_HOST` Secret Manager value is a live tunnel URL — `curl {OLLAMA_HOST}/api/tags` returns HTTP 200 with a model list.
2. Confirm the tunnel watchdog task `GAOS-OllamaTunnel` is running and auto-restarting after reboots — `Get-ScheduledTask -TaskName GAOS-OllamaTunnel | Select-Object State`.
3. Run two manual DM tests via `chat_emulator.py` and confirm Ollama replies are coherent and within the latency budget (< 8 seconds end-to-end).
4. In `agents/nexus_prime/orchestrator.py`, in `chat_respond()`, change:
   `model=settings.models.LOCAL_MODEL` → `model=settings.models.FAST_MODEL`
5. Update this table — move `chat_respond` from `LOCAL_MODEL` row to `FAST_MODEL` row.
6. Update `FORMAT_NODES` constant above to remove `chat_respond`.
7. Run `pytest --tb=short` — confirm 600/600 pass.
8. Commit: `fix: restore chat_respond to FAST_MODEL — Ollama tunnel stable`.

### 4.3 Hard Stop Behavior

When `hard_stop_triggered` is set to `True` in any node:
1. The current node completes its current step only (no further LLM calls)
2. State is passed directly to `record`
3. `record` writes a `hard_stop` entry to BigQuery `task_outcomes` with the stopping reason
4. `record` publishes a `STATUS_UPDATE` to `agent.nexus-prime.events` with `status = hard_stop`
5. No Pub/Sub broadcast goes to domain orchestrators — this prevents chain reactions

---

## 5. Knowledge Approval Decision Logic

`knowledge_review` handles the promotion question: _Is this observation trustworthy enough to promote to Memory Bank?_

### 5.1 Decision Criteria

```python
def _evaluate_knowledge_candidate(candidate: dict, similar_memories: list, verdict) -> str:
    """
    Returns: "promote", "reject", "defer_to_human"
    """
    # Hard reject: confidence too low
    if verdict.confidence < 0.60:
        return "reject"

    # Hard reject: semantically identical to existing memory
    if similar_memories and similar_memories[0].similarity >= 0.95:
        return "reject"

    # Auto-promote: high confidence and no conflict with existing memories
    if verdict.confidence >= 0.80 and not _conflicts_with_existing(candidate, similar_memories):
        return "promote"

    # Defer: contradicts existing high-confidence memory
    if _contradicts_existing(candidate, similar_memories):
        return "defer_to_human"

    # Default: moderate confidence but no conflict — defer for safety
    return "defer_to_human"
```

### 5.2 Promotion Sequence

When `promote` is called via the Approval Gate (for deferred knowledge):
1. Read the approved observation from `Pending_Knowledge` sheet (row identified by `content_hash`)
2. Query Memory Bank for the current version (capture its resource name for archiving)
3. Write the new observation to Memory Bank (same corpus, new chunk)
4. Archive the old chunk to `Knowledge/archive/` in Drive with timestamp suffix
5. Update `Pending_Knowledge` row to `Status = Promoted`
6. Log to `Logs` tab with `approved_by` and timestamp

---

## 6. Project Initialization Sequence

When Nexus-Prime detects a new project (either from a `NEW_PROJECT` Pub/Sub message or by scanning `Project Registry` for `status = Pending` rows at boot):

```
1. Validate row has required fields: project_id, project_name, owner_email, budget_ceiling_usd
2. Create Google Sheet workbook:
   a. Copy the default workbook template using Drive API
   b. Rename copy to "{project_name} — Control Plane"
   c. Capture new spreadsheet ID
3. Create Drive folder:
   a. Create subdirectory under root Knowledge/ folder: "Knowledge/{project_id}/"
   b. Capture new folder ID
   c. Copy seed files from Knowledge/default/ to Knowledge/{project_id}/
4. Pub/Sub (no new topics needed):
   a. All topic names include project_id in the message payload — no per-project topics needed
   b. Validate that subscriptions exist (create if missing using ensure_topic_exists)
5. Update Project Registry row:
   a. Update sheet_workbook_id, drive_folder_id fields
   b. Set status = Active
6. Broadcast PROJECT_INITIALIZED to all 6 domain orchestrator topics
7. Log initialization completion to BigQuery task_outcomes
```

**Failure handling:** If any step fails, the row stays in `Pending` status. Nexus-Prime logs the failure, publishes `ESCALATE` to its own topic for human attention, and retries on the next boot.

---

## 7. Conflict Resolution Procedure

Nexus-Prime enters `conflict_resolve` when two orchestrators publish messages with contradictory state for the same entity within the same `project_id`.

### 7.1 Conflict Detection

```python
def _detect_conflict(state: NexusPrimeWorkingMemory, new_msg: A2AMessage) -> bool:
    """
    Returns True if new_msg contradicts a recent message from another agent about the same entity.
    """
    entity_key = new_msg.payload.get("entity_key")
    if not entity_key:
        return False

    recent_agent_states = state.get("agent_state_cache", {})
    for agent_id, cached_msg in recent_agent_states.items():
        if (cached_msg.payload.get("entity_key") == entity_key and
                cached_msg.payload.get("value") != new_msg.payload.get("value") and
                agent_id != new_msg.source_agent):
            state["conflict_queue"].append({
                "entity_key": entity_key,
                "message_a": cached_msg,
                "message_b": new_msg,
            })
            return True

    return False
```

### 7.2 Resolution Prompt Structure

The DEEP_MODEL receives:
- The entity key and both conflicting values
- The agent identities of both sources (their domain and role)
- The timestamp of each message
- The relevant Memory Bank context for the entity (from the appropriate domain corpus)
- The arbitration question: "Which value is more likely to be correct, and why?"

The model returns a `decision` (one of the two values, or a synthesized third value) and a `rationale`.

The resolution is broadcast to ALL agents (not just the two in conflict), so all working memories are synchronized.

---

## 8. Model Call Wrapper

Nexus-Prime uses the shared `call_model()` wrapper from `GAOS-Agent-Spec.md §7`. Additionally, all DEEP_MODEL calls from Nexus-Prime include a system-level context injection with the current `system_state_summary`:

```python
def call_model_nexus(prompt: str, model: str, state: NexusPrimeWorkingMemory) -> ModelResponse:
    # Inject system state into context for all decisions
    system_context = {
        "role": "system",
        "content": (
            "You are Nexus-Prime, the Tier 1 root orchestrator of the Morphic-G AOS system. "
            f"Current project: {state['project_id']}. "
            f"Active projects: {list(state['system_state_summary'].keys())}. "
            f"Parked proposals awaiting approval: {len(state['parked_proposals'])}. "
            "Your decisions are final for all domain orchestrators. "
            "Always include your reasoning before your conclusion."
        )
    }
    messages = [system_context] + state["messages"] + [{"role": "user", "content": prompt}]
    return call_model(messages, model=model)
```

---

## 9. HTTP Endpoints (Cloud Run)

Nexus-Prime exposes these HTTP endpoints as a Cloud Run service:

| Path | Method | Handler | Description |
|------|--------|---------|-------------|
| `/pubsub` | POST | `handle_pubsub_push` | Receives all Pub/Sub push messages |
| `/ttl-sweep` | POST | `handle_ttl_sweep` | Called by Cloud Scheduler hourly |
| `/archive` | POST | `handle_archive` | Called by Cloud Scheduler nightly (2AM) |
| `/sync` | POST | `handle_sync` | Called by Apps Script `syncSkillsToVertex` after approval |
| `/daily-sync` | POST | `handle_daily_sync` | Called by Cloud Scheduler at 6 AM for morning briefing |
| `/chat` | POST | `parse_chat_event` + graph | Google Chat push events (direct messages + mentions) |
| `/vision` | POST | graph via `agent.run()` | Owner vision submission → Blueprint Doc creation |
| `/poll-comments` | POST | `handle_poll_comments` | Cloud Scheduler 5-min doc-comment poll |
| `/infra-provision` | POST | `handle_infra_plan` | CLI-triggered infra diff — builds manifest, writes `Agent_Approvals` row, sends Chat proposal card |
| `/health` | GET | `handle_health` | Returns 200 if the service is running |

All endpoints except `/health` require authentication via OIDC token (Cloud Run service-to-service auth). The `/pubsub` endpoint additionally validates the Pub/Sub push token.

---

## 10. Apps Script Integration Points

Nexus-Prime is the only Cloud Run service that Apps Script calls directly.

### 10.1 `syncSkillsToVertex` Callback

When a proposal is `Approved` in the Sheet, Apps Script calls `POST /sync` with:
```json
{
  "proposal_id": "uuid",
  "code_sha256": "hex-string",
  "approved_by": "email",
  "approver_tier": 5,
  "timestamp": "iso-string"
}
```

Nexus-Prime's `handle_sync` handler:
1. Reads the proposal from `Agent_Approvals` using `proposal_id`
2. Verifies `code_sha256` matches the sheet cell value (if mismatch → raise `SecurityError`)
3. Runs `validate_code_safety()` one final time on the code content
4. Publishes `APPROVAL_RESULT` to `agent.approvals.events` with `status = Approved`
5. The graph picks up the `APPROVAL_RESULT` message and routes to `promote`

### 10.2 Webhook Push (Outbound)

When Nexus-Prime writes a proposal to `Agent_Approvals`, it also calls `post_to_webhook()` from `tools/webhook_sender.py`. The webhook URL points to the Apps Script Web App, which logs the incoming notification and sends an email to the `Authorized Approvers` list.

---

## 11. Testing Requirements

Run current Nexus-Prime-related tests before claiming Phase 4 complete. Nexus-Prime-specific unit tests (§11.1) are a planned backlog item — `tests/test_nexus_prime.py` does not yet exist. Existing coverage:
- **Phase 3 reactive nodes** (`market_watchdog`, `roi_optimizer`): `tests/test_reactive_routing.py`
- **General agent contracts** (U1–U5 unit specs, S1–S4 static analysis gates): `tests/test_agents.py`
- **Tool-layer tests**: `tests/test_webhook_sender.py`, `tests/test_pubsub.py`, `tests/test_google_sheets.py`, etc.

> ⚠️ **Outstanding Phase 4 debt:** The unit tests in §11.1 (route/propose/promote/conflict/init\_project) have not been written. These are required before Phase 5 can begin. Track as a `tests/test_nexus_prime.py` backlog item.

### 11.1 Unit Tests

| Test | What it verifies |
|------|-----------------|
| `test_route_escalate` | `route()` returns `"diagnose"` for `ESCALATE` messages |
| `test_route_knowledge` | `route()` returns `"knowledge_review"` for `KNOWLEDGE_CANDIDATE` |
| `test_route_approval_approved` | approval sub-route returns `"promote"` |
| `test_route_approval_rejected` | approval sub-route returns `"record"` |
| `test_propose_gate_safety_fail` | `propose_gate` sets `hard_stop_triggered = True` when `validate_code_safety()` fails |
| `test_propose_gate_sha_written` | `propose_gate` writes `code_sha256` to sheet row |
| `test_promote_hash_mismatch` | `promote` raises `SecurityError` when hash does not match |
| `test_promote_success` | `promote` calls `_trigger_sync_to_vertex` and sets row to `Deployed` |
| `test_self_approval_blocked` | Nexus-Prime cannot call `promote` in response to a message it published |
| `test_init_project_full_sequence` | `init_project` creates sheet + folder + sets status Active |
| `test_conflict_detect` | `_detect_conflict` returns True for contradictory entity values |
| `test_conflict_resolve_broadcast` | `conflict_resolve` publishes `BROADCAST` to all 6 domain topics |
| `test_hard_stop_routes_to_record` | Graph routes `hard_stop_triggered = True` state to `record` without further LLM calls |
| `test_no_project_id_raises` | All tool calls raise `ValueError` when `project_id` is missing |

### 11.2 Integration Tests

These require live GCP resources (run against the `test-proj` project registry entry, not `default`):

| Test | What it verifies |
|------|-----------------|
| `test_full_escalation_loop` | Domain orchestrator ESCALATE → Nexus-Prime diagnose → propose_gate → human Approved → promote → Cloud Run deploy |
| `test_approval_hmac` | POST with correct HMAC → 200; POST with wrong HMAC → 401 |
| `test_approval_tier_enforcement` | Tier 3 approver cannot unblock a Tier 4 proposal |
| `test_knowledge_auto_promote` | High-confidence candidate with no conflict auto-promotes without human step |
| `test_knowledge_conflict_defers` | Contradicting existing memory writes to `Pending_Knowledge` and awaits human |
| `test_ollama_fallback` | Shutdown Ollama; `record` node falls back to FAST_MODEL without error |
| `test_cost_budget` | Full interaction cycle costs less than $0.01 (via token counters) |

### 11.3 Phase 4 Exit Criteria

Phase 4 is complete when every item below is checked (from `GAOS-Manager-Spec.md §16`):

- [x] Nexus-Prime publishes at least one message to a domain orchestrator via A2A protocol
- [x] Full approval loop completes end-to-end (escalation → proposal → human approval → code deploy)
- [ ] Self-evolution loop completes at least once successfully
- [x] All hard stops verified by unit tests
- [ ] Ollama fallback verified: LOCAL_MODEL timeout → FAST_MODEL
- [x] HMAC verification tests all passing
- [ ] Monthly cost projection under $5.00 based on measured test run costs

---

## 12. Reference Index

| Topic | Location |
|-------|----------|
| Tier 1 behavioral rules (§1) | `GAOS-Manager-Spec.md §1.1` |
| Approval Gate full spec + Apps Script code | `GAOS-Manager-Spec.md §14` |
| Security and guardrails | `GAOS-Manager-Spec.md §15` |
| Self-evolution loop | `GAOS-Manager-Spec.md §13` |
| Memory Bank usage + corpus naming | `GAOS-Memory-Spec.md §6` |
| Common agent patterns (boot, LangGraph, cost limits) | `GAOS-Agent-Spec.md §5–§9` |
| Tool API reference | `GAOS-Tools-Spec.md` |
| Infrastructure setup for all resources | `GAOS-Deploy-Spec.md` |
| Domain orchestrator identity files | `Docs/agents/*.md` |
