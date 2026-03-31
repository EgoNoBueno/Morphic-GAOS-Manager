# Gmail Integration — Implementation Plan v3

_Last updated: 2026-03-31_

---

## Architecture Overview

```
Gmail watch() → gmail-notifications Pub/Sub topic
    → POST /gmail-webhook (Cloud Run)
        → publish GMAIL_NOTIFICATION to agent.nexus-prime.events
        → return 200 immediately (< 200ms, zero LLM)
    → existing POST /pubsub handler picks it up
        → route() dispatches to process_gmail_notification LangGraph node
            → history.list() delta fetch
            → sender auth gate + loop prevention
            → get_thread_context() for Institutional Memory
            → append_row("Email Inbox")
            → publish EMAIL_RECEIVED to agent.nexus-prime.events
            → mark_as_read()
            → write new_history_id + watch_expiration to System_State Sheet
```

**Key invariant:** The webhook never touches an LLM. The Gmail Pub/Sub 10-second push timeout is structurally impossible to hit because `/gmail-webhook` only publishes one Pub/Sub message and returns. All processing is handled asynchronously by the existing Pub/Sub pipeline.

---

## Files Changed

| File | Change |
|------|--------|
| `tools/gmail.py` | **New** — 6 public functions |
| `models/__init__.py` | 2 new `MessageType` values |
| `scripts/setup_workspace.py` | Add `System_State` tab to `TABS` + `HEADERS` |
| `agents/nexus_prime/orchestrator.py` | `handle_gmail_webhook()`, `handle_gmail_renew_watch()`, new LangGraph node `process_gmail_notification` |
| `main.py` | 2 new endpoints + docstring table update |
| `scripts/provision_schedulers.py` | 1 new scheduler job |
| `scripts/setup_gmail_oauth.py` | **New** — one-time interactive OAuth2 setup |
| `tests/test_gmail.py` | **New** — 8 unit tests |
| `tests/test_agents.py` | New `TestGmailWebhook` class (4 tests) |
| `config/settings.yaml` | New `gmail:` section |

---

## Phase 1 — `tools/gmail.py` (new)

**6 public functions.** All follow existing tool conventions: explicit `project_id` on every call, raise don't swallow, no global service instance, Google-style docstrings.

### Functions

**`get_gmail_service(project_id: str)`**
- Fetches `GMAIL_OAUTH_CREDENTIALS` JSON blob `{"client_id": ..., "client_secret": ..., "refresh_token": ...}` from Secret Manager via `tools.secrets.get_secret()`
- Builds `google.oauth2.credentials.Credentials` and returns an authenticated `googleapiclient` Gmail service object
- Scopes: `gmail.modify` + `gmail.send`
- Raises `GmailAuthError` if secret missing or token invalid

**`fetch_new_messages(project_id: str, history_id: str) -> tuple[list[dict], str]`**
- Calls `users.history.list(startHistoryId=history_id, historyTypes=["messageAdded"])`
- Decodes multipart MIME; selects `text/plain` parts only — never `text/html` (token cost + noise)
- Returns `(messages, new_history_id)`
- Each message dict: `{message_id, thread_id, from_addr, subject, body, received_at}`
- Raises `GmailAPIError` on API failure

**`get_thread_context(project_id: str, thread_id: str, max_messages: int = 3) -> list[dict]`**
- Calls `users.threads().get(id=thread_id)`
- Extracts the last `max_messages` messages as plain text, oldest first
- Returns same dict shape as `fetch_new_messages` message dicts
- Used by `process_gmail_notification` LangGraph node before composing a reply to give the model Institutional Memory of the conversation

**`mark_as_read(project_id: str, message_id: str) -> None`**
- Calls `users.messages.modify()` to remove the `UNREAD` label
- Called after every successfully processed message to signal state change

**`send_email(project_id: str, to: str, subject: str, body: str, thread_id: str | None = None, in_reply_to: str | None = None) -> str`**
- Constructs RFC 2822 message with `email.mime.text.MIMEText`
- Sets `In-Reply-To` and `References` headers when `in_reply_to` is provided — keeps thread coherent in the recipient's inbox client
- Returns the sent `message_id`
- Raises `GmailAPIError` on API failure

**`setup_watch(project_id: str, topic_name: str, label_id: str) -> str`**
- Calls `users.watch(body={"topicName": topic_name, "labelIds": [label_id]})`
- Returns expiration epoch ms as a string (store in `System_State` Sheet)
- Must be called at deploy time and renewed every ≤7 days via Cloud Scheduler

### Custom Exceptions

```python
class GmailAuthError(Exception):
    """Secret missing, token invalid, or OAuth scope insufficient."""

class GmailAPIError(Exception):
    """Gmail REST API returned a non-retryable error."""
```

---

## Phase 2 — `models/__init__.py`

Add two new values to `MessageType` under a new comment block after the Infrastructure Provisioner block:

```python
# ── Gmail integration ──────────────────────────────────────────────────────
GMAIL_NOTIFICATION = "GMAIL_NOTIFICATION"   # Gmail watch fired — process historyId (internal)
EMAIL_RECEIVED     = "EMAIL_RECEIVED"       # Authorized email parsed and ready for routing
```

- `GMAIL_NOTIFICATION` — internal dispatch token. `/gmail-webhook` publishes this. It carries only `{"history_id": str}` in the payload. Never exposed outside nexus-prime.
- `EMAIL_RECEIVED` — outbound event published after a message is fully parsed and authenticated. This is what downstream orchestrators or LangGraph nodes react to.

---

## Phase 3 — `scripts/setup_workspace.py`

**Two changes:**

1. Add `"System_State"` to the `TABS` list.
2. Add to `HEADERS`:
```python
"System_State": ["key", "value", "updated_at"],
```

**Runtime keys written to this tab:**

| key | value | why it matters |
|-----|-------|---------------|
| `gmail_last_history_id` | `"12345678"` | Prevents reprocessing already-seen messages |
| `gmail_watch_expiration` | ISO-8601 datetime | Single source of truth for watch health — tells you exactly when/why the agent went deaf if Cloud Scheduler fails or hits quota |

---

## Phase 4 — `agents/nexus_prime/orchestrator.py` (3 additions)

### Step 11 — `handle_gmail_webhook(project_id: str, history_id: str) -> dict`

Standalone async function, importable by `main.py`. **Fast path only.**

- Publishes a single `A2AMessage(message_type=GMAIL_NOTIFICATION, payload={"history_id": history_id})` to `agent.nexus-prime.events`
- Returns `{"status": "enqueued", "history_id": history_id}` immediately
- **Zero calls to Gmail API. Zero Sheet reads/writes. Zero LLM.** Must complete in < 200ms.
- The 10-second Gmail Pub/Sub push timeout cannot be hit by design.

### Step 12 — `process_gmail_notification` LangGraph node

Wired into the existing `route()` dispatch: when `route()` receives `MessageType.GMAIL_NOTIFICATION`, it dispatches here.

Logic sequence:
1. Read `gmail_last_history_id` from `System_State` Sheet (`find_row("System_State", "key", "gmail_last_history_id", project_id)`). Fallback to `payload["history_id"]` on first run (seed).
2. Call `fetch_new_messages(project_id, last_history_id)` → `(messages, new_history_id)`
3. **Sender auth gate:** for each message, check `from_addr` against `GMAIL_AUTHORIZED_SENDERS` secret (comma-separated email list fetched from Secret Manager). Skip + log `WARNING` if not authorized.
4. **Loop prevention:** skip + log `WARNING` if `from_addr == settings.gmail.monitored_address`. Prevents the agent from processing its own outbound emails.
5. For each valid message:
   - Call `get_thread_context(project_id, thread_id)` → store in state for LLM node use
   - `append_row("Email Inbox", {Timestamp, From, Subject, Preview(200chars), Message ID, Thread ID, Status="Pending"}, project_id)`
   - Build `A2AMessage(message_type=EMAIL_RECEIVED, payload={message dict + thread_context})` and publish to `agent.nexus-prime.events`
   - `mark_as_read(project_id, message_id)`
6. Write `gmail_last_history_id = new_history_id` to `System_State` Sheet (`update_row` or `append_row` with upsert pattern)
7. Return `{"processed": N, "skipped": N, "new_history_id": new_history_id}`

### Step 13 — `handle_gmail_renew_watch(project_id: str) -> dict`

Standalone async function, importable by `main.py`:
- Reads `settings.gmail.label_id` and `settings.gmail.pubsub_topic`
- Calls `setup_watch(project_id, topic_name, label_id)` → expiration epoch ms
- Converts to ISO-8601 datetime; writes `gmail_watch_expiration` to `System_State` Sheet
- Returns `{"expires_at": iso_datetime_str}`

---

## Phase 5 — `main.py` (2 new endpoints)

### `POST /gmail-webhook`

```python
@app.post("/gmail-webhook")
async def gmail_webhook(request: Request) -> JSONResponse:
    """
    Gmail Pub/Sub push notification — watch fired, new mail arrived.
    Validates the push token, extracts historyId, enqueues for async processing.
    Returns 200 immediately — never runs LLM logic in this request cycle.
    Gmail push timeout is 10 seconds; this handler completes in < 200ms.
    Nexus-Prime only.
    """
```

- `_verify_pubsub_audience(request)` — same auth gate as `/pubsub`
- Nexus-Prime only guard (404 otherwise)
- Decode base64 Pub/Sub message data → extract `historyId` field
- `from agents.nexus_prime.orchestrator import handle_gmail_webhook`
- `result = await handle_gmail_webhook(project_id, history_id)`
- `return JSONResponse(content={"status": "ok", **result})`

### `POST /gmail-renew-watch`

```python
@app.post("/gmail-renew-watch")
async def gmail_renew_watch(request: Request) -> JSONResponse:
    """
    Cloud Scheduler hits this every 23 hours to renew the Gmail watch.
    Gmail watch() expires every 7 days maximum — renewal is mandatory.
    Nexus-Prime only.
    """
```

- Same pattern: auth gate → nexus-prime guard → `handle_gmail_renew_watch(project_id)` → `{"status": "ok", **result}`

**Also:** Update the module-level docstring endpoint table at the top of `main.py` to add both endpoints.

---

## Phase 6 — `scripts/provision_schedulers.py`

Add one entry to the `JOBS` list:

```python
{
    "id": "gaos-gmail-renew-watch",
    "schedule": "0 */23 * * *",   # every 23 hours — safely under Gmail's 7-day expiration
    "path": "/gmail-renew-watch",
    "description": "Renew Gmail watch() subscription (expires every 7 days max)",
},
```

No polling job — the push architecture replaces it.

---

## Phase 7 — `scripts/setup_gmail_oauth.py` (new, interactive one-time)

Steps performed by this script:

1. Prompt for path to `client_secrets.json` (downloaded from GCP Console → APIs & Services → Credentials → OAuth 2.0 Client IDs)
2. Run `google_auth_oauthlib.flow.InstalledAppFlow` with scopes `gmail.modify` + `gmail.send`
3. Print the `refresh_token` and the exact JSON blob to store as `GMAIL_OAUTH_CREDENTIALS` in Secret Manager
4. Print the two gcloud CLI commands needed:
   ```bash
   gcloud pubsub topics create gmail-notifications --project=<project_id>
   gcloud pubsub topics add-iam-policy-binding gmail-notifications \
       --role=roles/pubsub.publisher \
       --member=serviceAccount:gmail-api-push@system.gserviceaccount.com \
       --project=<project_id>
   ```
5. List existing Gmail labels via the API; prompt user to confirm or create a `GAOS-Tasks` label
6. Print the `label_id` to add to `settings.yaml`
7. Call `setup_watch()` to register the initial watch and print the expiration datetime

The script is **interactive** and **idempotent** — safe to re-run if a step failed.

---

## Phase 8 — `tests/test_gmail.py` (new — 8 unit tests)

All mocked at the SDK boundary: `googleapiclient.discovery.build` and `tools.secrets.get_secret`.

| Test | What it verifies |
|------|-----------------|
| `test_fetch_new_messages_happy` | Mock `history.list` returns 2 messages; assert 2 parsed dicts with correct fields; `new_history_id` matches API response |
| `test_fetch_new_messages_empty` | Mock returns no `messagesAdded`; assert `([], same_history_id)` |
| `test_fetch_new_messages_api_error` | Mock raises `HttpError`; assert `GmailAPIError` raised |
| `test_get_thread_context_happy` | Mock `threads.get` returns 5 messages; assert only last 3 returned (default `max_messages`) |
| `test_mark_as_read_happy` | Mock `messages.modify`; assert called with `removeLabelIds=["UNREAD"]` and correct `message_id` |
| `test_send_email_happy` | Mock `messages.send`; assert RFC 2822 message constructed; `message_id` returned |
| `test_send_email_with_thread_id` | Assert `threadId` passed in request body and `In-Reply-To` header set |
| `test_send_email_api_error` | Mock raises `HttpError`; assert `GmailAPIError` raised |
| `test_get_gmail_service_secret_missing` | `get_secret` raises `SecretNotFoundError`; assert `GmailAuthError` raised |

---

## Phase 9 — `tests/test_agents.py` — `TestGmailWebhook` class (4 tests)

| Test | What it verifies |
|------|-----------------|
| `test_gmail_webhook_handler_enqueues` | Mock `publish`; assert `handle_gmail_webhook` returns `{"status": "enqueued"}` and publish called once with `GMAIL_NOTIFICATION`; no Gmail API calls made |
| `test_gmail_process_node_happy` | Mock `fetch_new_messages` (2 msgs), `get_thread_context`, `append_row`, `publish`, `mark_as_read`, Sheet `find_row`/`update_row`; assert `processed=2`, `mark_as_read` called twice |
| `test_gmail_process_node_skips_unauthorized` | Mock `fetch_new_messages` returns 1 msg from unauthorized sender; assert `processed=0`, `skipped=1`, no `append_row` call |
| `test_gmail_webhook_endpoint_wrong_agent` | `TestClient` POST `/gmail-webhook` with `AGENT_NAME=beacon`; assert 404 |

---

## Phase 10 — `config/settings.yaml`

Add a new `gmail:` section:

```yaml
gmail:
  monitored_address: ''       # your Gmail address — populated after setup_gmail_oauth.py
  label_id: ''                # e.g. Label_1234567890 — populated after setup_gmail_oauth.py
  pubsub_topic: ''            # full topic path: projects/<pid>/topics/gmail-notifications
  max_results: 50
```

---

## Secrets Required

| Secret name | Value | Who uses it |
|-------------|-------|-------------|
| `GMAIL_OAUTH_CREDENTIALS` | `{"client_id":"...","client_secret":"...","refresh_token":"..."}` | `tools.gmail.get_gmail_service()` |
| `GMAIL_AUTHORIZED_SENDERS` | `"alice@example.com,bob@example.com"` | `process_gmail_notification` node sender auth gate |

Both fetched at runtime via `tools.secrets.get_secret()`. Neither stored in source or `settings.yaml`.

---

## Verification Checklist

1. `pytest tests/test_gmail.py -v` → 9 tests green
2. `pytest tests/test_agents.py::TestGmailWebhook -v` → 4 tests green
3. `pytest --tb=short` → full suite green (zero regressions)
4. `ruff check --fix .; if ($LASTEXITCODE -eq 0) { ruff format . }`
5. Manual: `python scripts/setup_gmail_oauth.py` → label ID confirmed, watch registered, expiration printed
6. Manual: `python scripts/provision_schedulers.py` → `gaos-gmail-renew-watch` job visible in Cloud Scheduler console
7. Manual E2E: send email with `GAOS-Tasks` label applied → Cloud Logging shows `GMAIL_NOTIFICATION` received + `EMAIL_RECEIVED` published → `Email Inbox` Sheet tab has new row

---

## Out of Scope (Phase 4+)

- LangGraph reply-composition node (wiring `EMAIL_RECEIVED` into a respond/draft workflow)
- Gmail push notifications replacing Cloud Scheduler watch renewal (polling is fine for renewal)
- Per-thread Firestore shadow (Sheets `System_State` is sufficient for MVP)
- Multi-account support
