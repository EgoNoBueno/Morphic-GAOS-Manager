# GAOS Email Pipeline — Reverse-Engineered Specification

> **Status:** Reverse-engineered from source on 2026-04-02. Covers the complete lifecycle from Gmail push notification to terminal LangGraph state, with every function, variable, gate, and side effect documented from live code.
>
> **Purpose:** Canonical reference for the email processing pipeline. Use this document to compare against existing specs (`GAOS-Nexus-Prime-Spec.md`, `GAOS-Tools-Spec.md`, `GAOS-Manager-Spec.md`) and identify discrepancies.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture Summary](#2-architecture-summary)
3. [Phase 1 — Gmail Push → `/gmail-webhook`](#3-phase-1--gmail-push--gmail-webhook)
4. [Phase 2 — `handle_gmail_webhook()` — Fast-Path Publisher](#4-phase-2--handle_gmail_webhook--fast-path-publisher)
5. [Phase 3 — `/pubsub` Endpoint → `agent.run()`](#5-phase-3--pubsub-endpoint--agentrun)
6. [Phase 4 — `NexusPrimeAgent.run()` — State Initialization](#6-phase-4--nexusprimeagentrun--state-initialization)
7. [Phase 5 — `boot()` Node](#7-phase-5--boot-node)
8. [Phase 6 — `monitor()` Node](#8-phase-6--monitor-node)
9. [Phase 7 — `route()` — Conditional Edge](#9-phase-7--route--conditional-edge)
10. [Phase 8 — `process_gmail_notification()` Node](#10-phase-8--process_gmail_notification-node)
11. [Phase 9 — `record()` Node — First Pass](#11-phase-9--record-node--first-pass)
12. [Phase 10 — Second Pub/Sub Cycle — EMAIL_RECEIVED](#12-phase-10--second-pubsub-cycle--email_received)
13. [Phase 11 — `compose_reply()` Node](#13-phase-11--compose_reply-node)
14. [Phase 12 — `record()` Node — Second Pass](#14-phase-12--record-node--second-pass)
15. [Phase 13 — Loop Termination Verification](#15-phase-13--loop-termination-verification)
16. [Error Path — `/log-sink` → `handle_cloud_run_error()`](#16-error-path--log-sink--handle_cloud_run_error)
17. [Telemetry — `@tracked` Decorator](#17-telemetry--tracked-decorator)
18. [Configuration Reference](#18-configuration-reference)
19. [Potential Improvements](#19-potential-improvements)

---

## 1. Overview

The GAOS email pipeline converts an inbound Gmail message into an AI-composed reply in **two asynchronous Pub/Sub cycles**:

1. **Cycle 1 — Ingest:** Gmail push → `/gmail-webhook` → Pub/Sub `GMAIL_NOTIFICATION` → `process_gmail_notification()` → filters, logs, publishes `EMAIL_RECEIVED` → `record()` → END.
2. **Cycle 2 — Reply:** Pub/Sub `EMAIL_RECEIVED` → `compose_reply()` → LLM drafts reply → `send_email()` → intent extraction → `record()` → END.

Each cycle is a complete `NexusPrimeAgent.run()` invocation through the LangGraph `StateGraph`. Both cycles share the same Cloud Run process (`workers=1`), the same Pub/Sub subscription (`nexus-prime.sub.events`), and the same graph topology.

**Key design constraints:**
- `workers=1` — LangGraph state must not be shared across processes
- Gmail push timeout is 10 seconds — `/gmail-webhook` must return in <200ms
- All external calls are instrumented via `@tracked` → `api_call_log` BQ table
- Three independent loop-prevention layers (Rule 26.1, 26.2, 26.3) prevent email storms

---

## 2. Architecture Summary

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Gmail API                                                              │
│  (Google servers)                                                       │
│                                                                         │
│  push notification ──► Pub/Sub topic: gmail-notifications               │
│                              │                                          │
│                              ▼                                          │
│                        Pub/Sub subscription                             │
│                        (push to Cloud Run)                              │
└─────────────────────┬───────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  Cloud Run: nexus-prime  (workers=1, uvicorn)                           │
│                                                                         │
│  ┌──────────────────┐                                                   │
│  │  /gmail-webhook   │◄── Gmail push (OIDC auth)                        │
│  │  (main.py L1123)  │                                                  │
│  │  < 200ms budget   │                                                  │
│  └────────┬─────────┘                                                   │
│           │                                                             │
│           ▼                                                             │
│  handle_gmail_webhook()                                                 │
│  (orchestrator.py L4484)                                                │
│  Publishes A2AMessage(GMAIL_NOTIFICATION)                               │
│           │                                                             │
│           ▼                                                             │
│  Pub/Sub topic: agent.nexus-prime.events                                │
│           │                                                             │
│           ▼                                                             │
│  ┌──────────────────┐                                                   │
│  │  /pubsub          │◄── Pub/Sub push (OIDC auth)                      │
│  │  (main.py L414)   │                                                  │
│  └────────┬─────────┘                                                   │
│           │                                                             │
│           ▼                                                             │
│  NexusPrimeAgent.run(envelope)                                          │
│           │                                                             │
│           ▼                                                             │
│  ┌─────────────────────────────────────────────────────────┐            │
│  │  LangGraph StateGraph                                    │            │
│  │                                                          │            │
│  │  boot ──► monitor ──[route()]──► process_gmail ──► record ──► END   │
│  │                                                          │            │
│  │  (publishes EMAIL_RECEIVED to Pub/Sub)                   │            │
│  └──────────────────────────────────────────────────────────┘            │
│           │                                                             │
│           ▼  (second Pub/Sub delivery)                                  │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────┐            │
│  │  LangGraph StateGraph (second cycle)                     │            │
│  │                                                          │            │
│  │  boot ──► monitor ──[route()]──► compose_reply ──► record ──► END   │
│  │                                                          │            │
│  │  • LLM call (gemini-2.5-flash)                           │            │
│  │  • Rule 26.2 per-task cap                                │            │
│  │  • Rule 26.3 flood guard (BQ query)                      │            │
│  │  • send_email() via Gmail API                            │            │
│  │  • Intent extraction → optional task dispatch            │            │
│  └──────────────────────────────────────────────────────────┘            │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Phase 1 — Gmail Push → `/gmail-webhook`

**Source:** `main.py` lines 1123–1178

**Trigger:** Google's Gmail API pushes a Pub/Sub message when a new email arrives in the monitored inbox (`dhess@sl10repairtechs.com`). The message is delivered to the push subscription which targets:

```
https://nexus-prime-975461050387.us-central1.run.app/gmail-webhook
```

**Authentication:** `_verify_pubsub_audience()` (`main.py` L234–240) confirms the `Authorization: Bearer <token>` header exists. Cloud Run's native OIDC verification validates the actual signature before the request reaches Python.

- Push SA: `pubsub-push-sa@morphic-gaos-prod.iam.gserviceaccount.com`
- Audience: `https://nexus-prime-975461050387.us-central1.run.app`

**Handler steps:**

| Step | Action | Failure Behavior |
|------|--------|-----------------|
| 1 | Reject if `AGENT_NAME != "nexus-prime"` | Return 404 |
| 2 | Parse JSON body → extract `body["message"]["data"]` (base64) | — |
| 3 | Base64-decode → JSON parse → extract `historyId` (string) | — |
| 4 | If no `historyId`: return `{"status":"ok", "history_id":""}` with 200 | Prevents Pub/Sub retry storm |
| 5 | Call `handle_gmail_webhook(project_id, history_id)` (async) | — |
| 6 | Return `JSONResponse({"status":"ok", ...result})` | — |

**Time budget:** Must complete in <200ms. Gmail Pub/Sub timeout is 10 seconds.

**Zero external calls at this stage:** No Gmail API, no Sheets, no LLM, no BigQuery. This endpoint is a pure fast-path publisher.

---

## 4. Phase 2 — `handle_gmail_webhook()` — Fast-Path Publisher

**Source:** `agents/nexus_prime/orchestrator.py` lines 4577–4603

**Purpose:** Converts the `historyId` into a structured `A2AMessage` and publishes it to Pub/Sub for async processing.

**Execution:**

```python
notification = A2AMessage(
    project_id=project_id,
    source_agent="gmail-push",
    target_agent="nexus-prime",
    message_type=MessageType.GMAIL_NOTIFICATION,
    priority=3,
    payload={"history_id": history_id},
)
publish("agent.nexus-prime.events", notification)
```

**`publish()` internals** (`tools/pubsub.py`):
- Serializes `A2AMessage` to JSON via `model_dump_json()`
- Creates `pubsub_v1.PublisherClient()` — new client per call
- Topic path: `projects/morphic-gaos-prod/topics/agent.nexus-prime.events`
- Decorated with `@tracked("pubsub")` → writes telemetry to `api_call_log`

**Returns:** `{"status": "enqueued", "history_id": history_id}`

**Net result:** A `GMAIL_NOTIFICATION` message is now in the `agent.nexus-prime.events` Pub/Sub topic, queued for push delivery to `/pubsub`.

---

## 5. Phase 3 — `/pubsub` Endpoint → `agent.run()`

**Source:** `main.py` lines 418–458

**Trigger:** Pub/Sub subscription `nexus-prime.sub.events` pushes the message to `/pubsub`.

**Steps:**

| Step | Action | Details |
|------|--------|---------|
| 1 | `_verify_pubsub_audience(request)` | Same bearer check as `/gmail-webhook` |
| 2 | Parse JSON envelope | Raw Pub/Sub push body |
| 3 | `_get_agent()` | Lazy-loads `NexusPrimeAgent` singleton (`main.py` L185–211) |
| 4 | `result = await agent.run(envelope)` | Enters LangGraph execution |
| 5 | Return `Response(status_code=204)` | ACK — tells Pub/Sub not to redeliver |

**On exception:** `agent.run()` exceptions are caught, logged, and re-raised as HTTP 500. Pub/Sub sees a 5xx response and **retries** the message according to the subscription's retry policy. This is the correct behavior — a processing failure should trigger redelivery. HTTP 204 (ACK) is returned only when `agent.run()` completes without raising.

**Agent initialization (first call only):**
- Imports `agents.nexus_prime.orchestrator`
- Instantiates `NexusPrimeAgent`
- Constructor: loads `DEEP_MODEL` alias (`gemini-2.5-pro`), loads identity file (`Docs/agents/nexus-prime.md` + Context Trio), compiles LangGraph via `build_nexus_prime_graph()`

---

## 6. Phase 4 — `NexusPrimeAgent.run()` — State Initialization

**Source:** `agents/nexus_prime/orchestrator.py` lines 3760–3820 (`NexusPrimeAgent.run()`)

Creates a **fresh** `NexusPrimeWorkingMemory` TypedDict with ~30+ fields:

| Key | Initial Value | Purpose |
|-----|--------------|---------|
| `task_id` | `uuid4()` | Unique per invocation |
| `project_id` | `""` | Set by `boot()` |
| `cost_usd` | `0.0` | Accumulated LLM spend |
| `tokens_used` | `0` | Accumulated token count |
| `incoming_message` | `None` | Set by `monitor()` |
| `emails_sent_this_task` | (not set, defaults to 0 via `.get()`) | Rule 26.2 counter |
| `hard_stop_triggered` | `False` | Failure flag |
| `_started_at` | `time.time()` | Wall-clock start |
| `_next_node` | `"record"` | Default routing fallback |
| `_raw_incoming` | **the raw Pub/Sub envelope** | Consumed by `monitor()` |
| `active_blueprints` | `{}` | Vision workflow |
| `blueprint_constraints` | `[]` | Vision workflow |
| `monologue_frame` | `None` | Strategic Architect think node |
| `parked_proposals` | `[]` | Approval gate |
| `system_state_summary` | `{}` | Project registry cache |
| `messages` | `[]` | Conversation history |
| `sub_task_results` | `[]` | Task outputs |
| `error_history` | `[]` | Error log |
| `memory_context` | `{}` | Domain memory |
| `episodic_cache` | `{}` | Episodic memory |
| `observation_buffer` | `[]` | OODA observations |

**Invocation:**

```python
final_state = await self._graph.ainvoke(
    {**initial_state, "_raw_incoming": agent_input},
    config={"configurable": {"thread_id": initial_state["task_id"]}},
)
```

**On exception:** Logs to Cloud Logging, sets `status = "failed"`.

**Returns:** `AgentOutput(task_id, project_id, agent_id="nexus-prime", status, result={}, cost_usd)`.

---

## 7. Phase 5 — `boot()` Node

**Source:** `agents/nexus_prime/orchestrator.py` lines 488–596

**Edge:** Entry point of LangGraph. Runs on **every** `ainvoke()` call.

**Boot sequence (Rule 9 — ordered and non-negotiable):**

| Step | Action | Failure Behavior |
|------|--------|-----------------|
| 1 | `state["_started_at"] = time.time()` | — |
| 2 | Initialize ~25 `state.setdefault()` fields | — |
| 3 | Resolve `project_id` from state or `settings.GCP_PROJECT_ID` (`morphic-gaos-prod`) | — |
| 4 | `init_sheets_client(pid)` — Google Sheets API client | Logs ERROR, continues |
| 5 | **Secret validation** — `get_secret("GEMINI_API_KEY", pid)` | `SecretNotFoundError`/`SecretAccessDenied` → `_log_cloud(CRITICAL)` → `sys.exit(1)` |
| 6 | `ensure_topic_exists()` for all 8 topics in `settings.pubsub.all_topics` | Non-fatal, swallowed |
| 7 | Load parked proposals from `Agent_Approvals` Sheet | Non-fatal, swallowed |
| 8 | Load Project Registry → `state["system_state_summary"]` | Non-fatal, swallowed |
| 9 | Log `"Boot complete"` | — |

**Caching:** Step 5 uses `_validated_pids` (module-level set) to cache successful secret checks. After the first successful validation for a given `project_id`, Secret Manager is never called again in that process's lifetime.

**Sheets caching (implemented 2026-04-02):** Steps 7 and 8 are now behind a 300-second TTL cache (`_boot_cache`). On the first invocation (or after TTL expiry), proposals and registry are fetched from Sheets and stored in the cache. Subsequent `ainvoke()` calls within the TTL window return cached data with zero Sheets API calls. Step 4 (`init_sheets_client`) still runs per-call but is a lightweight local initialization when the client is already authenticated.

---

## 8. Phase 6 — `monitor()` Node

**Source:** `agents/nexus_prime/orchestrator.py` lines 597–638

**Edge:** `boot → monitor` (unconditional)

**Steps:**

| Step | Action | Details |
|------|--------|---------|
| 1 | Read `state["_raw_incoming"]` | The raw Pub/Sub envelope |
| 2 | If missing → log warning, return | Increment `step_count` |
| 3 | `decode_push_message(raw)` | `tools/pubsub.py` — base64 decode → JSON → Pydantic `A2AMessage.model_validate()` |
| 4 | Set `state["incoming_message"] = msg` | Validated `A2AMessage` |
| 5 | Set `state["project_id"] = msg.project_id` | From the message payload |
| 6 | Set `state["task_id"] = msg.task_id or uuid4()` | Fresh if not provided |
| 7 | Increment `state["step_count"]` | — |
| 8 | Log `"monitor: decoded msg_type=GMAIL_NOTIFICATION from=gmail-push"` | — |

**Decoding errors** (`MessageDecodeError`, `MessageValidationError`) → logged as ERROR, state returned without `incoming_message` (routes to `record` as fallback).

---

## 9. Phase 7 — `route()` — Conditional Edge

**Source:** `agents/nexus_prime/orchestrator.py` lines 639–682

**Edge:** `route()` is the routing function in `graph.add_conditional_edges("monitor", route, {...})`. It is **not** a registered graph node — it is a pure function that returns a node name string and is called by the LangGraph framework as the conditional edge selector from `monitor`.

`route()` is a **pure function** — no I/O, no network calls, no side effects. It reads `state["incoming_message"]` and returns a node name from a routing table.

**Email-relevant routing entries:**

| `msg.message_type` | Returns | Target Node |
|--------------------|---------|-------------|
| `GMAIL_NOTIFICATION` | `"process_gmail"` | `process_gmail_notification()` |
| `EMAIL_RECEIVED` | `"compose_reply"` | `compose_reply()` |

**Routing table (19 entries in the dict — `APPROVAL_RESULT` is handled inline before the table and is not a routing_table entry):**

| MessageType | Node |
|-------------|------|
| `STATUS_UPDATE` | `record` |
| `TASK_COMPLETE` | `record` |
| `ESCALATION` | `think` |
| `EVOLUTION_REQUEST` | `think` |
| `KNOWLEDGE_CANDIDATE` | `think` |
| `CHAT_MESSAGE` | `think` |
| `BROADCAST` | `conflict_resolve` |
| `NEW_PROJECT` | `init_project` |
| `VISION_SUBMITTED` | `vision_blueprint` |
| `PLAN_REVIEW` | `iterate_plan` |
| `COMMENT_RECEIVED` | `iterate_plan` |
| `SKILL_REQUEST` | `handle_skill_request` |
| `STOCK_INSUFFICIENT` | `market_watchdog` |
| `DEAL_CLOSED` | `roi_optimizer` |
| `INFRA_PROVISION_APPROVED` | `handle_infra_provision` |
| `INFRA_PROVISION_REJECTED` | `handle_infra_provision` |
| `GMAIL_NOTIFICATION` | `process_gmail` |
| `EMAIL_RECEIVED` | `compose_reply` |
| `APPROVAL_REQUEST` | `handle_approval_request` |
| `APPROVAL_RESULT` (Approved) | `promote` |
| `APPROVAL_RESULT` (Rejected) | `record` |
| `APPROVAL_RESULT` (other) | `park_or_broadcast` |

---

## 10. Phase 8 — `process_gmail_notification()` Node

**Source:** `agents/nexus_prime/orchestrator.py` lines 4221–4576

**Registered as:** `_process_gmail_node` in `build_nexus_prime_graph()`

This is the **most complex node** in the email pipeline. It reads the Gmail history delta, applies three filtering gates, logs each valid email, publishes `EMAIL_RECEIVED` for downstream processing, and marks messages as read.

### 10.1 Critical Variables

| Variable | Source | Email Path Value |
|----------|--------|-----------------|
| `project_id` | `state["project_id"]` | `"morphic-gaos-prod"` |
| `task_id` | `uuid4()` | Fresh per invocation |
| `settings` | `get_settings()` | Loaded from `config/settings.yaml` |

### 10.2 Own-Address Set — Loop Prevention (Rule 26.1 Code Layer)

```python
_own_addresses = {
    settings.gmail.monitored_address.lower(),  # "dhess@sl10repairtechs.com"
    settings.gmail.sender_address.lower(),      # "aos@sl10repairtechs.com"
}
```

Additionally calls `get_gmail_service(project_id).users().getProfile(userId="me").execute()` to retrieve the OAuth account's primary email address and adds it to the set. On failure: logs WARNING, proceeds with the two settings addresses only.

**This is the primary loop prevention gate** — any email whose sender is in this set is dropped before the authorized_senders check runs.

### 10.3 Resolve `last_history_id`

- `find_row("System_State", "key", "gmail_last_history_id", project_id)` — reads from Google Sheets
- If not found: falls back to `msg.payload["history_id"]` from the notification (WARNING logged — delta fetch will return empty on first run)
- If still empty: returns early with `{processed: 0, skipped: 0}`

### 10.4 Load Authorized Senders (Rule 26.1 Config Layer)

- `get_secret("GMAIL_AUTHORIZED_SENDERS", project_id)` — from GCP Secret Manager
- Current value (v5): `"dhess@sl10repairtechs.com,denton.hess@gmail.com"`
- Split by comma, lowercase, strip → `authorized_senders` set
- If secret missing: logs ERROR, returns early (rejects all senders)

> ⚠️ **Warning — Loop risk:** The `GMAIL_AUTHORIZED_SENDERS` secret must **never** contain addresses the system sends from (`aos@sl10repairtechs.com`, `dhess@sl10repairtechs.com` as sender). See Rule 26.1 and the 2026-04-02 incident in `AI-Autocoding-Rules.md`.

### 10.5 Fetch Gmail Delta

`fetch_new_messages(project_id, last_history_id)` (`tools/gmail.py`):

| Step | Action | Details |
|------|--------|---------|
| 1 | `_load_credentials(project_id)` | `get_secret("GMAIL_OAUTH_CREDENTIALS", project_id)` → builds `google.oauth2.credentials.Credentials` |
| 2 | `get_gmail_service(project_id)` | `build("gmail", "v1", credentials=creds)` — cached per-process with 300s TTL (implemented 2026-04-02) |
| 3 | `history.list(userId="me", startHistoryId=X, historyTypes=["messageAdded"])` | Returns records AFTER the given history ID |
| 4 | Deduplicate message IDs | Across all `messagesAdded` history records |
| 5 | Per message: `messages.get(userId="me", id=mid, format="full")` | **3 retries** with 1.5s × attempt backoff on 404 |
| 6 | Extract fields | `message_id`, `thread_id`, `from_addr`, `subject`, `body` (text/plain only), `received_at`, `message_id_header` |
| 7 | Return `(messages, new_history_id, skipped_ids)` | `skipped_ids` = permanently missing after 3 retries |

### 10.6 Per-Message Processing Loop

For each message in the delta, three filtering gates are applied in order:

#### Gate 1: Own-Address Exclusion (Rule 26.1)

```python
# Extract bare email from "Display Name <email@example.com>"
_m = re.search(r"<([^>]+)>", from_addr_raw)
from_addr = _m.group(1) if _m else from_addr_raw

if from_addr in _own_addresses:
    # → skip, log WARNING "skipping own outbound message"
    skipped += 1
    continue
```

#### Gate 2: Sender Authorization

```python
if from_addr not in authorized_senders:
    # → skip, log WARNING "unauthorized sender {from_addr} — skipping"
    skipped += 1
    continue
```

#### Gate 3: Subject Keyword Gate

```python
_trigger_kw = settings.gmail.trigger_keyword.strip().lower()  # "kenny"
if _trigger_kw and _trigger_kw not in subject.lower():
    # → skip, log INFO (uses _redact_sender() for privacy)
    skipped += 1
    continue
```

#### Gate 4: Dedup — Duplicate Message ID Check (implemented 2026-04-02)

```python
if find_row("Email Inbox", "message_id", message_id, project_id):
    # → skip, log INFO "duplicate message_id={message_id} — skipping"
    skipped += 1
    continue
# On Sheets failure: proceed (don't drop a real email for a read error)
```

Prevents duplicate replies when `process_gmail_notification` is invoked twice for the same `history_id` (e.g., Pub/Sub redelivery after a slow ACK).

#### If All Gates Pass:

| Step | Action | External Call |
|------|--------|--------------|
| 1 | `get_thread_context(project_id, thread_id)` | Gmail `threads.get()` → last 3 messages |
| 2 | `append_row("Email Inbox", {...}, project_id)` | Google Sheets — logs email with status `"Pending"` |
| 3 | Publish `A2AMessage(EMAIL_RECEIVED)` to `agent.nexus-prime.events` | Pub/Sub |
| 4 | `mark_as_read(project_id, message_id)` | Gmail `messages.modify(removeLabelIds=["UNREAD"])` |
| 5 | `processed += 1` | — |

**EMAIL_RECEIVED payload:**

```python
A2AMessage(
    project_id=project_id,
    source_agent="nexus-prime",
    target_agent="nexus-prime",
    message_type=MessageType.EMAIL_RECEIVED,
    priority=3,
    payload={**message, "thread_context": thread_context},
)
```

The payload contains all extracted fields from the Gmail message plus the thread context list.

### 10.7 Persist New History ID

If `new_history_id` exists **and `skipped_ids` is empty** (implemented 2026-04-02):
- `find_row("System_State", "key", "gmail_last_history_id", project_id)` — check if row exists
- If exists: `update_row(...)` with new value
- If not: `append_row(...)` to create it

If `skipped_ids` is non-empty, the watermark is **not** advanced. Those message IDs may become fetchable on a retry — advancing past them would lose them permanently. The next Gmail notification will re-fetch from the old `history_id`, allowing the 404'd messages a second chance.

### 10.8 Final Log and Return

```python
_log_cloud(..., f"process_gmail: processed={processed} skipped={skipped} new_history_id={new_history_id}")
return {
    **state,
    "outcome": {
        "processed": processed,
        "skipped": skipped,
        "skipped_ids": skipped_ids,
        "new_history_id": new_history_id,
    },
}
```

---

## 11. Phase 9 — `record()` Node — First Pass

**Source:** `agents/nexus_prime/orchestrator.py` lines 1219–1380

**Edge:** `process_gmail → record` (unconditional)

| Step | Action | Details |
|------|--------|---------|
| 1 | `log_state_transition(EXECUTION → OBSERVATION)` | Agent state FSM |
| 2 | Build `outcome` dict | `{task_id, project_id, agent_id, task_type, status, cost_usd, duration_seconds, timestamp, log_date}` |
| 3 | `cb_check("nexus-prime", "bigquery")` | Circuit breaker — if BQ circuit OPEN, skip write |
| 4 | `insert_row("aos_logs.task_outcomes", outcome)` | BigQuery |
| 5 | `save_checkpoint("nexus-prime", project_id, state)` | Phoenix checkpoint (on BQ success only) |
| 6 | `_format_heartbeat(state)` | Attempts `LOCAL_MODEL` call for summary (see note below) |
| 7 | Publish `STATUS_UPDATE` heartbeat | To `agent.nexus-prime.events` (if not already processing a `STATUS_UPDATE`) |
| 8 | `_write_heartbeat(...)` | Writes to "Main Control Plane" Sheet tab + BQ `status_snapshots` |
| 9 | Return state → graph reaches `END` | — |

**Heartbeat short-circuit (implemented 2026-04-02):** `_format_heartbeat()` checks `os.environ.get("K_SERVICE")` (set by Cloud Run) and, if the model is Ollama-prefixed, returns the static fallback immediately — zero network calls. On local dev where Ollama is reachable, the LLM summary path is still used.

---

## 12. Phase 10 — Second Pub/Sub Cycle — EMAIL_RECEIVED

The `EMAIL_RECEIVED` message published in Phase 8 arrives at `/pubsub` via `nexus-prime.sub.events`. This triggers a **second complete `NexusPrimeAgent.run()` invocation**:

- **Phase 4 again:** Fresh `NexusPrimeWorkingMemory` state (all fields re-initialized)
- **Phase 5 again:** `boot()` runs — `_validated_pids` cache skips Secret Manager; `_boot_cache` TTL (300s) skips proposal and registry Sheets reads; only `init_sheets_client` and topic checks run
- **Phase 6 again:** `monitor()` decodes the `EMAIL_RECEIVED` message
- **Phase 7 again:** `route()` → for `MessageType.EMAIL_RECEIVED` → returns `"compose_reply"`

---

## 13. Phase 11 — `compose_reply()` Node

**Source:** `agents/nexus_prime/orchestrator.py` lines 3219–3446

The LLM-powered email reply composer. This is the only node in the email pipeline that calls Gemini.

### 13.1 Extract Payload

```python
from_addr = payload["from_addr"]          # e.g. "denton.hess@gmail.com"
subject = payload["subject"]              # e.g. "GAOS test"
body = payload["body"]                    # plain text
thread_id = payload["thread_id"]
message_id = payload["message_id"]
message_id_header = payload["message_id_header"]    # RFC Message-ID header
thread_context = payload["thread_context"]           # list of last 3 messages
```

If `from_addr` is empty → log WARNING, skip (return state unchanged).

### 13.2 Load Context Trio

`_load_context_trio()` (`agents/__init__.py` L841–862):

Reads three files from `Docs/` (if they exist) and concatenates them as the LLM system prompt:

1. `Docs/about-me.md` — owner business context (The Compass)
2. `Docs/brand-voice.md` — communication standards (The Persona)
3. `Docs/working-preferences.md` — operational rules (The Constitution)

Returns concatenated Markdown joined by `---` separators.

### 13.3 Build LLM Prompt

```
You received this email:
From: {from_addr}
Subject: {subject}
Body:
{body[:1500]}

Thread history (oldest first):
From: {msg.from_addr}
{msg.body[:500]}
---
...

Draft a professional, concise reply as Nexus-Prime, the AI Chief of Staff
for this business. Plain text only. Do not include subject, headers, or
salutation — just the reply body starting with the first substantive sentence.
```

- Body truncated to 1500 characters
- Thread context: last 3 messages, each body truncated to 500 characters

### 13.4 LLM Call

```python
resp = _call_model(
    prompt,
    model=settings.models.FAST_MODEL,   # "gemini-2.5-flash"
    system_prompt=context_trio,
)
```

**`_call_model()` routing** (`agents/__init__.py` L159–232):

1. Not an `ollama/` prefix → routes to `_call_model_gemini()`
2. Wrapped in `record_api_call("gemini", "_call_model", caller, pid)` → writes telemetry to BQ

**`_call_model_gemini()` internals** (`agents/__init__.py` L362–500):

| Step | Action | Failure |
|------|--------|---------|
| 1 | Circuit breaker #1: `cb_check("agents", "gemini/gemini-2.5-flash")` | Trips on 429, opens for 1 hour |
| 2 | Circuit breaker #2: `cb_check("agents", "gemini-api-key")` | Trips on 400, opens for 24 hours |
| 3 | `get_secret("GEMINI_API_KEY", project_id)` | RuntimeError if missing |
| 4 | `genai.Client(api_key=api_key)` | Google AI Studio (NOT Vertex AI) |
| 5 | `client.models.generate_content(model=model, contents=[...], config=GenerateContentConfig(system_instruction=system_prompt))` | — |
| 6 | Return `ModelResponse(text=, cost_usd=0.0, tokens_used=total_token_count, data={})` | — |

**After LLM call:**
- `state["cost_usd"] += resp.cost_usd`
- `state["tokens_used"] += resp.tokens_used`
- If empty reply → log WARNING, return state (skip send)

### 13.5 Rule 26.2 — Per-Task Email Cap

```python
emails_sent = state.get("emails_sent_this_task", 0)  # 0 on first pass
if emails_sent >= settings.outbound.max_emails_per_task:  # threshold: 3
    # skip send, log WARNING
    return state
```

### 13.6 Rule 26.3 — Time-Window Flood Guard

`_check_email_flood(project_id)` (`orchestrator.py` L5107–5161):

```sql
SELECT COUNT(*) AS cnt
FROM `morphic-gaos-prod.aos_logs.api_call_log`
WHERE project_id = @project_id
  AND caller = @caller          -- "nexus-prime"
  AND api_name = 'gmail'
  AND operation = 'send_email'
  AND success = TRUE
  AND ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 60 MINUTE)
```

- If `count >= 10` (threshold) → returns `False` → send blocked, log ERROR
- **On BQ query failure: fails CLOSED** (blocks the send) — logged as ERROR (hardened 2026-04-02, previously failed open)

### 13.7 Send Email

`send_email()` (`tools/gmail.py`):

| Step | Action |
|------|--------|
| 1 | Build `email.mime.text.MIMEText(body, "plain", "utf-8")` |
| 2 | Set headers: `To`, `Subject`, `From` (`aos@sl10repairtechs.com`), `In-Reply-To`, `References` |
| 3 | Base64 URL-safe encode the MIME message |
| 4 | `service.users().messages().send(userId="me", body={"raw": raw_bytes, "threadId": thread_id})` |
| 5 | Return `sent_id` (Gmail message ID) |

Decorated with `@tracked("gmail")` → telemetry to BQ.

**After send:**
- `state["emails_sent_this_task"] = emails_sent + 1`
- Log: `"compose_reply: reply sent to {from_addr} (sent_id={sent_id}, chars={len(reply_text)})"`

### 13.8 Update Email Inbox Status

```python
inbox_rows = get_all_records_with_row_numbers("Email Inbox", project_id)
sheet_row_num = next(
    (rn for rn, rec in inbox_rows if str(rec.get("message_id", "")) == message_id),
    None,
)
if sheet_row_num is not None:
    update_row("Email Inbox", sheet_row_num, {"status": "Replied"}, project_id)
```

> **Bug fix (2026-04-02):** Previously called `update_row("Email Inbox", "message_id", ...)` which searched column A (timestamp) for the literal string `"message_id"` — never matched, silently no-oped. Now uses `get_all_records_with_row_numbers` to find the actual sheet row number by `message_id` column value, then passes the integer to `update_row`.

### 13.9 Intent Extraction Dispatch

`_dispatch_task_from_email()` (`orchestrator.py` L3447–3580):

1. Calls `_call_model(prompt, model=FAST_MODEL)` — **second Gemini call per email**
2. Prompt asks LLM to return JSON: `{"is_task": bool, "task_type": str|null, "task_context": dict}`
3. Known task types and routing:

| `task_type` | Target Agent | Pub/Sub Topic |
|-------------|-------------|---------------|
| `drive_maintenance` | steward | `agent.steward.events` |
| `inventory_check` | foreman | `agent.foreman.events` |
| `deal_closed` | pursuit | `agent.pursuit.events` |

4. If `is_task=true` and valid `task_type`: publishes `TASK_HANDOFF` to domain agent
5. On failure: logs WARNING, does not dispatch (non-fatal)

### 13.10 Return

```python
return {
    **state,
    "outcome": {"replied_to": from_addr, "sent_id": sent_id, "chars": len(reply_text)},
}
```

---

## 14. Phase 12 — `record()` Node — Second Pass

Same as Phase 9, but now recording the `compose_reply` outcome. Task type in BQ: `EMAIL_RECEIVED`.

---

## 15. Phase 13 — Loop Termination Verification

After `compose_reply` sends the reply from `aos@sl10repairtechs.com`:

1. Gmail receives the outbound email → fires another Pub/Sub notification to `gmail-notifications`
2. Notification arrives at `/gmail-webhook` → publishes `GMAIL_NOTIFICATION`
3. Third `process_gmail_notification()` runs:
   - Fetches delta → finds the outbound message
   - **Gate 1 fires:** `from_addr = "aos@sl10repairtechs.com"` is in `_own_addresses`
   - Log: `"process_gmail: skipping own outbound message from aos@sl10repairtechs.com"`
   - `skipped += 1`
   - Result: `processed=0`

**The loop terminates** because the own-address gate (Rule 26.1 code layer) drops the self-generated email. Even if this gate failed, Rule 26.1 config layer (authorized_senders) provides a second check, and Rules 26.2/26.3 cap the total output.

---

## 16. Error Path — `/log-sink` → `handle_cloud_run_error()`

**Source:** `main.py` L1253–1340, `orchestrator.py` L5162–5410

**Trigger:** Cloud Logging log sink routes ERROR+ entries from the `nexus-prime` service to `nexus-prime-error-alerts` Pub/Sub topic → pushes to `/log-sink`.

| Step | Action | Details |
|------|--------|---------|
| 1 | Always returns 204 | **Never retries error alerts** — prevents feedback loop |
| 2 | Parse `LogEntry` from base64 Pub/Sub payload | — |
| 3 | Cooldown check | `find_row("System_State", "last_error_alert_ts")` — 15-minute cooldown |
| 4 | Cooldown check **fails closed** | If Sheets returns 429, alert is suppressed |
| 5 | BQ spike query | API error count in last 30 minutes |
| 6 | Send alert email | To `dentonh18@yahoo.com` (`settings.gmail.alert_address`) |
| 7 | Rule 26.3 flood guard applies | Same BQ-based check as compose_reply |
| 8 | Persist cooldown timestamp | `update_row("System_State", ...)` |

---

## 17. Telemetry — `@tracked` Decorator

**Source:** `tools/__init__.py`

Every Gmail, Sheets, Pub/Sub, BigQuery, and Gemini API call is instrumented via `@tracked(api_name)`:

- Extracts `project_id` from function arguments
- Reads `caller` from thread-local (set to `"nexus-prime"` at boot)
- Wraps call in `record_api_call()` context manager
- Writes to `aos_logs.api_call_log` BigQuery table:

| Column | Description |
|--------|-------------|
| `ts` | Timestamp |
| `api_name` | `"gmail"`, `"sheets"`, `"pubsub"`, `"bigquery"`, `"gemini"` |
| `operation` | Function name (e.g. `"send_email"`, `"_call_model"`) |
| `caller` | Agent ID (e.g. `"nexus-prime"`) |
| `project_id` | GCP project |
| `success` | Boolean |
| `latency_ms` | Wall-clock duration |
| `error_code` | HTTP status or exception name |
| `attempts` | Retry count |
| `tokens_used` | LLM token count (for Gemini calls) |
| `model` | Model alias (for Gemini calls) |

**Recursion prevention:** Uses `_is_metrics_write()` thread-local flag so the BQ `insert_row` call inside `record_api_call` does not trigger another telemetry write.

---

## 18. Configuration Reference

### 18.1 `config/settings.yaml` — Email-Relevant Keys

| Key | Value | Purpose |
|-----|-------|---------|
| `models.FAST_MODEL` | `gemini-2.5-flash` | Reply composition + intent extraction |
| `models.DEEP_MODEL` | `gemini-2.5-pro` | Used by NexusPrimeAgent ADK wrapper (not in email path) |
| `models.LOCAL_MODEL` | `ollama/llama3` | Heartbeat summary (unreachable from Cloud Run) |
| `gmail.monitored_address` | `dhess@sl10repairtechs.com` | Inbox watched by Gmail push |
| `gmail.sender_address` | `aos@sl10repairtechs.com` | Alias used as From on outbound emails |
| `gmail.alert_address` | `dentonh18@yahoo.com` | System error alerts (must NOT equal monitored_address) |
| `gmail.label_id` | `Label_6` | Gmail label for organization |
| `gmail.pubsub_topic` | `projects/morphic-gaos-prod/topics/gmail-notifications` | Gmail push target |
| `gmail.max_results` | `50` | Max messages per history fetch |
| `gmail.trigger_keyword` | `Kenny` | Subject must contain this word (case-insensitive) |
| `outbound.max_emails_per_task` | `3` | Rule 26.2 — hard cap per task execution |
| `outbound.max_publishes_per_task` | `10` | Rule 26.2 — hard cap on Pub/Sub publishes |
| `outbound.flood_window_minutes` | `60` | Rule 26.3 — rolling window |
| `outbound.flood_threshold` | `10` | Rule 26.3 — max emails in window |
| `pubsub.max_hop_count` | `5` | A2A loop prevention |

### 18.2 GCP Secret Manager — Email-Relevant Secrets

| Secret Name | Purpose |
|-------------|---------|
| `GEMINI_API_KEY` | Google AI Studio API key for LLM calls |
| `GMAIL_OAUTH_CREDENTIALS` | OAuth2 credentials JSON for Gmail API access |
| `GMAIL_AUTHORIZED_SENDERS` | Comma-separated sender allowlist |

### 18.3 Infrastructure

| Resource | Value |
|----------|-------|
| Cloud Run service | `nexus-prime` |
| Cloud Run URL | `https://nexus-prime-975461050387.us-central1.run.app` |
| Active revision | `nexus-prime-00081-vvr` |
| Workers | `1` (uvicorn) |
| Pub/Sub push SA | `pubsub-push-sa@morphic-gaos-prod.iam.gserviceaccount.com` |
| Scheduler SA | `nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com` |
| Pub/Sub subscription | `nexus-prime.sub.events` |

---

## 19. Potential Improvements

### Improvement #1: `boot()` Sheets Calls on Every Request

> **✅ Implemented 2026-04-02** — `_boot_cache` with 300s TTL. Proposals and registry are cached module-level; subsequent `ainvoke()` calls within the TTL window make zero Sheets calls.

**What was wrong:** `boot()` runs on every `ainvoke()`. Steps 4 (`init_sheets_client`), 7 (load all `Agent_Approvals` records), and 8 (load Project Registry) made full Sheets API calls on every Pub/Sub message.

**Impact:** 3+ Sheets API calls per email pipeline cycle × 2 cycles = 6+ unnecessary Sheets calls per inbound email. Contributed to 429 quota errors under burst load.

---

### Improvement #2: `get_gmail_service()` Builds a New Client Per Call

> **✅ Implemented 2026-04-02** — `_gmail_svc_cache` with 300s TTL in `tools/gmail.py`. Credential refresh handled transparently by google-auth.

**What was wrong:** `_load_credentials()` called Secret Manager and `build("gmail", "v1")` created a discovery-based client on every Gmail API call. This added ~200–400ms per call.

**Impact:** `process_gmail_notification()` makes 3–5 Gmail API calls (getProfile, history.list, messages.get × N, thread.get, mark_as_read). That was 600ms–2s of unnecessary overhead from repeated client creation.

---

### Improvement #3: Heartbeat `LOCAL_MODEL` Timeout

> **✅ Implemented 2026-04-02** — `_format_heartbeat()` now checks `os.environ.get("K_SERVICE")` and Ollama model prefix. On Cloud Run, returns static fallback immediately with zero network calls.

**What was wrong:** `_format_heartbeat()` called `LOCAL_MODEL` (`ollama/llama3`) to generate a one-sentence summary. Ollama was **unreachable from Cloud Run**, so this always timed out before falling back to a static string.

**Impact:** Added wasted latency to every `record()` node execution (twice per email cycle).

---

### Improvement #4: `_check_email_flood()` Fails Open on BQ Error

> **✅ Implemented 2026-04-02** — Changed to fail closed (`return False`). BQ query failure now blocks the send and logs ERROR severity.

**What was wrong:** If BigQuery was down, the flood guard allowed unlimited sends. This was the last-resort defense (Rule 26.3).

**Impact:** A sustained BQ outage silently disabled the flood guard, meaning Rules 26.1 and 26.2 were the only remaining protections.

---

### Improvement #5: Two Full `agent.run()` Cycles Per Email

> **Deferred** — boot caching (Improvement #1) already cuts overhead of the second cycle by ~90%. The Pub/Sub decoupling provides free retry semantics. Merging would lose that benefit.

---

### Improvement #6: Intent Extraction Uses `FAST_MODEL` Instead of `LOCAL_MODEL`

> **Deferred** — gemini-2.5-flash is free-tier and the cost is negligible (~1–2s latency). A heuristic pre-filter would save that time but adds maintenance burden not justified at current volume.

---

### Improvement #7: `update_row` API Contract in `compose_reply`

> **✅ Fixed 2026-04-02** — Now uses `get_all_records_with_row_numbers("Email Inbox", project_id)` to find the sheet row number by `message_id` column, then passes the integer to `update_row`. Email Inbox status is now correctly updated to `"Replied"`.

**What was wrong:** `compose_reply` called `update_row("Email Inbox", "message_id", {"status": "Replied"}, project_id)`. The `update_row` function interprets a string `row_index` as a value to search in column A (first column). Column A of Email Inbox is `timestamp`, not `message_id`. The search never found a match → `RowNotFoundError` → caught by the outer try/except → silently failed.

**Impact:** Email Inbox rows were never updated to `"Replied"` status. All rows remained `"Pending"` indefinitely.

---

### Improvement #8: No Deduplication on `EMAIL_RECEIVED` Publish

> **✅ Implemented 2026-04-02** — Added a dedup gate in `process_gmail_notification` that checks `find_row("Email Inbox", "message_id", message_id, project_id)` before appending. If the message_id already exists, the email is skipped. On Sheets read failure, processing proceeds to avoid dropping a real email.

**What was wrong:** If `process_gmail_notification` was invoked twice for the same `history_id` (e.g., Pub/Sub redelivery after a slow ACK on `/pubsub`), the same email would be published as `EMAIL_RECEIVED` twice, resulting in duplicate replies.

**Impact:** The recipient would receive two identical AI-generated replies.

---

### Improvement #9: History ID Advancement on Skipped Messages

> **✅ Fixed 2026-04-02** — History ID now only persisted when `skipped_ids` is empty. If messages were skipped (404), the watermark stays at the old position so the next poll can retry those messages.

**What was wrong:** `process_gmail_notification` persisted `new_history_id` even when `processed=0`. If all messages were skipped AND the delta contained a message that got a transient 404 (added to `skipped_ids`), that message was lost forever since the watermark advanced past it.

**Impact:** Rare — only triggered when a 404 coincided with all other messages being filtered. But the consequence was permanent message loss.

---

### Improvement #10: Single-Worker Bottleneck

> **Deferred (infrastructure)** — Already deployed with `workers=1`. This is a Dockerfile/Procfile concern, not a code concern. The fast-path webhook (`/gmail-webhook`) completes in <200ms so it never blocks on LLM calls.

---

_Last updated: 2026-04-03_
_Source: Reverse-engineered from live codebase — line numbers verified against current `master`_
