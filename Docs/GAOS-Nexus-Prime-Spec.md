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
from typing import Optional
from models import A2AMessage, ApprovalProposal
from tools.google_sheets import SheetRow

class NexusPrimeWorkingMemory(AgentWorkingMemory):
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
    parked_proposals: list[ApprovalProposal]  # Reloaded on boot from Agent_Approvals tab
    
    # Project initialization state (ephemeral per task)
    pending_project_row: Optional[SheetRow]
    new_project_id: Optional[str]
    
    # Evolution gate state
    candidate_code: Optional[str]
    candidate_agent_id: Optional[str]
    candidate_sha256: Optional[str]
    safety_check_passed: bool
    
    # System-wide health tracking
    system_state_summary: dict              # Populated from heartbeats
    last_ttl_sweep_at: Optional[str]        # ISO timestamp of last TTL sweep
```

---

## 3. LangGraph Graph

### 3.1 Node Inventory

Nexus-Prime's `StateGraph` has 10 nodes. The router node determines which branch to execute based on message type.

```
┌─────────────────────────────────────────────────────────┐
│                     Nexus-Prime Graph                   │
│                                                         │
│  [boot] ──► [monitor] ──► [route]                       │
│                               │                         │
│               ┌───────────────┼───────────────┐         │
│               │               │               │         │
│          [diagnose]    [knowledge_review]  [init_project]│
│               │               │               │         │
│          [propose_gate]   [promote]       [provision]   │
│               │                               │         │
│          [park_or_broadcast]             [notify_agents] │
│               │                               │         │
│          [conflict_resolve]                   │         │
│               │                               │         │
│               └───────────────┬───────────────┘         │
│                               │                         │
│                          [record] ──► END               │
└─────────────────────────────────────────────────────────┘
```

### 3.2 Node Definitions

#### `boot`

Runs once at service startup. Initializes all subscriptions, loads parked proposals from `Agent_Approvals`, and validates service account credentials.

```python
def boot(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    from tools.google_sheets import get_all_records
    from tools.pubsub import ensure_topic_exists
    import settings

    # Ensure all 8 topics exist (idempotent)
    for topic in settings.pubsub.all_topics:
        ensure_topic_exists(topic, state["project_id"])

    # Load parked proposals (Status = "Pending" or "Needs Revision")
    all_proposals = get_all_records("Agent_Approvals", state["project_id"])
    state["parked_proposals"] = [
        row for row in all_proposals
        if row.get("Status") in ("Pending", "Needs Revision")
    ]

    # Load system state summary (all active projects from Project Registry)
    registry = get_all_records("Project Registry", state["project_id"])
    state["system_state_summary"] = {
        row["project_id"]: row["status"]
        for row in registry if row.get("project_id")
    }

    return state
```

#### `monitor`

Listens for incoming Pub/Sub push messages. Decodes the `A2AMessage` envelope and loads it into working memory. This is the only node that reads from external HTTP input.

```python
def monitor(state: NexusPrimeWorkingMemory, incoming: dict) -> NexusPrimeWorkingMemory:
    from tools.pubsub import decode_push_message
    msg = decode_push_message(incoming)
    state["incoming_message"] = msg
    state["project_id"] = msg.project_id
    state["task_id"] = msg.task_id
    return state
```

#### `route`

Examines `incoming_message.message_type` and returns the next node name. This is a pure routing function — it makes no LLM calls and writes no data.

```python
def route(state: NexusPrimeWorkingMemory) -> str:
    msg = state["incoming_message"]
    if msg is None:
        return "record"   # TTL sweep or heartbeat with no action needed

    routing_table = {
        "STATUS_UPDATE":    "record",          # Log and store; no action
        "TASK_COMPLETE":    "record",          # Log and store; no action
        "ESCALATE":         "diagnose",         # Tier 2 needs help
        "EVOLUTION_REQUEST": "diagnose",        # Code evolution cycle requested
        "APPROVAL_RESULT":  "route_approval",   # Human responded to a proposal
        "KNOWLEDGE_CANDIDATE": "knowledge_review",  # New observation to evaluate
        "BROADCAST":        "conflict_resolve", # Cross-domain state conflict
        "NEW_PROJECT":      "init_project",     # Project Registry change detected
    }
    return routing_table.get(msg.message_type, "record")

# Approval sub-route (called from route when message_type == APPROVAL_RESULT)
def route_approval(state: NexusPrimeWorkingMemory) -> str:
    result = state["incoming_message"].payload.get("status")
    if result == "Approved":
        return "promote"
    elif result == "Rejected":
        return "record"   # Log rejection; remove from parked_proposals
    else:
        return "park_or_broadcast"
```

#### `diagnose`

Runs when an orchestrator has escalated or requested evolution. Uses `DEEP_MODEL` to analyze the error fingerprint, search Memory Bank for similar past failures, and determine if self-repair is possible or if a human approval proposal is needed.

```python
def diagnose(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    from tools.google_sheets import find_row
    from tools.memory import query_memory_bank

    msg = state["incoming_message"]
    error_fp = msg.payload.get("error_fingerprint", "")

    # Search Memory Bank for matching past failures
    similar = query_memory_bank(
        query=error_fp,
        corpus=f"gaos-{msg.agent_id.replace('-', '_')}",
        project_id=state["project_id"],
        top_k=3
    )

    # Also check Error Logs sheet for recent occurrences 
    recent_errors = find_rows("Error Logs", {"error_fingerprint": error_fp}, state["project_id"])

    # Determine next action using DEEP_MODEL
    analysis_prompt = _build_diagnosis_prompt(msg, similar, recent_errors)
    decision = call_model(analysis_prompt, model=settings.models.DEEP_MODEL)

    state["messages"].append({"role": "assistant", "content": decision.text})
    
    if decision.suggests_code_change:
        state["evolution_triggered"] = True
        state["candidate_agent_id"] = msg.agent_id
    
    return state
```

#### `propose_gate`

Only called after `diagnose` indicates code evolution is needed AND `validate_code_safety()` passes. Writes a proposal row to `Agent_Approvals` and registers it in `parked_proposals`.

```python
def propose_gate(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    from tools.google_sheets import append_row
    from tools.webhook_sender import post_to_webhook
    import hashlib, uuid

    if not state.get("candidate_code"):
        raise ValueError("propose_gate called without candidate_code in state")

    # Safety gate — hard stop if blocked by allowed_imports or blocklist
    safety_result = validate_code_safety(state["candidate_code"])
    if not safety_result.passed:
        state["hard_stop_triggered"] = True
        _log_hard_stop(state, safety_result.reason)
        return state

    sha256 = hashlib.sha256(state["candidate_code"].encode()).hexdigest()
    proposal_id = str(uuid.uuid4())
    priority = _compute_priority(state)

    row = {
        "ID": proposal_id,
        "Agent ID": state["candidate_agent_id"],
        "Issue": state["incoming_message"].payload.get("issue", ""),
        "Trigger Reason": state["incoming_message"].payload.get("trigger_reason", ""),
        "Stopping Constraint": state["incoming_message"].payload.get("stopping_constraint", ""),
        "Iterations Run": state["incoming_message"].payload.get("iterations_run", 0),
        "Total Cost USD": round(state["cost_usd"], 6),
        "Proposed Code": state["candidate_code"],
        "Status": "Pending",
        "Timestamp": utcnow_iso(),
        "Approved By": "",
        "Approver Tier": "",
        "code_sha256": sha256,
    }

    append_row("Agent_Approvals", row, state["project_id"])
    state["parked_proposals"].append(row)
    state["candidate_sha256"] = sha256

    # Notify via webhook — HMAC signed
    post_to_webhook(
        url=get_secret("WEBHOOK_URL", settings.gcp.project_id),
        payload=row,
        hmac_secret=get_secret("WEBHOOK_HMAC_SECRET", settings.gcp.project_id),
        code_sha256=sha256
    )

    return state
```

#### `knowledge_review`

Evaluates a `KNOWLEDGE_CANDIDATE` message from a domain orchestrator. Uses `DEEP_MODEL` to check confidence, freshness, and uniqueness against the Memory Bank.

```python
def knowledge_review(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    from tools.memory import query_memory_bank

    msg = state["incoming_message"]
    candidate = msg.payload

    # Check for duplicates in the Memory Bank
    duplicates = query_memory_bank(
        query=candidate["content"],
        corpus=f"gaos-{candidate['domain']}",
        project_id=state["project_id"],
        top_k=5,
        similarity_threshold=0.92
    )

    prompt = _build_knowledge_review_prompt(candidate, duplicates)
    verdict = call_model(prompt, model=settings.models.DEEP_MODEL)

    if verdict.confidence >= 0.80 and not verdict.is_duplicate:
        # Promote immediately if high confidence and no duplicate
        state["messages"].append({
            "role": "assistant",
            "action": "promote",
            "content_hash": candidate.get("content_hash"),
            "corpus": f"gaos-{candidate['domain']}"
        })
    else:
        # Add to Pending_Knowledge sheet for human review
        _write_to_pending_knowledge(state, candidate, verdict)

    return state
```

#### `promote`

Only called when `APPROVAL_RESULT` status is `Approved`. Verifies the SHA-256 hash matches `Agent_Approvals`, then calls `syncSkillsToVertex()` via the Apps Script webhook, then writes the promoted document to Memory Bank and archives the old version in Drive.

```python
def promote(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    from tools.google_sheets import find_row, update_row
    from tools.drive import write_file, read_file, copy_file
    from tools.webhook_sender import post_to_webhook

    msg = state["incoming_message"]
    proposal_id = msg.payload["proposal_id"]

    # Find the proposal row
    row = find_row("Agent_Approvals", {"ID": proposal_id}, state["project_id"])
    if row is None:
        raise ValueError(f"promote: proposal {proposal_id} not found in Agent_Approvals")

    # Hash must match — reject if tampered
    live_sha = hashlib.sha256(row["Proposed Code"].encode()).hexdigest()
    if live_sha != row["code_sha256"]:
        raise SecurityError(f"promote: SHA-256 mismatch for proposal {proposal_id}. Aborting.")

    # Trigger deployment via Apps Script syncSkillsToVertex
    _trigger_sync_to_vertex(row, state)

    # Update proposal status to Deployed
    update_row("Agent_Approvals", proposal_id, {"Status": "Deployed"}, state["project_id"])

    # Remove from parked proposals in working memory
    state["parked_proposals"] = [
        p for p in state["parked_proposals"] if p["ID"] != proposal_id
    ]

    return state
```

#### `init_project`

Called when a `NEW_PROJECT` message is received (or when `boot` detects a `Pending` row in `Project Registry`). Provisions all infrastructure for the new project namespace.

```python
def init_project(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    from tools.google_sheets import get_all_records, update_row
    
    pending = get_all_records("Project Registry", state["project_id"])
    new_rows = [r for r in pending if r.get("status") == "Pending"]

    for row in new_rows:
        new_pid = row["project_id"]
        # 1. Create a new Sheet workbook (via Drive API copy of template)
        new_sheet_id = _create_sheet_workbook(new_pid)
        # 2. Create Knowledge/ subfolder in Drive
        new_folder_id = _create_drive_folder(new_pid)
        # 3. Pub/Sub topics already exist (single shared topic namespace using project_id in payload)
        # 4. Update Project Registry row to Active
        update_row("Project Registry", row["ID"], {
            "status": "Active",
            "sheet_workbook_id": new_sheet_id,
            "drive_folder_id": new_folder_id,
        }, state["project_id"])
        # 5. Broadcast to all orchestrators so they initialize their workspace
        state["pending_project_row"] = row
        state["new_project_id"] = new_pid

    return state
```

#### `notify_agents`

Called after `init_project`. Publishes a `BROADCAST` message to all 7 domain orchestrator topics notifying them of the new project namespace.

```python
def notify_agents(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    from tools.pubsub import publish
    import settings

    new_pid = state.get("new_project_id")
    if not new_pid:
        return state

    broadcast = A2AMessage(
        sender_id="nexus-prime",
        recipient_id="broadcast",
        project_id=state["project_id"],
        task_id=state["task_id"],
        message_type="BROADCAST",
        payload={
            "action": "PROJECT_INITIALIZED",
            "new_project_id": new_pid,
            "sheet_workbook_id": state["pending_project_row"]["sheet_workbook_id"],
            "drive_folder_id": state["pending_project_row"]["drive_folder_id"],
        }
    )

    for agent in ["ledger", "beacon", "pursuit", "foreman", "steward", "scout"]:
        publish(f"agent.{agent}.events", broadcast, state["project_id"])

    state["active_broadcasts"].append(broadcast)
    return state
```

#### `conflict_resolve`

Called when two orchestrators have published messages with contradictory state about the same entity (same `project_id` + same entity key, different values). Uses `DEEP_MODEL` to arbitrate and publishes a `BROADCAST` resolution.

```python
def conflict_resolve(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    from tools.pubsub import publish

    for conflict in state["conflict_queue"]:
        prompt = _build_conflict_prompt(conflict)
        resolution = call_model(prompt, model=settings.models.DEEP_MODEL)

        broadcast = A2AMessage(
            sender_id="nexus-prime",
            recipient_id="broadcast",
            project_id=state["project_id"],
            task_id=state["task_id"],
            message_type="BROADCAST",
            payload={
                "action": "CONFLICT_RESOLVED",
                "entity_key": conflict["entity_key"],
                "resolution": resolution.decision,
                "rationale": resolution.rationale,
            }
        )

        for agent in ["ledger", "beacon", "pursuit", "foreman", "steward", "scout"]:
            publish(f"agent.{agent}.events", broadcast, state["project_id"])

        state["active_broadcasts"].append(broadcast)

    state["conflict_queue"] = []
    return state
```

#### `park_or_broadcast`

Called when an approval result is neither `Approved` nor `Rejected` (e.g., `Needs Revision`), or when a proposal cannot yet proceed. Updates the `Agent_Approvals` row status and keeps the proposal in `parked_proposals`.

```python
def park_or_broadcast(state: NexusPrimeWorkingMemory) -> NexusPrimeWorkingMemory:
    from tools.google_sheets import update_row

    msg = state["incoming_message"]
    proposal_id = msg.payload.get("proposal_id")
    new_status = msg.payload.get("status", "Parked")

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

    outcome = {
        "task_id": state["task_id"],
        "project_id": state["project_id"],
        "agent_id": "nexus-prime",
        "task_type": state["incoming_message"].message_type if state.get("incoming_message") else "TTL_SWEEP",
        "status": "hard_stop" if state["hard_stop_triggered"] else "success",
        "error_fingerprint": state.get("incoming_message", {}).get("payload", {}).get("error_fingerprint", ""),
        "cost_usd": state["cost_usd"],
        "duration_seconds": _elapsed_seconds(state),
        "timestamp": utcnow_iso(),
        "log_date": utcnow_date(),
    }
    insert_row("aos_logs.task_outcomes", outcome)

    # Publish heartbeat
    heartbeat = A2AMessage(
        sender_id="nexus-prime",
        recipient_id="broadcast",
        project_id=state["project_id"],
        task_id=state["task_id"],
        message_type="STATUS_UPDATE",
        payload={"summary": _format_heartbeat(state)}
    )
    publish("agent.nexus-prime.events", heartbeat, state["project_id"])

    return state
```

### 3.3 Graph Assembly

```python
from langgraph.graph import StateGraph, END

def build_nexus_prime_graph() -> StateGraph:
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

    return graph.compile()
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
| Deploy code that fails the import allowlist | Hard stop — `validate_code_safety()` must return `passed = True` | `propose_gate` gate check is unconditional; raises `CodeSafetyError` on failure |
| Deploy code without SHA-256 match | Hard stop — `promote` verifies hash against both `code_sha256` field and live cell value | `promote` raises `SecurityError` on mismatch and rolls back the approval status to `Needs Revision` |
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
                agent_id != new_msg.sender_id):
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
