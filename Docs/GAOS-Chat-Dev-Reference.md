# Google Chat Development Reference — Morphic-GAOS

*Last updated: 2026-04-03*

> ⚠️ **Status — Google Chat delivery ABANDONED (2026-03-30)**
>
> The `/chat` endpoint is deployed, JWT verification works, and all routing logic is
> intact. However, Google Chat never reliably delivered messages from mobile after
> ~2 weeks of testing. The feature was abandoned in favour of Gmail polling.
> **Primary user communication channel is now Gmail** — see `GAOS-Email-Pipeline-Spec.md`.
>
> This reference document is preserved because:
> - The `/chat` endpoint still handles inbound `CARD_CLICKED` events (Approval Gate
>   card buttons) — that path is wired and correct
> - The `tools/google_chat.py` helpers are used by `chat_respond`, `send_approval_card()`,
>   and `send_skill_import_card()` — all still active
> - The architecture patterns (JWT verification, threading, card widgets) remain valid
>   if Chat delivery is re-enabled
>
> Full post-mortem: `GAOS-Nexus-Prime-Spec.md §3.2 chat_respond` and
> `GAOS-Deploy-Spec.md §14 (❌ ABANDONED items)`.

A practical reference for working with the Chat integration layer in this codebase.
All patterns map directly to live code — every example points to a real file.

---

## 1. Architecture Overview

```
Google Chat User
       │  POST (signed JWT)
       ▼
  main.py @app.post("/chat")
       │  _verify_chat_jwt()
       │  parse_chat_event()
       │  build A2AMessage envelope
       ▼
  NexusPrimeAgent.run()
       │  LangGraph: boot → monitor → route → think → chat_respond
       ▼
  tools/google_chat.py — send_message() / send_card()
       │
       ▼
  Google Chat Space (reply)
```

The `/chat` endpoint in [main.py](../main.py) is the only public surface.
**Never call the Chat API directly from orchestrators** — always go through `tools/google_chat.py`.

---

## 2. Event Types & How GAOS Handles Each

`parse_chat_event()` in [tools/google_chat.py](../tools/google_chat.py) normalises all inbound
payloads into a flat dict. The `/chat` handler in [main.py](../main.py) then routes on `event_type`.

| `event_type`        | `action_name`     | Routes to (message type)  | What happens                                      |
|---------------------|-------------------|---------------------------|--------------------------------------------------|
| `MESSAGE` (no image)| —                 | `CHAT_MESSAGE`            | `think → chat_respond` — model replies in-space  |
| `MESSAGE` (image)   | —                 | `VISION_SUBMITTED`        | Gemini vision extract → Blueprint Doc generation |
| `CARD_CLICKED`      | `approve`         | `APPROVAL_RESULT`         | `promote` node — deploys approved code           |
| `CARD_CLICKED`      | `reject`          | `APPROVAL_RESULT`         | `record` node — logs rejection                   |
| `CARD_CLICKED`      | `skill_approve`   | `SKILL_REQUEST`           | `handle_skill_request` node — installs package   |
| `CARD_CLICKED`      | `skill_reject`    | `SKILL_REQUEST`           | `handle_skill_request` node — denies install     |
| `ADDED_TO_SPACE`    | —                 | (welcome)                 | Posts welcome text via `send_message()`, returns 200 — no graph invocation |
| `REMOVED_FROM_SPACE`| —                 | (no-op)                   | Returns 200 immediately — no graph invocation    |

> **Where to add new event types:** Add a new branch in the `/chat` handler in
> [main.py](../main.py) after the `elif event_type == "MESSAGE":` block, then add
> a new `MessageType` variant in [models/\_\_init\_\_.py](../models/__init__.py), and wire
> a `routing_table` entry in `route()` in the orchestrator.

---

## 3. Sending Messages

### Plain text

```python
from tools.google_chat import send_message

send_message(space_name="spaces/XXXXXXXXX", text="Task complete.")
```

Hard limit: 4 096 characters. `send_message()` auto-truncates with `"..."` if exceeded.

### Card v2 (rich UI)

```python
from tools.google_chat import send_card

card = {
    "cardId": "status-card-01",
    "header": {"title": "Deployment Status", "subtitle": "morphic-gaos-prod"},
    "sections": [
        {
            "widgets": [
                {"textParagraph": {"text": "<b>Status:</b> Deployed ✅"}},
                {"textParagraph": {"text": "<b>Revision:</b> nexus-prime-00009-phd"}},
            ]
        }
    ],
}
send_card(space_name="spaces/XXXXXXXXX", card=card)
```

> **Tip:** Use the [Google Card Builder](https://addons.gsuite.google.com/uikit/builder)
> to design the JSON visually. Paste the output here, then ask Copilot:
> _"Map this card JSON to a Python function that takes `title`, `status`, and `revision` as args."_

### Approval card (built-in)

```python
from tools.google_chat import send_approval_card

send_approval_card(
    space_name="spaces/XXXXXXXXX",
    proposal_id="abc-123",
    agent_id="beacon",
    issue_summary="Scout found stale BigQuery partition — recommends archiving.",
    proposed_action="Run archive_sweep() on dataset 'aos_logs' partitions > 90 days.",
    priority=4,
    cost_usd=0.0012,
    doc_url="https://docs.google.com/...",
    reasoning_summary="Tactical mode: high-priority alert, immediate action required.",
)
```

Generates the Approve / Reject card with Strategic Architect reasoning panel.
Button clicks return as `CARD_CLICKED` events routed to `APPROVAL_RESULT`.

### Skill import card (built-in)

```python
from tools.google_chat import send_skill_import_card

send_skill_import_card(
    space_name="spaces/XXXXXXXXX",
    proposal_id="skill-456",
    agent_id="pursuit",
    package_name="shapely",
    reason="Needed for geospatial bounding-box queries in territory analysis.",
    pypi_url="https://pypi.org/project/shapely/",
)
```

### Threaded replies

Two helpers exist in `tools/google_chat.py` for keeping replies inside a thread:

```python
from tools.google_chat import send_threaded_reply, send_reply_in_thread

# Bot-initiated thread — developer-chosen stable key (approval cards, briefings)
send_threaded_reply(
    space_name="spaces/XXXXXXXXX",
    thread_key="approval-abc-123",   # any stable string you choose
    text="Approval request filed.",
)

# Reply to a user message — use server-assigned thread.name from the Chat event
send_reply_in_thread(
    space_name="spaces/XXXXXXXXX",
    thread_name="spaces/XXXXXXXXX/threads/YYYYY",   # from parse_chat_event()["thread_name"]
    text="Got it — processing your request.",
)
```

See §16 for when to use each. `parse_chat_event()` returns the server thread name as
`event["thread_name"]`.

### Infrastructure proposal card (built-in)

```python
from tools.google_chat import send_infra_proposal_card

send_infra_proposal_card(
    space_name="spaces/XXXXXXXXX",
    proposal_id="infra-789",
    agent_id="foreman",
    resource_type="Cloud Run service",
    resource_name="pursuit",
    proposed_action="Scale min-instances from 0 to 1 to reduce cold-start latency.",
    estimated_cost_usd=0.08,
    reasoning="pursuit handles time-sensitive stock alert tasks.",
)
```

---

## 4. Building Interactive Cards

### Widget type reference

| Widget key        | Use case                                    | Supports HTML? |
|-------------------|---------------------------------------------|:--------------:|
| `textParagraph`   | Body copy, status lines, reasoning summaries| Yes (`<b>`, `<i>`, `<code>`) |
| `decoratedText`   | Icon + label pairs (e.g. key/value rows)    | No             |
| `image`           | Inline images (attach a public URL)         | N/A            |
| `buttonList`      | Action buttons (tap → `CARD_CLICKED`)       | No             |
| `selectionInput`  | Dropdowns, checkboxes, radio buttons, multi-select | No        |
| `textInput`       | Single/multi-line text fields (used in dialogs) | No         |
| `divider`         | Horizontal rule between sections            | N/A            |

### Button that triggers a CARD_CLICKED callback

```python
{
    "text": "Approve",
    "onClick": {
        "action": {
            "actionMethodName": "approve",           # matches action_name in parse_chat_event()
            "parameters": [
                {"key": "proposal_id", "value": proposal_id},
            ],
        }
    },
}
```

Parameters land in `event["parameters"]` after `parse_chat_event()` parses them.
The `/chat` handler reads them via `event["parameters"].get("proposal_id", "")`.

### Button that opens a URL

```python
{"text": "View Blueprint", "onClick": {"openLink": {"url": doc_url}}}
```

### Autocomplete dropdown (`selectionInput` with external data source)

For dropdowns that need to be populated dynamically (e.g. searching project names,
contact emails, or Vertex AI corpus entries as the user types):

```python
# Card widget dict — pass inside a section's "widgets" list
{
    "selectionInput": {
        "name": "project_id",           # key in formInputs when dialog is submitted
        "label": "Search Projects",
        "type": "DROPDOWN",
        "externalDataSource": {
            "onCustomItemRequested": {
                "function": "fetchProjectSuggestions",   # action_name in CARD_CLICKED
            }
        },
    }
}
```

When the user types, Chat sends a `CARD_CLICKED` event with
`action_name == "fetchProjectSuggestions"` and the partial text in `event["action"]["inputs"]`.
Your `/chat` handler responds with a `RenderActions` containing the suggestion list:

```python
# Response from /chat when action_name == "fetchProjectSuggestions"
qualified_text = event["action"].get("inputs", {}).get("project_id", "")
suggestions = _search_project_registry(qualified_text)  # returns list[str]
return JSONResponse(content={
    "actionResponse": {
        "type": "UPDATE_WIDGET",
        "updatedWidget": {
            "suggestions": {
                "items": [{"text": s, "value": s} for s in suggestions]
            }
        },
    }
})
```

Copilot prompt: _"Add a `fetchProjectSuggestions` branch to the `CARD_CLICKED` handler
in `main.py` that queries `get_all_records('Project Registry', project_id)`, filters
by the partial text from `event['action']['inputs']['project_id']`, and returns an
`UPDATE_WIDGET` response with the matching project IDs as suggestions."_

---

## 5. Handling the 30-Second Timeout

Google Chat expects an HTTP response within **30 seconds**. The full `boot → monitor
→ route → think → chat_respond → record` graph path calls at least one LLM, which
can take 5–15 seconds. If the graph stalls (Secret Manager cold start, BigQuery
write latency), the 30-second ceiling is reachable.

**Current behaviour:** The `/chat` handler `await agent.run(envelope)` — it blocks
until the full graph completes. This is acceptable for FAST_MODEL calls but is a
risk if the graph hits a DEEP_MODEL path or a slow external call.

**Mitigation pattern for future long-running Chat actions:**

```python
# In main.py /chat handler — return ACK immediately, run work in background
import asyncio

async def _run_and_reply(envelope, space_name):
    agent = _get_agent()
    await agent.run(envelope)
    # send_message() called inside chat_respond node — no action needed here

asyncio.create_task(_run_and_reply(envelope, event["space_name"]))
return JSONResponse(content={"status": "ok"})   # immediate ACK
```

> **Warning:** `asyncio.create_task()` is fire-and-forget. If the Cloud Run instance
> is recycled between the ACK and the task completing, the reply is lost.
> A more robust approach publishes to Pub/Sub and lets the graph reply asynchronously
> via `send_message()` in the `chat_respond` node after the queue delivers the message.

> ⚠️ **Warning — HTTP 204 with a JSON body causes a Pub/Sub retry storm.** Returning
> `JSONResponse(content={...}, status_code=204)` sends a body on a No-Content response.
> uvicorn raises `RuntimeError: Response content longer than Content-Length`, closes the
> connection, and Pub/Sub retries indefinitely — saturating all Cloud Run instances and
> blocking `/chat`. Use `Response(status_code=204)` with **no content** for any handler
> that must return 204. Never pass `content=` or a body to a 204 response.

**"Thinking..." immediate ACK + async patch pattern:**

For tasks that will definitely exceed a few seconds, send a placeholder immediately
then patch it once the real answer is ready:

```python
# Step 1 — ACK with placeholder (inside chat_respond node or /chat handler)
placeholder = send_message(space_name, "Thinking...")
message_name = placeholder.get("name", "")   # e.g. "spaces/XXX/messages/YYY"

# Step 2 — Do the slow work
reply = run_slow_task()

# Step 3 — Patch the placeholder with the real answer
service = _get_chat_service()
service.spaces().messages().patch(
    name=message_name,
    updateMask="text",
    body={"text": reply},
).execute()
```

Copilot prompt: _"Write a helper that sends a 'Thinking...' Chat message and returns a
`patch()` callable that updates it with the final text, using the message name from
the initial send_message() response."_

---

## 6. Mentioning Users in Messages

```python
# Mention a specific user by their Google Workspace user resource name
send_message(space_name, f"<users/USER_ID> your task has been approved.")

# Mention everyone in the space
send_message(space_name, "Hi <users/all>, a new deployment has started!")
```

`USER_ID` is the numeric Google account ID visible in Google Workspace Admin,
or returned in the `sender.name` field of a Chat event (format: `users/12345678901`).

---

## 7. Slash Commands

Slash commands are defined in **Google Cloud Console → Chat API → Configuration → Slash commands**.
Each command gets a numeric `commandId`. The payload arrives as a normal `MESSAGE` event
with an `annotations` array.

```python
# In parse_chat_event() output — text will be "/createTask" but also check annotations
body.get("message", {}).get("slashCommand", {}).get("commandId")  # int or None
```

Handling pattern (add inside the `elif event_type == "MESSAGE":` block in main.py):

```python
slash_cmd = message.get("slashCommand", {})
command_id = slash_cmd.get("commandId")

if command_id == 1:   # /help
    msg_type = MessageType.CHAT_MESSAGE
    payload = {"text": "/help", "sender_email": ..., "space_name": ..., "message_name": ...}
elif command_id == 2:  # /status
    msg_type = MessageType.CHAT_MESSAGE
    payload = {"text": "/status", "sender_email": ..., "space_name": ..., "message_name": ...}
```

> **Recommended IDs for GAOS:**
> | ID | Command      | Intent                                    |
> |----|--------------|-------------------------------------------|
> | 1  | `/help`      | List commands and current system status   |
> | 2  | `/status`    | Report active projects and parked proposals |
> | 3  | `/vision`    | Submit a project vision via text          |
> | 4  | `/approvals` | List pending approval proposals           |

---

## 8. Modal Dialogs

Modal dialogs keep the chat history clean for multi-step flows (e.g. submitting a
vision statement with multiple fields, or confirming a destructive action).

**When to use:** Any flow requiring more than one input from the user, or where
you want to collect structured data without cluttering the space history.

Return a `RenderActions` response from a `CARD_CLICKED` handler:

```python
# main.py — return from chat handler instead of JSONResponse({"status": "ok"})
return JSONResponse(content={
    "actionResponse": {
        "type": "DIALOG",
        "dialogAction": {
            "dialog": {
                "body": {
                    "sections": [
                        {
                            "header": "Submit Project Vision",
                            "widgets": [
                                {
                                    "textInput": {
                                        "label": "Describe your vision",
                                        "type": "MULTIPLE_LINE",
                                        "name": "vision_text",
                                    }
                                }
                            ],
                        }
                    ],
                    "fixedFooter": {
                        "primaryButton": {
                            "text": "Submit",
                            "onClick": {
                                "action": {
                                    "actionMethodName": "submit_vision",
                                    "parameters": [],
                                }
                            },
                        }
                    },
                }
            }
        },
    }
})
```

The submitted form data arrives as another `CARD_CLICKED` event with
`actionMethodName = "submit_vision"` and a `formInputs` dict on the body.

---

## 9. JWT Verification

Every inbound Chat request is verified in `_verify_chat_jwt()` in [main.py](../main.py).

| Env var            | Effect                                               |
|--------------------|------------------------------------------------------|
| `CLOUD_RUN_URL`    | Used as the JWT audience — **must exactly match** the App URL configured in Cloud Console → Chat API → Configuration |
| `SKIP_CHAT_JWT_VERIFY=true` | Bypasses verification entirely — local dev only, never set in Cloud Run |

> **Common 401 cause:** The URL in Cloud Console has a trailing `/` or uses a
> different hostname format than `CLOUD_RUN_URL`. Both must be identical, including
> scheme and no trailing slash.

To verify the current Cloud Run service URL:
```powershell
gcloud run services describe nexus-prime --region=us-central1 --format="value(status.url)"
```

---

## 10. Local Development: Chat Emulator

[scripts/chat_emulator.py](../scripts/chat_emulator.py) lets you test the full
`chat_respond` graph path without deploying to Cloud Run or configuring Chat:

```powershell
# From repo root with venv active
.venv\Scripts\python.exe scripts/chat_emulator.py
```

The emulator:
- Patches `send_message()` to capture replies to the terminal
- Builds synthetic `CHAT_MESSAGE` envelopes identical to what `/chat` produces
- Invokes `build_nexus_prime_graph()` directly — no HTTP, no JWT
- Supports `/vision`, `/status`, and `/help` commands
- Preserves `MemorySaver` state across turns in the same session

---

## 11. Copilot Prompts for Common Tasks

| Task | Prompt |
|------|--------|
| New card widget | `"Generate a Google Chat Card v2 widget for displaying a key/value table with 3 rows. Map to a Python function taking a list of (label, value) tuples."` |
| New button action | `"Add a 'View Logs' button to the approval card in tools/google_chat.py that opens a URL passed as a parameter."` |
| New slash command | `"Add a /status slash command handler to the MESSAGE block in main.py that dispatches a CHAT_MESSAGE with text='/status'."` |
| New event type | `"Add a FORM_SUBMITTED event type to parse_chat_event() in tools/google_chat.py that extracts formInputs from the body."` |
| Async reply | `"Refactor the /chat handler in main.py to ACK immediately and run agent.run() as a background task."` |
| Mock payload | `"Generate a realistic Google Chat MESSAGE event JSON payload for a user sending 'Hello' to a bot in space 'spaces/XXXXXXX'. Include user, space, and message fields matching the parse_chat_event() schema in tools/google_chat.py."` |
| State card builder | `"Refactor send_approval_card() in tools/google_chat.py to accept a single ApprovalCardState dataclass instead of 8 positional args. Generate the dataclass definition."` |

---

## 12. Quick-Reference: Production Checklist

Before deploying any Chat integration change:

- [ ] `CLOUD_RUN_URL` env var matches App URL in Cloud Console **exactly** (no trailing slash)
- [ ] New `action_name` values are handled in the `/chat` handler in main.py
- [ ] New `MessageType` variants are in `routing_table` in `route()` in the orchestrator
- [ ] `parse_chat_event()` returns all fields the new handler needs
- [ ] `send_message()` / `send_card()` calls are inside `try/except` — Chat delivery failure must not propagate as a 500 to Google (it will retry)
- [ ] Any handler returning 204 uses `Response(status_code=204)` with no body — not `JSONResponse` (see §5 warning)
- [ ] `record()` / `publish()` calls from `nexus-prime` do **not** publish `STATUS_UPDATE` messages to `agent.nexus-prime.events` — guard with `if not (msg and msg.message_type == MessageType.STATUS_UPDATE)`
- [ ] New `settings.yaml` keys have a corresponding field declared in the Pydantic model in `config/__init__.py` — Pydantic silently drops unknown fields with no error
- [ ] Full test suite passes: `python -m pytest --tb=short -q` (727+ tests as of 2026-04-03)
- [ ] WORKLOG updated with the change

---

## 13. State-Driven Card Builder Pattern

Avoid functions with 8+ positional arguments. When a card's content depends on
several values that may be populated at different times, pass a single state object
and render from it in one place:

```python
from dataclasses import dataclass, field

@dataclass
class ApprovalCardState:
    proposal_id: str
    agent_id: str
    issue_summary: str
    proposed_action: str
    priority: int = 3
    cost_usd: float = 0.0
    doc_url: str = ""
    reasoning_summary: str = ""

def build_approval_card(state: ApprovalCardState) -> dict:
    """Single render function — the only place card JSON is assembled."""
    ...
```

**Why this matters:** When a card needs a new field (e.g. adding `reasoning_summary`
was a one-file change), the caller sites don't need to change — they just populate
the dataclass. This is the pattern to request when asking Copilot to add fields to
an existing card.

Copilot prompt: _"Refactor `send_approval_card()` in `tools/google_chat.py` to accept
a single `ApprovalCardState` dataclass. Generate the dataclass and update all call
sites."_

---

## 14. The Echo Bug — LLM System Prompt Fix

LLMs used in `chat_respond` and similar conversational nodes tend to mirror or
acknowledge the user's input before answering ("Sure! You asked about X...").
This pattern wastes message space in Chat cards and makes the bot feel robotic.

**Fix:** Add the following instruction to the system prompt (context trio or the
`chat_respond` prompt directly):

```
Do not acknowledge, rephrase, or echo the user's words.
Respond only to the meaning and implication of the message.
Be direct and concise — Chat space is limited.
```

In GAOS, the system prompt for `chat_respond` is the output of `_load_context_trio()`.
The identity file at `Docs/agents/nexus-prime.md` is where persona-level instructions
like this belong — changes there apply to all model calls that load the context trio.

> **Test for it:** In [scripts/chat_emulator.py](../scripts/chat_emulator.py), send
> "How many days until the end of the quarter?" and check whether the first sentence
> of the reply starts with "You asked" or "Sure" — if so, tighten the identity file.

**Concise-card AI constraint (2026 user preference):** Chat UI real estate is narrow.
Users read cards in 3–5 seconds. Add this alongside the no-echo instruction:

```
Keep your reply short enough to fit in a single Chat message without scrolling.
If the user needs detail, end with one follow-up question or a 'View Details' button
offer — do not front-load everything.
```

If the response arrives as a card rather than plain text, limit it to one section
with at most 3 widgets. The `send_approval_card()` pattern in
[tools/google_chat.py](../tools/google_chat.py) already does this — use it as the
template for any new card type.

---

## 15. Command Dispatcher Pattern

Avoid a single `route()` function growing into a long `if/elif` chain as new
`MessageType` values are added. The existing `routing_table` dict in `route()`
already implements this pattern — keep it.

**Current implementation** (from `agents/nexus_prime/orchestrator.py`):

```python
routing_table = {
    MessageType.STATUS_UPDATE:              "record",
    MessageType.TASK_COMPLETE:              "record",
    MessageType.ESCALATION:                "think",          # think → diagnose
    MessageType.EVOLUTION_REQUEST:         "think",          # think → diagnose
    MessageType.KNOWLEDGE_CANDIDATE:       "think",          # think → knowledge_review
    MessageType.CHAT_MESSAGE:              "think",          # think → chat_respond
    MessageType.BROADCAST:                 "conflict_resolve",
    MessageType.NEW_PROJECT:               "init_project",
    MessageType.VISION_SUBMITTED:          "vision_blueprint",
    MessageType.PLAN_REVIEW:               "iterate_plan",
    MessageType.COMMENT_RECEIVED:          "iterate_plan",
    MessageType.SKILL_REQUEST:             "handle_skill_request",
    MessageType.STOCK_INSUFFICIENT:        "market_watchdog",
    MessageType.DEAL_CLOSED:               "roi_optimizer",
    MessageType.INFRA_PROVISION_APPROVED:  "handle_infra_provision",
    MessageType.INFRA_PROVISION_REJECTED:  "handle_infra_provision",
    MessageType.GMAIL_NOTIFICATION:        "process_gmail",
    MessageType.EMAIL_RECEIVED:            "compose_reply",
    MessageType.APPROVAL_REQUEST:          "handle_approval_request",
    # ... add new types here, not as elif blocks
}
return routing_table.get(msg.message_type, "record")
```

**Rule:** Every new `MessageType` gets exactly one entry in `routing_table`.
If the routing logic for a type is conditional (like `APPROVAL_RESULT` which checks
`payload["status"]`), handle that inline at the top of `route()` before the table
lookup — not by adding a new `elif`.

**Rule for `/chat` handler in main.py:** The same discipline applies. New
`event_type` / `action_name` combinations get a new `elif` block that constructs
a payload dict and sets `msg_type` — the envelope construction and `agent.run()`
call at the bottom never change.

Copilot prompt: _"Add a `CARD_CLICKED` handler for `action_name='view_logs'` to the
`/chat` handler in `main.py`. It should build a `CHAT_MESSAGE` payload with
`text='/logs'` and dispatch via the existing envelope pattern."_

---

## 16. Thread Keys — Keep Replies In-Thread

By default, `send_message()` posts to the top level of a space, creating a new
conversation each time. For incident alerts, approval notifications, and any
multi-turn flow, all messages should stay in one thread.

Pass a `thread` body field to the Chat API when creating messages:

```python
# In tools/google_chat.py or a new send_threaded_message() helper
service.spaces().messages().create(
    parent=space_name,
    messageReplyOption="REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD",
    body={
        "text": text,
        "thread": {"threadKey": thread_key},  # developer-chosen stable key
    },
).execute()
```

`thread_key` is any stable string you choose — it acts as a persistent identifier
that Chat maps to a real thread. Good values:

| Use case | `thread_key` value |
|----------|--------------------|
| Approval proposal | `f"approval-{proposal_id}"` |
| Project incident | `f"incident-{project_id}-{date}"` |
| Daily briefing | `f"daily-sync-{date}"` |
| Vision session | `f"vision-{task_id}"` |

> ⚠️ **Warning — `threadKey` does NOT work for replies to user-originated messages.**
> `threadKey` is a developer-assigned namespace. For messages sent by a human, Chat has
> never seen your `threadKey`, so `REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD` falls back to a
> new top-level thread — the bot reply appears disconnected from the original message.
>
> **Correct pattern for replying to a user message:** use the **server-assigned**
> `thread.name` from the inbound Chat event, not `threadKey`:
>
> ```python
> # In tools/google_chat.py — send_reply_in_thread()
> service.spaces().messages().create(
>     parent=space_name,
>     messageReplyOption="REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD",
>     body={
>         "text": text,
>         "thread": {"name": thread_name},   # e.g. "spaces/XXX/threads/YYY"
>     },
> ).execute()
> ```
>
> `thread_name` is `message["thread"]["name"]` from the raw Chat event body.
> `parse_chat_event()` returns it as `event["thread_name"]`.
>
> **`threadKey` is correct for bot-initiated threads** (approval cards, briefings)
> where no user message exists to anchor the thread — the table above still applies
> for that use case.

**Current implementation in GAOS:** `send_reply_in_thread(space_name, thread_name, text)` in
`tools/google_chat.py` uses the `thread.name` pattern above. `chat_respond` in the
orchestrator extracts `thread_name` from the payload and calls it directly.

`send_threaded_reply(space_name, thread_key, text)` uses `threadKey` and is the correct
choice for bot-initiated threads (e.g. approval card follow-ups) where no server thread
name is available.

---

## 17. Block Quote Formatting in Messages

As of March 2026, Google Chat supports Markdown-style block quotes in plain-text
messages and card `textParagraph` widgets.

```python
# Quoting the user's original question in the reply
quoted_reply = f"> {user_text}\n\n{model_reply}"
send_message(space_name, quoted_reply)
```

Useful in `chat_respond` when the model's answer might be read out of context
(e.g. in a space with multiple users). The `>` prefix renders as a grey left-border
block in the Chat UI.

> **Limit:** Block quotes are supported in `text` fields only — not in `header`,
> `decoratedText`, or button labels. Use them in `textParagraph` widgets only.

---

## 18. Conversation State Durability — MemorySaver Limitation

The LangGraph graph in GAOS uses `MemorySaver` for checkpointing:

```python
checkpointer = MemorySaver()
graph = graph_builder.compile(checkpointer=checkpointer)
```

`MemorySaver` is in-process RAM. **Every Cloud Run cold start wipes all state.**
Cloud Run scales to zero after ~15 minutes of inactivity and between request scaling
events, so multi-turn Chat conversations that span more than a few minutes will lose
context.

**Current impact:** Low, because `chat_respond` is stateless by design — it loads
the context trio fresh on each call via `_load_context_trio()`. The `MemorySaver`
checkpoint mainly preserves `parked_proposals` and `system_state_summary` loaded
during `boot`.

**If stateful multi-turn Chat becomes a requirement**, replace `MemorySaver` with a
durable checkpointer keyed to `space.name`:

```python
# Hypothetical — LangGraph supports custom checkpointers via the Checkpointer ABC
# Key: use space_name as thread_id so state survives Cloud Run restarts
config = {"configurable": {"thread_id": space_name}}
await graph.ainvoke(state, config=config)
```

Durable backend options in order of implementation complexity:
1. **BigQuery** — already integrated; serialize state JSON to a `chat_sessions` table keyed by `space_name + date`
2. **Cloud Firestore** — lower latency than BigQuery for read-modify-write; native LangGraph checkpointer available via `langgraph-checkpoint-firestore`
3. **Redis (Memorystore)** — fastest; not currently in the GAOS stack

> **Note:** Adding durable state means the `boot` node's in-memory setup runs once
> per space, not once per Cloud Run instance. Measure BQ/Firestore read latency
> against the 30-second Chat timeout before adopting this pattern.

Copilot prompt: _"Show how to implement a BigQuery-backed LangGraph checkpointer
that stores and retrieves graph state as a JSON column keyed by `thread_id` and
`checkpoint_id`, using the existing `tools/bigquery.py` helpers."_

---

## 19. Production Operational Gotchas

Failures discovered during live Chat integration testing that are non-obvious from the
code alone. Root cause, symptom, and exact fix for each.

### STATUS_UPDATE self-loop — `record()` saturates all instances

**Root cause:** `record()` publishes every log message to `agent.nexus-prime.events`.
`nexus-prime.sub.events` is a push subscription on that same topic. Every published
message triggers a Pub/Sub delivery → `agent.run()` → `record()` → publishes again
— infinite loop at ~25 req/sec.

**Symptom:** All Cloud Run instances at 429, Sheets write quota exhausted within
minutes, zero `/chat` entries responding in logs.

**Fix:** Guard the publish call in `record()` (or wherever `STATUS_UPDATE` messages
are generated):

```python
if not (msg and msg.message_type == MessageType.STATUS_UPDATE):
    publish(topic, msg)
```

### HTTP 204 + JSON body → uvicorn RuntimeError → Pub/Sub retry storm

**Root cause:** `JSONResponse(content={...}, status_code=204)` attaches a body to a
No-Content response. uvicorn raises `RuntimeError: Response content longer than
Content-Length` and closes the TCP connection. Pub/Sub treats this as a delivery
failure and retries at ~1 req/sec indefinitely.

**Symptom:** The same Pub/Sub message ID visible in Cloud Logging every second,
handler apparently executing but the message never ACKs, instances stuck.

**Fix:** `return Response(status_code=204)` — no `content=` argument, no
`JSONResponse`. Any 204 path must have zero body.

### `threadKey` doesn't keep replies in-thread for user messages

**Root cause:** `threadKey` is a developer namespace; it only creates or joins threads
that the bot itself originated. A human's message lives in a Chat-managed thread
with no developer `threadKey`, so `REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD` with a
`threadKey` starts a new thread instead of replying.

**Symptom:** Bot replies appear as new top-level posts, not threaded under the user's
message. Content is correct; only threading is wrong.

**Fix:** Use `message["thread"]["name"]` (server-assigned) extracted from the inbound
Chat event. Pass it as `{"thread": {"name": thread_name}}` in the create body.
Full details and the correct function signature in §16.

### Cloud Run auth credential path — diagnose with a `log.warning` branch marker

**Root cause pattern:** When a tool uses a multi-branch credential strategy (key file
→ DWD → ADC fallback), a 403 from the target API doesn't reveal which branch was
taken. If a config value like `dwd_subject` is silently absent (see next entry),
the fallback runs without error — the 403 only appears when the wrong credential
hits the API.

**Diagnostic pattern:** Add a `log.warning("tool: credential path=<branch>")` at the
top of each credential branch. One line in Cloud Run Logs immediately reveals the
path; no local reproduction needed.

```python
if dwd_subject:
    log.warning("google_docs: credential path=DWD subject=%s", dwd_subject)
    # ... DWD setup
else:
    log.warning("google_docs: credential path=ADC (no dwd_subject configured)")
    # ... ADC fallback
```

### Pydantic silently drops unknown YAML fields — new `settings.yaml` keys vanish

**Root cause:** Pydantic `BaseModel` ignores YAML keys that have no matching field
declaration. A value present in `settings.yaml` is invisible at runtime if the
corresponding field is missing from the model — no error, no warning, the field reads
as its default (usually `""`).

**Symptom:** Config appears correct in `settings.yaml`; runtime shows the default
value with no indication the YAML key was ever read.

**Fix:** Every new key added to `settings.yaml` **must** have a matching typed field
in the relevant config class in `config/__init__.py`:

```python
# ❌ Wrong — dwd_subject is in settings.yaml but DocsConfig has no field for it
class DocsConfig(BaseModel):
    scope: str = ""

# ✅ Correct
class DocsConfig(BaseModel):
    scope: str = ""
    dwd_subject: str = ""   # must declare or Pydantic silently drops the YAML value
```

---

_Last updated: 2026-03-24 — Morphic-GAOS Phase 2.5_
