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

Nexus-Prime's `StateGraph` has 14 nodes. The router node determines which branch to execute based on message type.

```
┌────────────────────────────────────────────────────────────────┐
│                       Nexus-Prime Graph                        │
│                                                                │
│  [boot] ──► [monitor] ──► [route]                              │
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

Node abbreviations: diag=diagnose, know=knowledge\_review, init=init\_project, conf=conflict\_resolve, park=park\_or\_broadcast, vision=vision\_blueprint, iter=iterate\_plan, prop=propose\_gate, prom=promote, notif=notify\_agents

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
        ensure_topic_exists(topic, pid)

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

    # Tactical override — priority >= 4 is always time-critical
    if priority >= 4:
        data["response_mode"] = "Tactical"

    # Validate response_mode
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

Evaluates a `KNOWLEDGE_CANDIDATE` message from a domain orchestrator. Uses `DEEP_MODEL` to check confidence, freshness, and uniqueness against the Memory Bank.

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
        # Auto-promote: write directly to Memory Bank
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
        # Uncertain — send to Pending_Knowledge for human review
        _write_to_pending_knowledge(state, candidate, resp)

    return state
```

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
            publish(f"agent.{agent}.events", broadcast, state["project_id"])
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
                publish(f"agent.{agent}.events", broadcast, state["project_id"])
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
        update_row("Agent_Approvals", proposal_id, {"Status": new_status}, state["project_id"])

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
```

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

    prompt   = _build_vision_prompt(vision_text, project_id)
    doc_text = _call_llm(prompt, model=settings.models.DEEP_MODEL)

    try:
        doc_id = create_document(title=f"Blueprint: {blueprint_id}",
                                  content=doc_text, project_id=project_id)
        state.setdefault("active_blueprints", {})[blueprint_id] = doc_id
    except Exception as exc:
        logger.error("vision_blueprint: create_document failed: %s", exc)
        return state

    try:
        append_row("Project_Incubator",
                   [blueprint_id, doc_id, "Pending", msg.payload.get("submitted_by", "")],
                   project_id)
    except Exception as exc:
        logger.warning("vision_blueprint: append_row failed: %s", exc)

    try:
        space = (msg.payload or {}).get("space_name") or settings.chat.owner_space
        send_approval_card(space_name=space, proposal_id=blueprint_id,
                           summary=f"Blueprint ready for review: {doc_id}",
                           project_id=project_id)
    except Exception as exc:
        logger.warning("vision_blueprint: send_approval_card failed: %s", exc)

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

### 3.3 Graph Assembly

```python
from langgraph.graph import StateGraph, END

def build_nexus_prime_graph() -> Any:
    graph = StateGraph(NexusPrimeWorkingMemory)

    graph.add_node("boot",             boot)
    graph.add_node("monitor",          monitor)
    graph.add_node("route",            route)
    graph.add_node("diagnose",         diagnose)
    graph.add_node("propose_gate",     propose_gate)
    graph.add_node("knowledge_review", knowledge_review)
    graph.add_node("promote",          promote)
    graph.add_node("init_project",     init_project)
    graph.add_node("notify_agents",    notify_agents)
    graph.add_node("conflict_resolve", conflict_resolve)
    graph.add_node("park_or_broadcast", park_or_broadcast)
    graph.add_node("record",           record)

    graph.set_entry_point("boot")
    graph.add_edge("boot", "monitor")
    graph.add_edge("monitor", "route")

    # Conditional routing from route node
    graph.add_conditional_edges("route", route, {
        "diagnose":          "diagnose",
        "knowledge_review":  "knowledge_review",
        "init_project":      "init_project",
        "conflict_resolve":  "conflict_resolve",
        "promote":           "promote",
        "park_or_broadcast": "park_or_broadcast",
        "record":            "record",
    })

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

```python
DECISION_NODES = {"diagnose", "knowledge_review", "conflict_resolve"}
FORMAT_NODES   = {"record"}

def get_model_for_node(node_name: str) -> str:
    if node_name in DECISION_NODES:
        return settings.models.DEEP_MODEL
    if node_name in FORMAT_NODES:
        # LOCAL_MODEL with fallback to FAST_MODEL on timeout
        return settings.models.LOCAL_MODEL
    return settings.models.FAST_MODEL  # Routing and utility nodes
```

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

Run all tests in `tests/test_nexus_prime.py` before claiming Phase 4 complete.

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

- [ ] Nexus-Prime publishes at least one message to a domain orchestrator via A2A protocol
- [ ] Full approval loop completes end-to-end (escalation → proposal → human approval → code deploy)
- [ ] Self-evolution loop completes at least once successfully
- [ ] All hard stops verified by unit tests
- [ ] Ollama fallback verified: LOCAL_MODEL timeout → FAST_MODEL
- [ ] HMAC verification tests all passing
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
