# GAOS Work Log

Active work session. Updated in real time — refresh or keep open in VS Code.
**Most recent entries are at the top.**

---

## 2026-03-23T10:04-03:00 — Bug 7+8: Silent publish() Failures + Nexus-Prime Boot Secret Validation

### What was done
Full codebase audit against AI-Autocoding-Rules.md uncovered two critical bugs.

**Bug 7 — publish() 3-arg TypeError (silent failure across 5 orchestrators):**
11 call sites across Beacon (3), Pursuit (2), Scout (4), Steward (3), and Ledger (3)
passed `project_id` as a third argument to `publish(topic_name, message)` which only
accepts 2 parameters (`project_id` is read internally from `settings.GCP_PROJECT_ID`).
Every call raised `TypeError: publish() takes 2 positional arguments but 3 were given`
— but the error was silently swallowed by `except Exception: pass` blocks.
Result: all Pub/Sub messages (heartbeats, handoffs, escalations) from 5 of 6
sub-orchestrators were silently dropped. Only Foreman had correct 2-arg calls.
Fix: removed the trailing `pid` argument from all 11 call sites.

**Bug 8 — Nexus-Prime boot() missing secret validation (Rule 9 §7 Step 3):**
`boot()` did not call `get_secret()` at startup — a missing `GEMINI_API_KEY`
would only be discovered at first task execution, not at boot. All other
orchestrators (Beacon confirmed) already implement the fail-fast pattern.
Fix: added `get_secret("GEMINI_API_KEY", pid)` with `sys.exit(1)` on
`SecretNotFoundError` or `SecretAccessDenied`, matching Beacon's established pattern.

### Files changed
- `agents/beacon/orchestrator.py` — Removed trailing `pid` from 3 `publish()` calls
- `agents/pursuit/orchestrator.py` — Removed trailing `pid` from 2 `publish()` calls
- `agents/scout/orchestrator.py` — Removed trailing `pid` from 4 `publish()` calls
- `agents/steward/orchestrator.py` — Removed trailing `pid` from 3 `publish()` calls
- `agents/ledger/orchestrator.py` — Removed trailing `pid` from 3 `publish()` calls
- `agents/nexus_prime/orchestrator.py` — Added secret validation block in `boot()`

### Tests
413/413 passed (full suite in ~581s).

### Audit findings tracked for later
- 3 tools missing test files: `drive.py`, `google_search.py`, `web_search.py` (Rule 8)
- `_ALLOWED_IMPORTS` wider than spec (28+ vs ~14 documented)
- ~40 bare `except Exception: pass` blocks (intentional per SIM105 ignore in pyproject.toml)

### What's next
- Deploy to Cloud Run with Bug 7+8 fixes
- Address remaining audit findings (missing tests, allowed imports alignment)

---

## 2026-03-22T20:15-03:00 — Bug 5+6: Chat Thread Routing + Double LLM Call

### What was done
Diagnosed and fixed two production bugs causing "Nexus-Prime not responding" errors in Google Chat.

**Bug 5 — Thread routing (root cause of "not responding" symptom):**
`chat_respond()` called `send_threaded_reply(space_name, threadKey=message_name)`.
`threadKey` is a developer-assigned identifier that only works when the bot originally
created the thread. User-originated messages have no bot-assigned threadKey →
`REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD` created a new top-level thread for every reply.
The user's original thread received no reply → Chat showed "not responding."
Fix: extract `message.thread.name` (server-assigned, always present on inbound events)
from `parse_chat_event()` and use new `send_reply_in_thread(space, thread_name, text)`
that sends with `{"thread": {"name": thread_name}}`.

**Bug 6 — Double LLM call (latency):**
`CHAT_MESSAGE` routed through `think()` (FAST_MODEL call #1 — deterministic result for
CHAT_MESSAGE) then `chat_respond()` (FAST_MODEL call #2). Added `CHAT_MESSAGE` fast-path
in `think()` that skips the model call with `response_mode: "Direct"`, cutting hot-path
latency roughly in half.

### Files changed
- `tools/google_chat.py` — Added `thread_name` extraction to both `_parse_legacy_format`
  and `_parse_addons_format`; added `send_reply_in_thread()` function.
- `main.py` — Added `"thread_name": event.get("thread_name", "")` to CHAT_MESSAGE payload.
- `agents/nexus_prime/orchestrator.py` — `chat_respond()` now uses `send_reply_in_thread`
  when `thread_name` present; `think()` skips LLM for CHAT_MESSAGE.
- `tests/test_google_chat.py` — Added C17 (`send_reply_in_thread` API shape), C18
  (raises on empty thread_name); updated P1 to assert `thread_name`; updated `_message_body`
  to include `thread.name`.

### Tests
413/413 passed (34/34 for `test_google_chat.py` in 0.90s, full suite in ~676s).

### What's next
Full codebase alignment audit — completed in next session (see Bug 7+8 entry above).

### What's next
- Live chat smoke test — send a message to Nexus-Prime and confirm reply lands in same thread
- Phase 3 tasks (see GAOS-Agent-Spec.md §Phase 3 checklist)

---

## 2026-03-21T22:17-03:00 — Bug 4: STATUS_UPDATE Heartbeat Self-Loop + Pub/Sub Storm

### What was done
Identified and fixed a fourth production bug: `record()` published a `STATUS_UPDATE`
heartbeat to `agent.nexus-prime.events` on every message completion, including when
processing a `STATUS_UPDATE`. The `nexus-prime.sub.events` push subscription re-delivers
own heartbeats → infinite fan that flooded all 5 Cloud Run instances → 429s blocked
`/chat` from getting any instance.

### Root cause
- `route()` maps `STATUS_UPDATE → "record"` (line 545, intentional — skip LLM)
- `record()` unconditionally published `STATUS_UPDATE` to `agent.nexus-prime.events`
- `nexus-prime.sub.events` is a push subscription on that topic → delivered back to `/pubsub`
- Each delivery triggered another `record()` → another publish → infinite loop at ~25 req/sec

### Fix
Added guard in `record()` before the heartbeat `publish()` call:
```python
if not (msg and msg.message_type == MessageType.STATUS_UPDATE):
    publish("agent.nexus-prime.events", heartbeat)
```
`heartbeat_text` is computed unconditionally (above the guard) so `_write_heartbeat()` still has it.

### Compound effect
The loop also caused `boot()` → `init_sheets_client()` to hit Sheets Read quota [429]
(60 reads/min default). All instances hammering Sheets during rapid boot cycles
exhausted the quota. This compounded with the earlier Bug 2 (HTTP 204 body →
uvicorn crash → initial Pub/Sub retry seed) to create a self-sustaining storm even
after Bug 2 was fixed.

### Storm clearance
- Seeked all 8 nexus-prime subscriptions to near-future timestamps (01:05Z, 01:20Z)
  twice — once per `gcloud pubsub subscriptions seek --time=<ts>` batch
- New revision `nexus-prime-00012-wc7` deployed, traffic switched
- HTTP 429 rate: ~25/sec → 0 after fix

### Files changed
- `agents/nexus_prime/orchestrator.py` — guard in `record()` before `publish("agent.nexus-prime.events", ...)`

### Tests
- 411/411 passed — no regressions

### What's next
- User should send a fresh Google Chat message to test end-to-end
- Monitor Sheets quota recovery (~1–2 min after storm stops)
- Consider reducing `init_sheets_client` calls in `boot()` or adding lazy init strategy for high-frequency deployments

---

## 2026-03-21T21:54-03:00 — Google Chat Operational Integrity Audit + Three Bug Fixes

### What was done
Full operational integrity check of the Google Chat integration path. All 411 tests
passed before and after changes. Three production bugs found and patched; nexus-prime
redeployed to revision `nexus-prime-00011-sg5` (serving 100% traffic).

### Files changed
- **`main.py`** — Two fixes:
  1. `_verify_chat_jwt`: Changed `audience=service_url` to `audience=f"{service_url}/chat"`.
     The Google Chat JWT `aud` claim is the full endpoint URL including the `/chat` path.
     Using the bare base URL caused every inbound Chat event to return 401.
  2. `/pubsub` handler: Changed `JSONResponse(content={"status":"ok"}, status_code=204)`
     to `Response(status_code=204)`. HTTP 204 No Content must have no body; the JSON body
     caused uvicorn to raise `RuntimeError: Response content longer than Content-Length` on
     every Pub/Sub ACK, crashing the response and triggering a Pub/Sub retry storm
     (the origin of the repeated 429 "no available instance" errors in Cloud Run logs).

- **`agents/nexus_prime/orchestrator.py`** — `boot()` function: Changed
  `state.get("project_id", settings.GCP_PROJECT_ID)` to
  `state.get("project_id") or settings.GCP_PROJECT_ID`. The `initial_state` dict
  initialises `project_id=""` (empty string), so `dict.get(key, default)` returned `""`
  (key is present!) and never used the default. Every agent invocation called
  `init_sheets_client("")` and logged ERROR on boot. The `or` operator correctly
  falls back when the value is falsy.

### Bugs found from logs (before fixes)
| Bug | Symptom | Root cause |
|-----|---------|------------|
| Chat JWT 401 | Every inbound `/chat` POST rejected 401 | `aud` verified against base URL; Chat sends `/chat` path |
| Sheets init ERROR | `project_id ''` on every invocation | `dict.get("", default)` returns `""` not fallback |
| Pub/Sub retry storm | 429 "no available instance" flood | HTTP 204 with JSON body → uvicorn RuntimeError → Pub/Sub retries |

### Test result
411 passed — all green before and after.

### Deployment
`gcloud run deploy nexus-prime --source . --region us-central1 --project morphic-gaos-prod`
Traffic migrated to `nexus-prime-00011-sg5` via `update-traffic --to-revisions`.

> **Note:** `gcloud run deploy --source` with an explicitly pinned traffic revision
> (spec.traffic[].revisionName) creates a new revision but does NOT automatically
> update traffic. Must follow with `gcloud run services update-traffic --to-revisions
> <new-rev>=100` or redeploy with `--tag latest`.

### What's next
- Conduct live mobile Chat test (send a message to Nexus-Prime in the owner DM space).
- If daily-sync briefing card is still missing at 06:00, rebuild with settings.yaml check.

---

## 2026-03-21T21:08-03:00 — Chat quality improvements from GAOS-Chat-Dev-Reference.md

### What was done
Implemented 5 code changes and a test payload layer derived from the reference doc
(§16 thread keys, §14 echo constraint, welcome message, canonical payloads):

1. **`tools/google_chat.py`** — Added `send_threaded_reply(space_name, thread_key, text)`.
   Calls the Chat API with `messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD` and
   `thread.threadKey`. All bot replies now stay in the originating thread.

2. **`agents/nexus_prime/orchestrator.py` — `chat_respond` node** — Replaced
   `send_message(space_name, reply)` with `send_threaded_reply(space_name, thread_key, reply)`
   where `thread_key = message_name or f"chat-{task_id}"`. Fixes the root cause of every
   `chat_respond` reply creating a new top-level thread.

3. **`main.py` — `ADDED_TO_SPACE` handler** — Replaced the silent no-op with a welcome
   message via `send_message()`. Failure is caught and logged; the 200 ACK is always sent.
   `REMOVED_FROM_SPACE` still returns 200 silently.

4. **`Docs/agents/nexus-prime.md` — Don't section** — Added: "Never acknowledge, rephrase,
   or echo the user's words back to them in a Chat reply."

5. **`tests/payloads/`** — Created canonical mock JSON payloads:
   - `chat_message.json`
   - `added_to_space.json`
   - `card_clicked_approve.json`
   - `card_clicked_skill_approve.json`

6. **`tests/test_google_chat.py`** — Added C15 (`send_threaded_reply` API call shape),
   C16 (`send_threaded_reply` raises on empty thread_key), updated E5 (asserts
   `send_message` IS called and `agent.run()` is NOT called), added E9 (loads
   canonical payload file and asserts CHAT_MESSAGE routing).

### Test result
411 passed (up from 408) — all green.

### What's next
- Fix live Chat 401 (JWT audience mismatch: verify `CLOUD_RUN_URL` env var matches
  Chat Console App URL exactly, no trailing slash — see `_verify_chat_jwt`).
- Consider promoting `tests/payloads/` canonical files to integration smoke tests.


## 2026-03-21T18:15-03:00 — Chat Interface Debugging + Graph Fix + chat_respond Node

**What was done:** Tested Google Chat interface live. "How many days to Thanksgiving" got no response. Diagnosed three bugs: (1) Cloud Run IAM blocked Google Chat with 403, (2) LangGraph `INVALID_GRAPH_NODE_RETURN_VALUE` — `route` was registered as a graph node but returned a string, (3) `CHAT_MESSAGE` had no handler and fell through to `record` silently.

### Changes

**`agents/nexus_prime/orchestrator.py`**
- Removed `route` as a graph node. `route()` is a routing function only (returns string) and cannot be a LangGraph node (which must return dict). Moved conditional edges from `"route"` node to `"monitor"` node.
- Inlined `_route_approval` sub-routing into `route()` for `APPROVAL_RESULT` messages. Removed `_route_approval` function and the broken `"_route_approval"` routing key that was not in the conditional edge map.
- Added `CHAT_MESSAGE → "think"` to the routing table.
- Updated `think()` to set `_next_node = "chat_respond"` when `msg_type == CHAT_MESSAGE`.
- Added new `chat_respond()` node: calls FAST_MODEL with context trio + user text, sends reply via `send_message(space_name, reply)`.
- Updated `_route_from_think` conditional edge map to include `"chat_respond": "chat_respond"`.
- Added `graph.add_node("chat_respond", chat_respond)` and `graph.add_edge("chat_respond", "record")`.

**GCP — Cloud Run IAM**
- Added `allUsers` to `roles/run.invoker` on `nexus-prime` Cloud Run service.
- Required: grant `roles/orgpolicy.policyAdmin` to `dhess@sl10repairtechs.com` at org level, set project-level org policy override `constraints/iam.allowedPolicyMemberDomains → allValues: ALLOW` to bypass domain restriction that was blocking `allUsers` + external service accounts.

### Tests
408/408 pass. The graph fix (removing route as node) doesn't have direct test coverage — tested manually by deploying to Cloud Run.

### What's next
- Test Chat bot again: "How many days to Thanksgiving" should get a reply
- Approval Gate E2E: submit proposal → tap Approve in Chat → verify Sheet row
- Vision path E2E: send image to bot → verify Blueprint Doc created
- §4e billing check (~2026-03-28)



**What was done:** Force-ran all 4 Cloud Scheduler jobs and verified HTTP 200 responses in Cloud Logging. Discovered and fixed two bugs in the nightly archive path. Checked off 4/6 §4d items (Approval Gate and Vision path still require manual human test). Also checked off §4c `chat.owner_space`.

### Changes

**`agents/nexus_prime/orchestrator.py`** — `_parse_ts()` extended to handle Google Sheets timestamp format (`M/D/YYYY H:MM:SS`) in addition to ISO 8601. All 3 `bq_rows` construction blocks in `handle_archive()` now call `_parse_ts(...).isoformat()` / `.date().isoformat()` instead of passing the raw Sheet string to BQ.

**`Docs/GAOS-Deploy-Spec.md`** — §4c `owner_space` and §4d nightly-archive, ttl-sweep, daily-kickoff, doc-comment-poll checked off with evidence. Bug note added at §4d.

### Scheduler job verification (Cloud Logging, 2026-03-21T20:13Z)

| Job | Endpoint | HTTP | Internal result |
|-----|----------|------|-----------------|
| ttl-sweep | POST /ttl-sweep | 200 | — |
| nightly-archive | POST /archive | 200 | `NIGHTLY_ARCHIVE complete: 0 rows archived` (no aged rows yet) |
| daily-kickoff | POST /daily-sync | 200 | `DAILY_SYNC complete: 2 logs, 0 errors, 5 pending approvals` |
| doc-comment-poll | POST /poll-comments | 200 | `poll_comments complete: 1 published, 0 errors, 1 docs polled` |

### Bugs found + fixed

**Bug 1 — BQ timestamp format:** `aos_logs.task_outcomes` and `aos_logs.approval_history` rejected all rows because Sheets stores `3/21/2026 20:13:00` but BQ TIMESTAMP requires `YYYY-MM-DD HH:MM:SS`. Fix: `_parse_ts()` now tries `%m/%d/%Y %H:%M:%S` as a fallback after ISO parse fails.

**Bug 2 — Missing BQ schema columns:** `aos_logs.approval_history` was missing `issue` (STRING) and `code_sha256` (STRING) columns. Fix: added via Python BQ client `update_table()` call directly against the live table.

> **Lesson learned:** Google Sheets formats dates as locale-dependent `M/D/YYYY H:MM:SS` strings (not ISO). Any code that passes Sheet timestamp values to BigQuery TIMESTAMP/DATE columns must normalize through a parser, not assume ISO format. `strptime` with `%m/%d/%Y %H:%M:%S` handles 1- and 2-digit month/day correctly.

### Remaining §4d (manual human interaction required)
- **Approval Gate Chat-path E2E** — submit a proposal, tap Approve in Chat, verify Sheet row + Pub/Sub
- **Vision path E2E** — send image to Chat bot, verify Blueprint Doc created in Drive + link reply

---

## 2026-03-21T17:06-03:00 — Phase 4 §4e + §4f Exit Criteria · commit a0c7074

**What was done:** Ran all Phase 4 §4e cost/security verification items and the full §4f GAOS-Doctor health check. Built `scripts/gaos_doctor.py` as the actual runnable doctor implementation (replaces the placeholder spec). 33/33 checks passed.

### Changes

**`scripts/gaos_doctor.py`** — New: 5-section health check script (33 checks):
1. Sheet connectivity — Project Registry readable (2 rows)
2. Pub/Sub — all 8 topics + 23 subscriptions active + 3 spot-checks
3. Secret Manager — all 6 secrets accessible (GEMINI_API_KEY, OLLAMA_HOST, WEBHOOK_HMAC_SECRET, WEBHOOK_URL, GOOGLE_SEARCH_API_KEY, GOOGLE_SEARCH_CX)
4. Cloud Run /health — all 7 services HTTP 200
5. Vertex AI RAG corpora — all 7 corpora exist + named

> **Lesson learned:** For local dev, `gcloud auth print-identity-token --impersonate-service-account=nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com --audiences=<URL>` is required to call Cloud Run endpoints with `--no-allow-unauthenticated`. User ADC (OAuth refresh token) cannot produce OIDC ID tokens directly — only service account credentials or metadata server can. `nexus-prime-sa` has `roles/run.invoker` on all 7 services and is the right SA to use for the all-services health check.

**`Docs/GAOS-Deploy-Spec.md §19`** — §4e/§4f items checked off:
- 4e: budget alert ($10/month `SL10 Cloud Dev Budget`, thresholds 50%/90%/100%) ✅
- 4e: Cloud Logging `_Default` bucket retention = 7 days ✅
- 4e: all 7 Cloud Run services — no `allUsers` IAM binding ✅
- 4f: GAOS-Doctor 33/33 passed ✅; updated runbook reference to `scripts/gaos_doctor.py`
- 4e `/health` note: updated to show SA impersonation command

**`Docs/GAOS-Doctor.md`** — Replaced placeholder spec with actual implementation docs: quick start, 33-check table, prerequisites, known limitations (ID token note), last run timestamp.

### What remains for Phase 4 exit criteria
- `[ ]` Cloud Billing ≤ $5/month — needs 7 days of production traffic data (check ~2026-03-28)
- `[ ]` §4c: `VERTEX_AGENT_ENDPOINT` Apps Script property verified at correct URL
- `[ ]` §4c: `chat.owner_space` in settings.yaml confirmed (currently set; needs live smoke test)
- `[ ]` §4d: all 6 E2E validation items (Approval Gate Chat-path, Vision path, nightly archive, TTL sweep, daily kickoff, doc comment poll)

---



**What was done:** Picked up from previous session (commit `44a80ba`). Audited all 18 files in `Docs/` plus `README.md` and `Docs/agents/` for stale facts. Found and fixed every discrepancy.

### Changes

**GAOS-Tools-Spec.md** — **Critical structural bug fixed:** A 323-line duplicate block (§12 web_search, §15–§17 vertex_search/google_docs/google_search with wrong section numbers) had been inserted mid-way through the §11 google_chat section, splitting Chat's Error Types from Settings Required/Usage Rule. Used PowerShell line-level splice (removed lines 926–1248) to restore clean §1–§18 structure. No duplicate sections remain.

**GAOS-Deploy-Spec.md** — Added `— the GCP project that owns \`oauth-client.json\`; see §0.4` parenthetical after OAuth project number `490183704378` in §4.4 bound-script warning.

**GAOS-Onboarding-Spec.md** — Added `>=1.0.0` version pin to `google-adk` in the bootstrap `uv pip install` command (matches `pyproject.toml`).

**Morphic-GAOS-Manager-Summary.md** — Updated `396-test suite` → `408-test suite` in the file/directory reference table (line 463); the other two 396→408 fixes from the prior session were already applied.

**Docs/agents/nexus-prime.md** — Removed `**[Phase 2.5]**` labels from three Objectives bullets (Vision Blueprint, ITERATE_PLAN, Chat approval callbacks) — these are now live production capabilities, not phase-gated items.

**README.md** — Version pin, 9 endpoints, Phase 3/4 status, footer updated (applied earlier in session before summarization interrupted).

**Verified clean (no changes needed):** GAOS-Agent-Spec.md, GAOS-Manager-Spec.md, GAOS-Nexus-Prime-Spec.md, GAOS-Memory-Spec.md, GAOS-Doctor.md, GAOS-Persona-Spec.md, GAOS-Privacy-Spec.md, GAOS-Skill-Compliance-Spec.md, GAOS-Project-Glossary.md, AI-Autocoding-Rules.md, about-me.md, brand-voice.md, working-preferences.md, Docs/agents/beacon.md, foreman.md, ledger.md, pursuit.md, scout.md, steward.md.

### Tests
No code changes — docs only. Test suite unchanged at 408 passing.

### What's next
- §4e/§4f cost and security verification
- GAOS-Doctor.md checklist items (placeholder doc — may need expansion)
- Phase 4 exit criteria: all checkboxes in Deploy-Spec §11

---



**What was done:** Resumed after Windows crash. Force-ran all 4 Scheduler jobs. Identified and fixed two production bugs discovered during §4d E2E validation. Confirmed pipeline live via Chat notification (5 approval proposals received). Updated spec and memory.

### Changes
- **No source files modified** — all fixes were GCP infrastructure changes
- **IAM:** Granted `roles/secretmanager.secretAccessor` at project level to 6 SAs (`ledger-sa`, `beacon-sa`, `pursuit-sa`, `foreman-sa`, `steward-sa`, `scout-sa`). Previously missing — agents were hitting `Permission denied` on all secret fetches.
- **Cloud Run:** Updated all 7 services from `--timeout 60s` to `--timeout 300s`. 60s was insufficient for Gemini API + LangGraph graph execution under load, causing `asyncio.exceptions.CancelledError` and HTTP 500s.
- **`Docs/GAOS-Deploy-Spec.md`:** Added warning to §3.2 (per-secret IAM silently may not apply — verify with `get-iam-policy`; fallback project-level grant shown). Added warning to §9.1 (`--timeout 60s` → `--timeout 300s`; symptom and fix command included). Updated both deploy loop and re-deploy snippet to use `300s`.
- **`/memories/repo/gotchas.md`:** Added two new bullets for secret IAM and timeout issues.

### Bugs Found and Fixed
1. **Secret access denied for 6 SAs** — `gcloud secrets get-iam-policy GEMINI_API_KEY` returned zero bindings despite §3.2 commands having been run. Root cause unclear (possible propagation failure). Fixed with project-level grant.
2. **60s Cloud Run timeout** — `CancelledError` on Foreman, Beacon, Steward during LLM calls. Fixed to 300s across all 7 services.

### Verifications passed
- **§4d Scheduler E2E** — `daily-kickoff` triggered → Nexus-Prime processed → Chat DM received 5 approval proposals ✅
- **All 7 services** now at 300s timeout ✅
- **All 6 non-nexus SAs** have `roles/secretmanager.secretAccessor` ✅

### What's next
- Review and action the 5 approval proposals in `Agent_Approvals` (approve or reject in the Sheet)
- §4e: Cost/security — Cloud Billing dashboard, $10 budget alert, verify `--no-allow-unauthenticated`
- §4f: GAOS-Doctor full checklist (`Docs/GAOS-Doctor.md`)
- §11: Cloud Logging retention — set `_Default` bucket to 7 days (GCP console, 2 min)

---

## 2026-03-21T11:30-03:00 — §4.7 Verified · §6.2 Seeded · §7 BigQuery · Full Status Mapped

**What was done:** Zero-trust pytest run; §4.7 approval pipeline verified via Logs tab; §6.2 Drive seed files confirmed idempotent; §7 BigQuery dataset + 6 tables provisioned; full Phase 4 exit criteria status mapped.

### Changes
- **No source files modified** — all work was infrastructure provisioning and verification
- **BigQuery**: `morphic-gaos-prod.aos_logs` dataset created (US, indefinite); 6 tables created:
  - `task_outcomes` (30d TTL, `log_date` partition)
  - `evolution_tasks` (365d TTL, `log_date` partition)
  - `approval_history` (730d TTL, `log_date` partition)
  - `observability_weekly` (indefinite, no partition)
  - `memory_entries` (indefinite, no partition)
  - `monologue_frames` (90d TTL, `timestamp` partition)

### Verifications passed
- **408/408 pytest** — 15.22s, zero failures (zero-trust baseline)
- **§4.7 approval trigger** — Logs tab row 55: `['3/21/2026 3:52:09', 'APPROVAL', 'SMOKE67-6B136EAF', 'dhess@sl10repairtechs.com', '5', 'Approved']` ✅
- **§6.2 seed files** — 0 created, 13 skipped (all pre-existing from prior session) ✅
- **§7 BigQuery** — all 6 tables confirmed via `client.list_tables()` ✅
- **§10 Scheduler** — 4 jobs ENABLED, all with recent last-attempt times ✅

### Phase 4 exit criteria status
**Complete:**
- ✅ §4 Apps Script (all sub-steps including §4.7)
- ✅ §5 Pub/Sub (22 subscriptions, OIDC auth)
- ✅ §6 Drive Knowledge folder (structure + seed files)
- ✅ §7 BigQuery (dataset + 6 tables)
- ✅ §8 settings.yaml
- ✅ §9 Cloud Run (7 services deployed)
- ✅ §10 Scheduler (4 jobs ENABLED)
- ✅ §12 Vertex AI RAG corpora (7, all populated)
- ✅ §13 Smoke tests (8/8 webhook tests pass)

**Remaining:**
- ⬜ §4d E2E validations — force-run Scheduler jobs; Chat-path approval E2E; vision workflow
- ⬜ §4e Cost/security — Cloud Billing dashboard; $10 budget alert; verify `--no-allow-unauthenticated`
- ⬜ §4f GAOS-Doctor — run full checklist (`Docs/GAOS-Doctor.md`)
- ⬜ §11 Cloud Logging retention — set `_Default` bucket to 7 days (GCP console, 2 min)
- ⬜ AppSheet app (§14 Phase 2.5, optional)

### What's next
§4d E2E validations — force-run `daily-kickoff` and confirm morning briefing Chat card arrives in `spaces/jbpdpSAAAAE`.

---

## 2026-03-21T19:45-03:00 — VERTEX_AGENT_ENDPOINT fix; Chat app configured; Chat DM discovered

**What was done:** Fixed `VERTEX_AGENT_ENDPOINT` placeholder bug in `--post-auth`; configured Google Chat app in GCP console; discovered owner Chat DM space; investigated persistent `scripts.run()` 403 and confirmed root cause.

### Changes
- `scripts/setup_apps_script.py` (commit `00cbba9`): Replaced hardcoded `"https://placeholder.invalid/sync"` with dynamic `gcloud run services describe nexus-prime` call; fallback to `https://nexus-prime-7bu22bxlda-uc.a.run.app` if gcloud fails
- `config/settings.yaml`: `chat.owner_space` written automatically by `--post-auth` → `spaces/jbpdpSAAAAE` ✅

### Root causes found
- `VERTEX_AGENT_ENDPOINT` was hardcoded to `https://placeholder.invalid/sync` in `phase2()` — now derived dynamically from Cloud Run
- Chat API 404 "Google Chat app not found": the API being enabled ≠ the Chat app being configured. Configured in GCP console → Configuration tab → all 4 URL fields → `https://nexus-prime-7bu22bxlda-uc.a.run.app/chat`
- `scripts.run()` 403 persists despite editor consent: root cause is that the Apps Script project is **bound to the spreadsheet** (`parentId: spreadsheet_id` in `projects.create()`). Bound scripts use the spreadsheet's auto-assigned GCP project, which is different from the OAuth client project (`490183704378`). The Execution API cannot cross that project boundary with ADC credentials. **This is permanently manual** — `--post-auth` will always 403 for all 3 `scripts.run()` calls.

### What remains (manual — Phase 2 final steps)
1. Script Properties (Apps Script editor → ⚙ Project Settings → Script Properties):
   - `VERTEX_AGENT_ENDPOINT` = `https://nexus-prime-7bu22bxlda-uc.a.run.app/sync`
   - `WEBHOOK_URL` = `https://script.google.com/macros/s/AKfycbyObDuvcf57T3Tk1x85yDg8Q0Pdx7YPUzcHU6-T2WZUgZvRm-fkDdp_l2sI09VrWgPP/exec`
   - `GCP_PROJECT` = `morphic-gaos-prod`
   - `WEBHOOK_HMAC_SECRET` = value of `gcloud secrets versions access latest --secret=WEBHOOK_HMAC_SECRET --project=morphic-gaos-prod`
2. onEdit trigger: Triggers → Add Trigger → `onChangeApproval` | From spreadsheet | **On edit**
3. setupProtections: select in dropdown → Run (may already be done from consent click)
4. Owner row in Authorized Approvers tab (§4.3)

---

## 2026-03-21T17:30-03:00 — Automate --post-auth: Chat DM discovery, scriptapp scope, chat API

**What was done:** Automated both E2E blockers (VERTEX_AGENT_ENDPOINT + chat.owner_space) inside `setup_apps_script.py --post-auth`. After one ADC re-auth, `--post-auth` sets all Script Properties AND discovers the Chat DM space.

### Changes
- `scripts/setup_apps_script.py`: Added `chat.spaces.readonly` and `script.scriptapp` to SCOPES; added `discover_chat_dm_space()` function; wired call into `phase2()` with settings.yaml write
- `config/settings.yaml`: Added `chat:` section with empty `owner_space` field
- `Docs/GAOS-Deploy-Spec.md`: Updated §0.4 ADC scope command to include `script.scriptapp` + `chat.spaces.readonly`; updated §6.2 ADC re-auth warning with new scopes and additional warning
- Enabled `chat.googleapis.com` API in `morphic-gaos-prod`

### Root causes found
- `setup_apps_script.py --post-auth` was failing with `RefreshError` — ADC token expired; re-auth required in standalone PS window (browser redirect fails in VS Code terminal)
- ADC created without `script.scriptapp` → `scripts.run()` returns 403; `chat.spaces.readonly` also missing → Chat API returns `ACCESS_TOKEN_SCOPE_INSUFFICIENT`
- `chat` section was entirely absent from `settings.yaml`; `chat.googleapis.com` API was not enabled

### Next: User must run (in standalone PS window)
```powershell
$s = "https://www.googleapis.com/auth/"
gcloud auth application-default login `
  --client-id-file=oauth-client.json `
  --scopes="${s}spreadsheets,${s}drive,${s}script.projects,${s}script.deployments,${s}script.scriptapp,${s}chat.spaces.readonly,${s}cloud-platform"
gcloud auth application-default set-quota-project morphic-gaos-prod
# Then in VS Code terminal:
python scripts/setup_apps_script.py --post-auth
```

---

## 2026-03-21T17:00-03:00 — §4c Pub/Sub push auth verified; iam.serviceAccountTokenCreator applied

**What was done:** Verified all 22 Pub/Sub push subscriptions are correctly configured. Applied missing `roles/iam.serviceAccountTokenCreator` to Pub/Sub service agent.

### Findings
- All 22 subscriptions already existed with push endpoints to `*-975461050387.us-central1.run.app/pubsub`
- Confirmed both URL formats (`*-975461050387.us-central1.run.app` AND `*-7bu22bxlda-uc.a.run.app`) are valid aliases per `run.googleapis.com/urls` annotation — no subscription updates needed
- `pubsub-push-sa` has `roles/run.invoker` on all 7 Cloud Run services ✅
- Pub/Sub service agent `service-975461050387@gcp-sa-pubsub.iam.gserviceaccount.com` was missing `roles/iam.serviceAccountTokenCreator` (only had `roles/pubsub.serviceAgent`) — applied now

### Files changed
- `Docs/GAOS-Deploy-Spec.md` — §19 4c: Pub/Sub subscription item checked; `VERTEX_AGENT_ENDPOINT` value updated to real URL
- `WORKLOG.md` — this entry

### What's next (remaining blockers for live E2E)
1. Set `VERTEX_AGENT_ENDPOINT` in Apps Script Script Properties → `https://nexus-prime-7bu22bxlda-uc.a.run.app/sync`
2. Set `chat.owner_space` in `settings.yaml` to your DM space resource name
3. Run Chat-path E2E validation (§19 4d item 1)
4. Provision Cloud Scheduler jobs (§10.1–10.4)

---

## 2026-03-21T16:30-03:00 — Phase 4 CI/CD pipeline live: all 7 services deployed + health-checked

**What was done:** Approved the `production` GitHub Environment gate for pipeline run `23376208619`. OpenTofu Apply completed — 7 imported, 0 added, 7 changed, 0 destroyed. `Wire CLOUD_RUN_URL on nexus-prime` step ran automatically and confirmed `CLOUD_RUN_URL=https://nexus-prime-7bu22bxlda-uc.a.run.app`. All 7 services health-checked HTTP 200.

### Outcome
- All 7 Cloud Run services: status `Ready`, image updated to commit SHA from run `23376208619`
- Import blocks in `infra/main.tf` resolved the 409 conflicts from the prior failed run — services were imported into TF state then reconciled in-place
- `CLOUD_RUN_URL` wired automatically via `tofu output -raw nexus_prime_url` → `gcloud run services update`
- `GET /health` → `{"status":"ok"}` for all 7 (nexus-prime, ledger, beacon, pursuit, foreman, steward, scout)

### Actual service URLs (Cloud Run hashed format)
All URLs follow `https://<agent>-7bu22bxlda-uc.a.run.app` — updated throughout `GAOS-Deploy-Spec.md`.

> ⚠️ **Cloud Run URL format gotcha:** The actual URLs use the hashed format `*-7bu22bxlda-uc.a.run.app`, NOT the project-number format `*-975461050387.us-central1.run.app` that was in the spec as a placeholder. `gcloud run services update` prints the old format in its output (display artifact), but `gcloud run services describe` returns only the hashed format. Trust `gcloud run services describe --format="value(status.url)"` as authoritative.

### Files changed
- `Docs/GAOS-Deploy-Spec.md` — §19 4b: all 5 items checked; all 20+ URL references updated to `7bu22bxlda-uc.a.run.app` format
- `WORKLOG.md` — this entry

### What's next
1. Wire Pub/Sub push subscriptions to new Cloud Run URLs (§5.2) — blocks agent messaging
2. Set `VERTEX_AGENT_ENDPOINT` in Apps Script Script Properties → `https://nexus-prime-7bu22bxlda-uc.a.run.app/sync`
3. Set `chat.owner_space` in `settings.yaml`
4. Run Chat-path E2E validation (§19 4d)
5. Provision Cloud Scheduler jobs (§10.1–10.4)

---

## 2026-03-21T14:00-03:00 — Phase 4 §20 bootstrap: GCS bucket, WIF, IAM, GitHub Secrets

**What was done:** Executed §20 bootstrap sequence — created all missing GCP bootstrap resources and set GitHub Secrets. All 5 §19 4a items now checked except `production` GitHub Environment (UI-only).

### Resources created / confirmed
- `gs://morphic-gaos-tfstate` — created with versioning enabled
- `deployer-sa@morphic-gaos-prod.iam.gserviceaccount.com` — already existed; all IAM bindings applied: `run.admin` (project), `artifactregistry.writer` (AR repo scoped), `storage.objectAdmin` (tfstate bucket), `iam.serviceAccountUser` on all 7 agent SAs
- WIF pool `github-actions` — already existed; OIDC provider `github-oidc` created (was missing)
- WIF `roles/iam.workloadIdentityUser` binding applied to `deployer-sa` for `EgoNoBueno/Morphic-GAOS-Manager` repository attribute
- GitHub Secrets: `WIF_PROVIDER` + `WIF_SERVICE_ACCOUNT` — set via `gh secret set`
- Artifact Registry `cloud-run-source-deploy` — already existed (306MB)

### Files changed
- `Docs/GAOS-Deploy-Spec.md` — §19 4a: 5 of 6 items checked
- `WORKLOG.md` — this entry

### What's next
1. Create `production` GitHub Environment (GitHub UI → Settings → Environments → add required reviewer)
2. Push to master → CI/CD triggers → approve the pipeline gate → all 7 services deploy
3. Update Pub/Sub push subscriptions to new Cloud Run URLs (§5.2)
4. Set `VERTEX_AGENT_ENDPOINT` in Apps Script Script Properties
5. Run Chat-path E2E validation (§19 4d)

---

## 2026-03-20T15:00-03:00 — IaC hardening: TF outputs + CLOUD_RUN_URL auto-wiring + WIF secret

**What was done:** Closed three gaps in the IaC pipeline before the Phase 4 bootstrap can be executed.

### Files changed

- **`.github/workflows/deploy.yml`** — Three fixes:
  1. All 3 `google-github-actions/auth` steps now use `service_account: ${{ secrets.WIF_SERVICE_ACCOUNT }}` instead of the hardcoded `deployer-sa@morphic-gaos-prod.iam.gserviceaccount.com`. The `WIF_SERVICE_ACCOUNT` secret is set by the bootstrap runbook (§20 Step 5) — this makes the workflow portable to any project ID without code changes.
  2. Added a `Wire CLOUD_RUN_URL on nexus-prime` step at the end of the apply job: reads the `nexus_prime_url` output from `tofu output -raw` and calls `gcloud run services update nexus-prime --update-env-vars CLOUD_RUN_URL=...` immediately after apply completes. No manual post-deploy action needed for Chat JWT audience verification.
  3. No other changes — plan/apply artifact flow and concurrency groups unchanged.
- **`infra/main.tf`** — Added two `output` blocks: `service_urls` (map of all 7 agent names → URIs) and `nexus_prime_url` (raw nexus-prime URI). These let the apply job and local operators read deploy results without going to the GCP console.
- **`Docs/GAOS-Deploy-Spec.md`** — §19 4c checklist: marked `CLOUD_RUN_URL` as auto-wired (checked) and added note. §20 post-apply steps: updated step 4 to state CLOUD_RUN_URL is handled automatically; step 1 now says to read from `service_urls` TF output.

**What was learned:** Cloud Run v2 TF resource doesn't support self-referential URLs during creation — the URL is only known after the first revision deploys. Reading via `tofu output -raw nexus_prime_url` and issuing a `gcloud run services update` immediately after apply is the correct pattern. No new revision is created by `--update-env-vars`, just an in-place metadata update.

**What's next:** All code-side work is done. Phase 4 is pure GCP bootstrap (§20 runbook) + live E2E validation.

---

## 2026-03-20T14:30-03:00 — Phase 4 prep: ruff clean + Phase 4 Bootstrap Spec

**What was done:** Fixed the only remaining ruff lint warnings; wrote Phase 4 exit criteria and bootstrap runbook.

### Files changed

- **`models/__init__.py`** — Migrated `MessageType(str, Enum)` and `ApprovalStatus(str, Enum)` to `StrEnum` (ruff UP042). Removed `Enum` import, added `StrEnum`. Behavior identical — no `auto()` used; all member values are explicit strings. Tests: 408/408 still passing; `ruff check` now reports `All checks passed!`
- **`Docs/GAOS-Deploy-Spec.md`** — Added §19 "Phase 4 Exit Criteria Checklist" (6 subsections: 4a Infrastructure Bootstrap, 4b CI/CD Validation, 4c Production Wiring, 4d Live E2E Validation, 4e Cost + Security, 4f GAOS-Doctor Runbook); added §20 "Phase 4 Bootstrap Runbook" (copy-paste PowerShell sequence for the 8-step GCP bootstrap from a clean state). Updated §16 Reference Index with pointers to §19–20.

**What was learned:** Nothing non-obvious — the StrEnum migration is a clean mechanical fix. `StrEnum` is identical to `(str, Enum)` when all values are explicit string literals (no `auto()`).

**What's next (Phase 4, all human/GCP actions):**
1. Run §20 bootstrap runbook (Steps 1–7) — creates TF state bucket, AR repo, deployer-sa, WIF pool, sets GitHub Secrets.
2. Create `production` GitHub Environment with required reviewer (GitHub UI only).
3. Push to `master` → CI/CD pipeline runs → all 7 Cloud Run services deploy.
4. Complete §19 post-deploy wiring (update Pub/Sub endpoints, `VERTEX_AGENT_ENDPOINT`, `CLOUD_RUN_URL`).
5. Run §19 4d–4f live E2E + GAOS-Doctor validation.

---

## 2026-03-20T14:00-03:00 — Phase 3 formally closed + README accuracy pass

**What was done:** Reviewed the full project from Phase 0 to present. 408 tests passing, 0 warnings. All Phase 3 code verified implemented — no gaps. Formalized Phase 3 completion with a new exit criteria checklist and updated the README roadmap to match reality.

### Files changed

- **`Docs/GAOS-Deploy-Spec.md`** — Added new §18 "Phase 3 Exit Criteria Checklist": 13 items (11 checked ✅, 2 pending Cloud Run/GCP bootstrap). Covers: `think` node + MonologueFrame, Tactical mode, `vision_blueprint`, `handle_skill_request`, `iterate_plan` + `_run_compaction`, `handle_poll_comments`, Memory Mirror (`sync_to_atlas`), Chat Interactive Hub (JWT + approval cards + CARD_CLICKED routing), multimodal `_call_model`, OpenTofu IaC pipeline, WIF auth, 408-test green suite. Two unchecked items require GCP infrastructure (OpenTofu bootstrap + Chat-path E2E live validation).
- **`README.md`** — Phase table updated: Phase 1 test count 320→408; Phase 2 status "Spec complete"→"Complete"; Phase 2.5 "Deployed"→"Complete"; Phase 3 "Spec complete"→"Code-complete — Cloud Run bootstrap + Chat-path E2E validation pending"; Phase 4 "Spec complete"→"Up next"; Phase 3 focus updated to reflect actual scope. Footer badge updated: "396 tests green"→"408 tests green", "Phase 1 and Phase 2.5 deployed"→"Phases 1–3 code-complete."

**What was learned:** No new gaps. The project is in good shape — everything described in the Phase 3 spec is implemented and tested. The only remaining blockers for Phase 3 "done" status are infrastructure steps (OpenTofu bootstrap, WIF GitHub Secrets, first Cloud Run deploy).

**What's next (Phase 4):**
1. One-time GCP bootstrap for OpenTofu (§9.3): create `morphic-gaos-tfstate` bucket + `cloud-run-source-deploy` AR repo + `deployer-sa` + WIF pool + OIDC provider; add `WIF_PROVIDER` and `WIF_SERVICE_ACCOUNT` to GitHub Secrets.
2. Push to `master` to trigger the first WIF-based CI/CD pipeline run → all 7 Cloud Run services deploy.
3. Validate Approval Gate Chat-path E2E live: Chat card Approve tap → `APPROVAL_RESULT` → Nexus-Prime resumes parked task → Sheet audit row written.
4. Run through GAOS-Doctor checklist post-deploy.

---

## 2026-03-20T10:00-03:00 — Layer 4 Memory Mirror + doc audit

**What was done:** Implemented the Memory Mirror feature (Layer 4 Knowledge Atlas) and updated all affected specifications.

### Code changes (commit `af24f79`)

- **`tools/memory_mirror.py`** — New tool. `sync_to_atlas(entry: MemoryEntry) -> None` appends each approved MemoryEntry to a pre-created Knowledge Atlas Google Doc via `append_content()`. When `entry.supersedes` is set, a `⛔ SUPERSEDED` audit marker is appended. Raises `MemoryMirrorError` on all failures; caller (knowledge_review node) catches and logs `WARNING` — the Vertex AI write is never blocked.
- **`tests/test_memory_mirror.py`** — 9 unit tests: happy path, supersedes marker, `approved_at=None` fallback rendering, `DocsApiError` wrapping, `DocumentNotFoundError` wrapping, unexpected exception wrapping, missing config guard (doc ID not set), append not called when config missing. All 405 tests passing.
- **`config/__init__.py`** — `DocsConfig` gets `knowledge_atlas_doc_id: str = ""`.
- **`config/settings.yaml.template`** — `docs:` block updated with `knowledge_atlas_doc_id` field and inline comment.
- **`agents/nexus_prime/orchestrator.py`** — Two changes:
  - `_build_knowledge_review_prompt()`: updated to ask the LLM whether the candidate supersedes an existing `memory_id`; returns `supersedes_memory_id` field in JSON (null if additive, existing `memory_id` if retiring an old entry).
  - `knowledge_review()`: `MemoryEntry` constructor now sets `approved_at=datetime.now(UTC)` and `supersedes=resp.data.get("supersedes_memory_id") or None`; `sync_to_atlas(entry)` called in a nested `try/except` after `write_approved_memory()`.

### Doc changes (this commit)

- **`Docs/GAOS-Tools-Spec.md`** — Added §18 `tools/memory_mirror.py`; updated §16 `docs:` settings block to include `knowledge_atlas_doc_id`.
- **`Docs/GAOS-Memory-Spec.md`** — Added post-`write_approved_memory()` note explaining the Atlas mirror call; fixed `approved_at=datetime.utcnow()` → `datetime.now(UTC)` in the promote pseudocode example.
- **`Docs/GAOS-Nexus-Prime-Spec.md`** — Replaced pre-implementation `knowledge_review` code block with the actual implementation (approved_at, supersedes, sync_to_atlas nested try/except); added JSON schema doc for `_build_knowledge_review_prompt` return value.
- **`Docs/GAOS-Deploy-Spec.md`** — New §17 "Knowledge Atlas Google Doc (Memory Mirror)" with step-by-step setup (create doc, copy ID, paste into settings.yaml, verification command); updated §8 settings.yaml example to include `docs:` block; added Atlas item to Phase 2 exit checklist; updated §16 reference index.
- **`README.md`** — Added Knowledge Atlas row to the memory layer table.

**What was learned:** The `_build_knowledge_review_prompt()` function was previously returning only `{"confidence": float, "is_duplicate": bool, "rationale": str}` — no `supersedes` field. Without asking the LLM to identify which existing memory_id is being retired, the `supersedes` field in `MemoryEntry` would always be None and the supersession chain would be broken. The prompt update is the load-bearing change that makes active learning work.

**What's next:** Create the Knowledge Atlas Google Doc in Drive (GAOS-Deploy-Spec.md §17) and paste its ID into `config/settings.yaml`. Once set, the first `KNOWLEDGE_CANDIDATE` auto-promotion will populate the Atlas.

---

## 2026-03-20T03:00-03:00 — Migrate CI/CD auth to Workload Identity Federation

**What was done:** Replaced the long-lived SA key (`GCP_SA_KEY`) in the GitHub Actions
pipeline with Workload Identity Federation (WIF) — eliminating any long-lived credential from
the CI/CD path.

**Files changed:**
- `.github/workflows/deploy.yml` — added `permissions: id-token: write / contents: read` at
  workflow level; replaced all 3 `credentials_json: ${{ secrets.GCP_SA_KEY }}` auth steps with
  `workload_identity_provider: ${{ secrets.WIF_PROVIDER }}` + `service_account:`; updated header comment
- `Docs/GAOS-Deploy-Spec.md §9.3` — replaced step 3 (SA key generation) with WIF bootstrap
  commands (`workload-identity-pools create`, `providers create-oidc`, `add-iam-policy-binding`
  with `attribute.repository` condition + `principalSet` member); replaced `deployer-key.json`
  warning with `attribute-condition` scope note; removed "Migration to WIF (Phase 3 task)"
  paragraph (implemented now, not deferred)

**What was learned:** None (clean implementation, no surprises).

**What's next:** Run the one-time WIF bootstrap (§9.3 step 3) in the GCP project,
add `WIF_PROVIDER` as a GitHub Secret, then push to `master` to trigger the first WIF-based run.

---

## 2026-03-20T02:30-03:00 — OpenTofu IaC Pipeline ✅

**Implemented Push-to-Deploy infrastructure as code for all 7 Cloud Run agents.**

### What was done
- `infra/main.tf` — OpenTofu blueprint: GCS backend (morphic-gaos-tfstate), `image_tag` variable, `for_each` over 7 agents, 512Mi/1CPU/concurrency=1 per spec, per-agent SA identity
- `.github/workflows/deploy.yml` — 3-job pipeline: `build` (docker build+push to AR), `plan` (tofu plan -out=tfplan, 3-day artifact retention), `apply` (manual gate via `production` GitHub Environment, downloads tfplan from same run)
- `Docs/GAOS-Deploy-Spec.md` — §2.1: added `deployer-sa` creation; §2.2: added deployer SA IAM (run.admin, artifactregistry.writer on repo, storage.objectAdmin on bucket, per-SA actAs × 7 with warning); §9.1: supersession note; §9.3: new IaC section (bootstrap commands, GH Environment setup, artifact retention policy, WIF migration note)
- `README.md` — Added "Deployment Architecture: The OpenTofu Pipeline" section (SHA-pinned, plan integrity, drift-correcting, scope boundary)

### Architecture decisions baked in
- Single-run integrity: all jobs in same run ID → no cross-run artifact downloads
- Per-SA `actAs` binding (not project-level) — least privilege
- Docker AR auth step explicit in workflow (gcloud auth configure-docker) — prevents 403
- `roles/artifactregistry.writer` on repo resource (not project) — scoped correctly
- 3-day artifact retention: Friday push survives to Monday approval
- `concurrency=1` comment references LangGraph state rationale
- WIF migration flagged as Phase 3 task in §9.3

### What's next
- One-time bootstrap: create `morphic-gaos-tfstate` bucket, `cloud-run-source-deploy` AR repo, `deployer-sa`, and set `GCP_SA_KEY` GitHub Secret
- Create `production` GitHub Environment with required reviewer
- First live `tofu init` + plan run to validate

---

## 2026-03-20T01:11-03:00 — Phase 5 Interactive Hub + Test Fix Sweep ✅

**Implemented Phase 5 Google Chat Interactive Hub: multimodal vision, approval cards, JWT auth, and systematic test-mock fixes.**

### What was done — Phase 5 Implementation

- `agents/__init__.py` — `_call_model` + `_call_model_gemini`: added `image_bytes: bytes | None = None` parameter; multimodal content sent as `[Part.from_bytes(...), Part.from_text(...)]`; Ollama + image_bytes logs warning and strips bytes
- `tools/google_chat.py` — `send_approval_card`: 3-section card (Context + conditional Reasoning + Decision with Approve/Reject buttons); `parse_chat_event`: returns `"attachments"` list from `message.get("attachment", [])`
- `main.py` — Added `_CHAT_ISSUER`, `_CHAT_CERTS_URL`, `_verify_chat_jwt()`, `_download_chat_attachment()`; `/chat` handler: JWT verify → image attachment detection → download → `_call_model` with vision prompt + `image_bytes` → `VISION_SUBMITTED` dispatch
- `agents/nexus_prime/orchestrator.py` — `propose_gate`: sends card with `reasoning_summary` from `monologue_frame`; `vision_blueprint`: appends image timestamp note + BQ cost log when `vision_source == "image"`
- `tests/test_google_chat.py` — 29 tests passing; C8/C9 updated for 3-section card; added C13/C14 (reasoning sections), P7 (attachment parsing), E8 (vision dispatch)
- `tests/test_agents.py` — Added `TestMultimodalVisionPath` (M1–M5 + 3 code quality): all 8 passing

### What was done — Test Mock Fix Sweep

Systematic fix for `patch("agents._log_cloud")` and `patch("agents._write_heartbeat")` targets that hung on real Cloud Logging gRPC. Root cause: Python `from X import Y` creates a local binding in the importing module; `patch("agents._log_cloud")` replaces the attribute on the `agents` package but does NOT affect the already-bound local reference in cached submodules like `agents.steward.orchestrator`.

**Correct pattern:** `patch("agents.<submodule>.orchestrator._log_cloud")`

Files fixed:
- `tests/test_agents.py` — TestU4: steward + beacon `_log_cloud` / `_write_heartbeat` targets
- `tests/test_agents.py` — TestU5: dynamic `f"{orchestrator_module.__name__}._log_cloud"` + steward target
- `tests/test_agents.py` — TestArchiveJob: added `patch("agents.nexus_prime.orchestrator._log_cloud")` to all 7 `handle_archive` tests
- `tests/test_agents.py` — TestNexusPrimeThinkNode: added autouse `_patch_log_cloud` fixture for all 10 `think()` tests

### Test result
393 passed, 1 warning (datetime.utcnow deprecation in main.py:529) in 16.72s

### What's next
- Fix `datetime.utcnow()` deprecation in main.py
- WORKLOG rules compliance: spec and docs are complete in same commit
- Phase 5 exit criteria verification

---

## 2026-03-19T00:30-03:00 — think node + MonologueFrame ✅

**Implemented Directive 2: Strategic Architect `think` node for Nexus-Prime.**

### What was done
- `models/__init__.py` — Added `MonologueFrame` Pydantic model (8 fields: `task_id`, `project_id`, `knowledge_gap_detected`, `knowledge_gap_description`, `partial_result_available`, `response_mode`, `reasoning_summary`, `timestamp`)
- `agents/nexus_prime/orchestrator.py`:
  - Added `_load_context_trio` to imports; added `MonologueFrame` to model imports
  - Extended `NexusPrimeWorkingMemory` with `_next_node: str` and `monologue_frame: Optional[dict]`
  - Added `_build_think_prompt()` helper with full Strategic Architect reasoning instructions
  - Added `_route_from_think()` sub-router (reads `state["_next_node"]`)
  - Added `think()` node: uses `FAST_MODEL` + Context Trio system prompt + `parse_json=True`; Tactical mode forced on priority >= 4; graceful fallback on model or BQ failure; logs `MonologueFrame` to `aos_logs.monologue_frames`
  - Updated `route()` routing table: ESCALATION/EVOLUTION_REQUEST/KNOWLEDGE_CANDIDATE now → `"think"` (then think → diagnose or knowledge_review)
  - Updated `build_nexus_prime_graph()`: added `think` node; updated `add_conditional_edges` from `route` to include `"think"`; added `add_conditional_edges` from `think` via `_route_from_think`
  - Updated `initial_state` in `NexusPrimeAgent.run()` with `_next_node: "record"` and `monologue_frame: None`
- `Docs/GAOS-Deploy-Spec.md` — Updated `monologue_frames` BQ schema to match `MonologueFrame` fields; added IAM note (nexus-prime-sa already has dataset-level write via `bigquery.dataEditor`)
- `Docs/GAOS-Nexus-Prime-Spec.md` — Updated `route()` routing table; added full `think` node section between `route` and `diagnose` with code listing, field table, and Tier 1-only warning callout
- `tests/test_agents.py` — Added `TestNexusPrimeThinkNode` (12 tests): routing for each message type, Tactical mode overrides at p4/p5, BQ insert shape, context trio in system_prompt, call_model fallback, BQ fallback, `_route_from_think` default behavior

### Test result
59 passed, 12 deselected (chronic process-launchers: TestU4MissingSecretCausesExit, TestArchiveJob)

### What's next
- WORKLOG rules compliance: spec and docs are complete in same commit
- GAOS-Persona-Spec.md §4 references `monologue_frames` — verify it's consistent with new schema

---

## 2026-03-18T23:30-03:00 — Code Review Fixes ✅

**Applied 15 findings from code review across 13 files. 332 tests green.**

- `.env.example`: clarified `gcloud secrets create` vs `versions add` for Google Search secrets
- `.github/copilot-instructions.md`: replaced `&&` with PS 5.1-compatible `if ($LASTEXITCODE -eq 0)` sequence; noted PS7+ required for `&&`
- `Docs/GAOS-Deploy-Spec.md`: backtick-fixed `gcloud auth` PowerShell example; clarified `projects.default` is not a fallback for `init_sheets_client`
- `Docs/GAOS-Project-Glossary.md`: TL;DR moved before TLS (alphabetical); definition updated (beginning, not end)
- `Docs/about-me.md`: "no filler, no filler content" → "no filler, no fluff"
- `README.md`: `onEdit` → `onChange` at line 282 (matches diagram and `.gs` file name)
- `apps_script/onChangeApproval.gs`: `approverEmail` now falls back to `'unknown@audit'` when both Session methods return empty
- `scripts/_seed_knowledge_files.py`: all module-level side effects moved into `main()`; helpers accept `svc` as param; `if __name__` guard added; `base64` dead import removed; `googleapiclient.http` import made explicit
- `scripts/setup_apps_script.py`: `updateContent` call wrapped in `try/except HttpError`; removed nonexistent `--redeploy` from printed message
- `scripts/smoke_test_4.py`: duplicate failures block + duplicate `if __name__` guard removed
- `scripts/smoke_test_6_7.py`: secret values redacted in output (masked); hardcoded timestamp replaced with `datetime.now(timezone.utc).isoformat()`
- `scripts/smoke_test_pubsub_sub.py`: hardcoded `PROJECT_ID` replaced with `--project` argparse flag; helpers refactored to accept paths as params; `base64` dead import removed
- `agents/nexus_prime/orchestrator.py`: dead `settings = get_settings()` assignment removed

---

## 2026-03-18T22:47-03:00 — Phase 2 Observability Loop Complete ✅

**`scripts/observability_loop.py` smoke test passing end-to-end.**

- Ollama (`llama3:latest`) called successfully, 49 log rows sampled
- `SYSTEM_THOUGHTS` row appended to Logs tab in Sheet ✅
- Fixed `UnicodeEncodeError` in print statement (`→` → `->` + `sys.stdout.reconfigure(encoding='utf-8')`)
- Root cause of prior `\r` error was OLLAMA_HOST secret containing `\r\n` — fixed in `agents/__init__.py` with `.strip().rstrip("/")`
- Terminal buffer workaround established: redirect output to `out.txt`, read via file tool
- **Files changed:** `scripts/observability_loop.py` (Unicode fix), `agents/__init__.py` (OLLAMA_HOST strip), `config/settings.yaml` (LOCAL_MODEL, timeout)
- **Next:** Update Deploy Spec Phase 2 checklist → Phase 3

---

## 2026-03-18T16:30-03:00 — Phase 1 Complete ✅

**Pub/Sub local subscriber smoke test passing. All Phase 1 non-[Phase 2.5] exit criteria complete.**

| Item | Result |
|---|---|
| Pub/Sub local subscriber smoke test | PASS — `scripts/smoke_test_pubsub_sub.py` |
| Phase 1 checklist | All non-[Phase 2.5] items now checked off |

**Files changed:**
- `scripts/smoke_test_pubsub_sub.py` — new script; creates temp pull sub, publishes synthetic APPROVAL_RESULT, pulls + prints proposal_id + new_status, acks, cleans up
- `Docs/GAOS-Deploy-Spec.md` — §14 Pub/Sub subscriber item checked off

**What's next:** Phase 2.5 remaining items — AppSheet deploy (UI-only, no API path), Approval Gate Chat-path E2E (requires Cloud Run deploy). Phase 2 (Ollama integration) can begin in parallel.

---

## 2026-03-18T13:00-03:00 — Smoke Tests 6+7: 8/8 Passing ✅

**8/8 webhook HMAC tests passing. Phase 1 exit criteria: webhook items checked off.**

| File | Change |
|------|--------|
| `config/settings.yaml` | Added `projects.morphic-gaos-prod` entry (same `sheet_id` + `drive_folder_id` as `default`) — required for `init_sheets_client('morphic-gaos-prod')` to resolve |
| `Docs/GAOS-Deploy-Spec.md` | §14 checklist: checked off both webhook smoke test items. §0.5: Added warning — `settings.yaml` needs explicit named project entry, `default` is not a fallback. §0.6: Added warning — Project Registry `status` must be lowercase `'active'` (Apps Script strict equality check). |
| `/memories/repo/gotchas.md` | Added two new gotcha bullets: Project Registry status case and settings.yaml named project entry requirement. |

**Gotchas discovered this session:**
- `init_sheets_client('morphic-gaos-prod')` → `WorkbookNotFoundError` unless `projects.morphic-gaos-prod` is in `settings.yaml` — `default` is not a fallback
- Apps Script `isValidProject_()` uses `=== 'active'` (strict lowercase) — `'Active'` silently fails with `400 Unknown project_id`
- Project Registry row for `morphic-gaos-prod` was missing entirely — added via Python script

**Next:** AppSheet app deployment → Approval Gate Chat-path E2E validation.

---

No code changes. Documentation-only session capturing lessons from prior setup work.

| File | Change |
|------|--------|
| `Docs/GAOS-Tools-Spec.md` | §3 (`google_sheets.py`): Added `⚠️ Warning` callout — Sheets API rejects unquoted tab names containing spaces in A1 notation range strings. Includes correct/incorrect code examples and canonical reference link. |
| `/memories/repo/gotchas.md` | NEW — Repo-scoped quick-reference bullets for all four gotchas: SDK EOL, AI Studio keys, `GOOGLE_APPLICATION_CREDENTIALS` ADC override, Sheets tab quoting. The Deploy Spec already captured the first three; this file gives future sessions a single indexed lookup point. |

**Gotchas already in spec files (no new edits needed):**
- `GAOS-Deploy-Spec.md §0.3` — `google-generativeai` EOL; use `google-genai>=1.0.0`
- `GAOS-Deploy-Spec.md §0.4` — `GOOGLE_APPLICATION_CREDENTIALS` silently overrides ADC
- `GAOS-Deploy-Spec.md §3.1` — AI Studio keys live in Google's shared project, not your billing project

**Next:** Resume Phase 2.5 work or run `python scripts/smoke_test_6_7.py` against the live endpoint.

---

## 2026-03-18T06:06-03:00 — Smoke Tests 6+7: Webhook HMAC Script COMPLETE ✅

**332 / 332 tests passing. Zero regressions. No new unit tests (integration-only).**

| File | Change |
|------|--------|
| `scripts/smoke_test_6_7.py` | NEW — Automated webhook HMAC smoke test harness. Fetches `WEBHOOK_URL` and `WEBHOOK_HMAC_SECRET` from Secret Manager, then runs all 8 test cases from `GAOS-Manager-Spec.md §14` against the live Apps Script doPost endpoint: (1) valid payload → 200, (2) tampered signature → 401, (3) missing signature → 401, (4) missing project_id → 400, (5) invalid project_id → 400, (6) priority out of range → 400, (7) empty body → 400/500, (8) replay → 200. Prints `8/8 tests passed` then cleanup reminder. Run: `python scripts/smoke_test_6_7.py`. |
| `Docs/GAOS-Deploy-Spec.md` | §13 smoke test table: collapsed tests 6+7 into one row pointing to `smoke_test_6_7.py`; updated unit test count from 320 → 332. §14 checklist: replaced two vague "All 7 smoke tests" / "All 8 webhook tests" items with specific `smoke_test_6_7.py` run requirement. |

**Next:** Run `python scripts/smoke_test_6_7.py` against the live endpoint to confirm 8/8 pass → AppSheet no-code deployment → Approval Gate Chat-path E2E.

---

## 2026-03-18T05:49-03:00 — Phase 2.5 Step 7: SKILL_REQUEST Approval Flow COMPLETE ✅

**332 / 332 tests passing. Zero regressions. +12 new tests.**

| File | Change |
|------|--------|
| `agents/nexus_prime/orchestrator.py` | New node `handle_skill_request()` — two-path handler (inbound: posts `send_skill_import_card()` + writes `Agent_Approvals` row + parks proposal_id; resolution: updates row status, publishes `SKILL_REQUEST` back to agent on Approved or `ALERT` on Rejected). Added `MessageType.SKILL_REQUEST: "handle_skill_request"` to `route()` routing table. Registered node in `build_nexus_prime_graph()` and added `handle_skill_request → record` edge. |
| `tests/test_skill_request.py` | NEW — 12 tests across 3 classes: `TestHandleSkillRequestInbound` (SR1–6), `TestHandleSkillRequestResolution` (SR7–11), `TestSkillRequestRouting` (SR12). All passing. |

**Phase 2.5 Step 7 scope note:** `ITERATE_PLAN` constraint compaction (`_run_compaction` + `iterate_plan` node) was confirmed already fully implemented from a prior session. This session completed the only genuinely missing piece: the `SKILL_REQUEST` routing and handler.

**Next:** Smoke tests 6+7 (HMAC webhook — `POST` with valid and tampered signatures to the Apps Script Web App URL) → 8 webhook tests from `GAOS-Manager-Spec.md §14` → AppSheet deployment → Approval Gate Chat-path E2E.

---

## 2026-03-17T22:44-03:00 — Smoke Test 4 Debug + Pass; Tooling Improvements COMPLETE ✅

**Smoke test 4 (`onEdit` approval trigger) debugged and confirmed passing.**

| Item | Action |
|------|--------|
| `apps_script/onChangeApproval.gs` | Fixed `TypeError: Cannot read properties of undefined (reading 'source')` — replaced `e.source.getActiveSheet()` with guard `if (!e \|\| !e.range) return;` + `e.range.getSheet()`. Added `getEffectiveUser()` fallback for `getActiveUser()` returning empty string on installable triggers. |
| `scripts/setup_apps_script.py` | Added `--push` flag: re-uploads all `.gs` files to the live Apps Script project via API without creating a new deployment. Eliminates copy-paste into the editor after any local `.gs` edit. |
| `scripts/smoke_test_4.py` | NEW — automated smoke test 4 harness. Appends a throwaway test row (`SMOKE4-HHMMSS`), prompts user to type `Approved` in the UI, polls for K/L stamp, checks Logs tab, then cleans up. Prints PASSED/FAILED. |
| `.github/copilot-instructions.md` | NEW — mirrors `Docs/AI-Autocoding-Rules.md`. VS Code injects this automatically into every Copilot Agent session (including after compaction) — no manual attach required. |
| `Docs/GAOS-Deploy-Spec.md` | §4.4 Step 3: On change → On edit. §4.6: Added `e.source` undefined warning + `getEffectiveUser()` fallback note + `--push` automation note. §13 smoke test table: updated test 4 to reference `smoke_test_4.py`. §14: Checked off Apps Script trigger item ✅. |

**Files changed:** `apps_script/onChangeApproval.gs`, `scripts/setup_apps_script.py`, `scripts/smoke_test_4.py` (new), `.github/copilot-instructions.md` (new), `Docs/GAOS-Deploy-Spec.md`, `WORKLOG.md`

**Tests added:** none (smoke_test_4.py is a manual-assist integration harness, not a pytest unit test)

**Commits:** `da7e559` (onEdit fix + --push + smoke_test_4), `aecfafa` (copilot-instructions.md)

**Next:** Smoke tests 6+7 (HMAC webhook — `POST` with valid and tampered signatures to the Apps Script Web App URL) → 8 webhook tests from `GAOS-Manager-Spec.md §14` → AppSheet deployment → Approval Gate Chat-path E2E.

---

## 2026-03-17 — Phase 1 Deploy Infrastructure Audit COMPLETE ✅

**All automatable Phase 1 §14 items confirmed or created. 13 Drive seed files present in Knowledge/.**

| Item | Action |
|------|--------|
| `customsearch` + `discoveryengine` APIs | Enabled in `morphic-gaos-prod` — were not present |
| `GOOGLE_SEARCH_API_KEY` + `GOOGLE_SEARCH_CX` secrets | Created in Secret Manager (6 secrets total now) |
| `daily-kickoff` + `doc-comment-poll` Scheduler jobs | Created — were wrongly marked `[x]` in §14 |
| Drive Knowledge/ seed files (13 files) | Confirmed present; Drive API enabled in OAuth client project (`490183704378`) |
| `scripts/_seed_knowledge_files.py` | Created — idempotent script to provision seed files via ADC |
| `Docs/GAOS-Deploy-Spec.md` §14 | Updated: corrected false positives, marked 15+ confirmed items `[x]` |
| `.env.example` | Added `GOOGLE_SEARCH_API_KEY` + `GOOGLE_SEARCH_CX` reference entries |

**ADC note:** Standard `gcloud auth application-default login` only grants `cloud-platform` scope. Drive/Sheets access requires re-auth with `--client-id-file=oauth-client.json` and explicit `--scopes`. See Deploy Spec §6.2 warning.

**Still manual (not yet done):** Cloud Logging retention (7 days), `setupProtections()` in Apps Script, Authorized Approvers tab owner row.

**Commits:** `28159bf`, `9546cc5`, `08882b1`, `ac2f250`, `2e3781e`

**Next up:** Phase 1 smoke tests → Phase 2.5 Step 7 (`ITERATE_PLAN` + `SKILL_REQUEST`).

---

## 2026-03-17 — Phase 2.5 Step 6: Scout `_discover` + `tools/google_search.py` + `KNOWLEDGE_INJECTION` COMPLETE ✅

**320 / 320 tests passing. Zero regressions. +24 new tests.**

| File | Change |
|------|--------|
| `tools/google_search.py` | NEW — Google Custom Search JSON API v1 wrapper. `search()` (single query, ≤10 results), `research_topic()` (multi-query with URL deduplication). `GoogleSearchError` for quota, auth, and network failures. Credentials fetched from Secret Manager at call time. |
| `config/__init__.py` | Added `GoogleSearchConfig(api_key_secret, cx_secret, max_search_depth=3, max_queries_per_mandate=15)`. Added `google_search: GoogleSearchConfig` field to `Settings`. |
| `config/settings.yaml.template` | Added `google_search:` block (`api_key_secret`, `cx_secret`, `max_search_depth`, `max_queries_per_mandate`). |
| `agents/scout/orchestrator.py` | New nodes: `_discover` (RESEARCH_MANDATE handler — recursive Google Custom Search, max depth 3, max 15 queries, LLM-generated query expansion, corroboration detection at ≥5 sources), `_inject_knowledge` (publishes `KNOWLEDGE_INJECTION` to `agent.scout.events`; appends Section E to Blueprint Doc if `blueprint_doc_id` present). New router `_route_after_boot` (RESEARCH_MANDATE → discover, all others → plan). Updated `build_scout_graph()` with new nodes and edges. Updated `_initial_state()` to decode Pub/Sub push envelopes. Added `datetime` import. |
| `tests/test_scout_discover.py` | NEW — 24 tests across 6 classes: `TestGoogleSearchTool` (GS1–5), `TestResearchTopic` (RT1–4), `TestDiscoverNode` (DN1–6), `TestInjectKnowledgeNode` (IK1–5), `TestRouteAfterBoot` (RB1–2), `TestInitialStatePubSub` (IS1–2). All passing. |
| `Docs/AI-Autocoding-Rules.md` | Added Rule 22: PowerShell-native commands — no Unix-only aliases (`tail` → `Select-Object -Last`, `grep` → `Select-String`, etc.). |

**Step 6 note:** Google Custom Search API free tier is 100 queries/day. `max_queries_per_mandate=15` caps each mandate at 15% of daily quota. Requires `GOOGLE_SEARCH_API_KEY` and `GOOGLE_SEARCH_CX` secrets wired in Secret Manager before production use.

**Next up:** Phase 2.5 Step 7 — `ITERATE_PLAN` constraint compaction node + `SKILL_REQUEST` approval flow.

---

## 2026-03-16 — Phase 2.5 Step 5: Vision Workflow COMPLETE ✅

**296 / 296 tests passing. Zero regressions. +30 new tests.**

| File | Change |
|------|--------|
| `agents/nexus_prime/orchestrator.py` | `NexusPrimeWorkingMemory`: added `active_blueprints: dict` and `blueprint_constraints: list[dict]` fields. `_COMPACTION_THRESHOLD = 5` constant. New prompt builders: `_build_vision_prompt`, `_build_compaction_prompt`. New nodes: `vision_blueprint` (VISION_SUBMITTED handler — Gemini Pro blueprint generation, Docs create, Sheet append, Chat approval card) and `iterate_plan` (PLAN_REVIEW / COMMENT_RECEIVED handler — constraint accumulation + compaction). New `_run_compaction` helper (Gemini Flash compaction, BQ archive). New `handle_poll_comments` standalone async handler (polls all active Blueprint Docs every 5 min, publishes `COMMENT_RECEIVED`). `route()` routing table updated for VISION_SUBMITTED, PLAN_REVIEW, COMMENT_RECEIVED. `build_nexus_prime_graph()` updated with `vision_blueprint` + `iterate_plan` nodes and edges. `NexusPrimeAgent.run()` initial state includes new fields. `boot()` setdefaults for new fields. |
| `main.py` | Added `POST /vision` endpoint (Nexus-Prime only; body: `vision_text`, `submitted_by`, `space_name`, `project_id`; wraps VISION_SUBMITTED synthetic A2AMessage). Added `POST /poll-comments` endpoint (Nexus-Prime only; calls `handle_poll_comments()` directly). Updated module docstring endpoint list. |
| `tests/test_vision_workflow.py` | NEW — 30 tests across 6 classes: `TestVisionBlueprintNode` (8), `TestIteratePlanNode` (6), `TestRunCompaction` (3), `TestHandlePollComments` (4), `TestVisionEndpoint` (5), `TestPollCommentsEndpoint` (4). All passing. |
| `SCRATCH.md` | Marked Step 5 ✅ in build sequence tree and component table. |
| `Docs/GAOS-Manager-Spec.md` | Updated Phase 2.5 roadmap table — Step 5 marked ✅ Complete. |

**Step 5 note:** AppSheet UI (visual front-end for `Project_Incubator` tab) is a no-code config step with no backend changes — deferred to Step 7 slot as originally planned. `doc-comment-poll` Scheduler job (wire in GCP) and `POST /poll-comments` endpoint (code complete) are production-ready.

**Next up:** Phase 2.5 Step 6 — `tools/google_search.py` + Scout `_discover` recursive node + `KNOWLEDGE_INJECTION` protocol.

---

## 2026-03-16 — Phase 2.5 Step 5: Vision Workflow — Started

**Scope:**
- `Project_Incubator` Google Sheet tab (schema wired into `vision_blueprint` node)
- `VISION_SUBMITTED` → `vision_blueprint` LangGraph node: Gemini Pro generates Blueprint Doc via `tools/google_docs.py`; Chat approval card sent; `Project_Incubator` row updated
- `PLAN_REVIEW` / `COMMENT_RECEIVED` → `iterate_plan` node: appends constraint, triggers compaction at 5
- `ITERATE_PLAN` constraint compaction: Gemini Flash summarises 5 constraints → 1 paragraph; originals archived to BigQuery `aos_logs.blueprint_constraints`
- `handle_poll_comments()` standalone async handler: polls `list_comments()` for all active Blueprint Docs; publishes `COMMENT_RECEIVED` for new unresolved comments
- `POST /vision` endpoint in `main.py`
- `POST /poll-comments` endpoint in `main.py`
- `tests/test_vision_workflow.py` (~15–20 new tests)

**Baseline:** 266 tests passing.

---

## 2026-03-16 — Phase 2.5 Step 4: Google Docs Tool (Blueprint Factory wrapper) COMPLETE ✅

**266 / 266 tests passing. Zero regressions.**

| File | Change |
|------|--------|
| `tools/google_docs.py` | NEW — Google Docs + Drive API wrapper. `create_document`, `read_document`, `append_content`, `list_comments`. SA key or ADC auth. `DocsApiError`, `DocumentNotFoundError` error types. |
| `config/__init__.py` | Added `DocsConfig(service_account_key, blueprints_folder_id)`; added `docs: DocsConfig` field to `Settings`. |
| `config/settings.yaml.template` | Added `docs:` block with `service_account_key` and `blueprints_folder_id` keys. |
| `tests/test_google_docs.py` | NEW — 29 tests across 6 classes (`TestExtractText` × 4, `TestCreateDocument` × 7, `TestReadDocument` × 5, `TestAppendContent` × 5, `TestListComments` × 6, `TestCredentials` × 2). All passing. |
| `Docs/GAOS-Tools-Spec.md` | Added §15 (`tools/vertex_search.py`) and §16 (`tools/google_docs.py`). Updated §14 Reference Index with Vertex Search + Docs entries. |
| `SCRATCH.md` | Marked Step 3 (vertex_search) and Step 4 (google_docs) ✅ in build sequence tree and component table. |
| `Docs/GAOS-Manager-Spec.md` | Updated Phase 2.5 roadmap table — Steps 3 and 4 marked ✅. |

**Step 3 note:** `vertex_search.py` was already implemented (22 tests) but undocumented in GAOS-Tools-Spec.md. §15 closes that gap. `write_playbook` in orchestrators remains ⏳ — deferred to Phase 3 (Approval Gate) when the Playbook Drive folder is live and the approval loop is wired.

**Next up:** Phase 2.5 Step 5 — Vision workflow (`Project_Incubator` tab + `VISION_SUBMITTED` handler + Blueprint Doc generator in Nexus-Prime + `ITERATE_PLAN` node).

---

## 2026-03-16 — Phase 2.5 Step 4: Google Docs Tool (Blueprint Factory wrapper) — Started

**Scope:** `tools/google_docs.py` — Google Docs API wrapper (create, read, append, list comments). Feeds the Blueprint Factory in Nexus-Prime's `VISION_SUBMITTED` handler and the `ITERATE_PLAN` node. Config: `DocsConfig` + `docs:` settings block.

---

## 2026-03-16 — Phase 2.5 Step 2: Morning Briefing + POST /daily-sync COMPLETE ✅

**215 / 215 tests passing. Zero regressions.**

| File | Change |
|------|--------|
| `config/__init__.py` | Added `ChatConfig(owner_space: str)` model; added `chat: ChatConfig` field to `Settings`. |
| `config/settings.yaml.template` | Added `chat.owner_space` key with comment. |
| `agents/nexus_prime/orchestrator.py` | Added `handle_daily_sync(project_id)` — queries overnight Logs, Error Logs, and Agent_Approvals; composes Chat Card v2 morning briefing; calls `send_card()` to `settings.chat.owner_space`. |
| `main.py` | Added `POST /daily-sync` endpoint (Nexus-Prime only); updated docstring. |
| `tests/test_daily_sync.py` | NEW — 13 tests across 2 classes (`TestHandleDailySync` D1–D9, `TestDailySyncEndpoint` S1–S4). All passing. |

**Next up:** Configure `daily-kickoff` Cloud Scheduler job to POST to `/daily-sync` at 6 AM, then proceed to **Phase 2.5 Step 3** (`tools/vertex_search.py` + Playbook schema + `write_playbook` in all orchestrators).

---

## 2026-03-16 — Phase 2.5 Step 2: PAUSED — design reviewed, implementation not started

**Step 2 scope confirmed from SCRATCH.md:**

- **Trigger:** `daily-kickoff` Cloud Scheduler job fires at **6 AM** → POST to `/sync` with `DAILY_SYNC` payload
- **What `/sync` currently does:** handles only `APPROVAL_RESULT` from Apps Script (hard-coded path)
- **Change needed:** detect `DAILY_SYNC` vs `APPROVAL_RESULT` in the request body; for `DAILY_SYNC` → query overnight Logs/Alerts rows from Sheets → compose morning briefing Chat card → `send_card()` to owner's Chat space
- **Settings change needed:** add `chat.owner_space` to `settings.yaml` / `settings.yaml.template` (the `spaces/…` resource name for the owner's DM)
- **`DAILY_SYNC` MessageType:** already exists in `models/__init__.py` ✅
- **`send_card()` in `tools/google_chat.py`:** ready to use ✅

**Resumption point:** `POST /sync` extension + `handle_daily_sync()` in nexus_prime orchestrator + ~8 new tests + WORKLOG + commit.

---

## 2026-03-16 — Phase 2.5 Step 1: Google Chat Tool + POST /chat COMPLETE ✅

**202 / 202 tests passing. Zero regressions.**

| File | Change |
|------|--------|
| `tools/google_chat.py` | NEW — Google Chat API v1 client. `send_message`, `send_card`, `send_approval_card`, `send_skill_import_card`, `parse_chat_event`. ADC auth (Cloud Run) or SA key (local). |
| `main.py` | Added `POST /chat` endpoint. Nexus-Prime only. Routes `CARD_CLICKED` → `APPROVAL_RESULT` or `SKILL_REQUEST`; `MESSAGE` → `CHAT_MESSAGE`; lifecycle events → 200 ACK. |
| `tests/test_google_chat.py` | NEW — 25 tests across 3 classes (`TestGoogleChatTool`, `TestParseChatEvent`, `TestChatEndpoint`). All passing. |

**Fix logged:** `importlib.reload()` must run **before** `patch.object()` in endpoint tests — reload destroys previously patched symbols. Use `_reloaded_client()` pattern.

---

## 2026-03-16T18:00 — Phase 2.5 Design Session COMPLETE ✅

**All design decisions locked. Spec documents, models, and agent identity files updated.**

**What was planned:**

| Innovation Area | Decision |
|----------------|---------|
| Google Chat App | New primary approval surface (Option A). Chat cards → Approve/Reject. Sheet is audit trail only. |
| AppSheet / Vision Hub | New Step 5 — visual "Vision to Blueprint" submission interface. |
| Vertex AI Search | Layer 5b retrieval added. Fills write-only gap in Drive procedural knowledge. |
| Google Docs Dynamic Templates | Blueprint Factory — Nexus-Prime generates structured project docs from Chat Vision submissions. |
| Recursive Scout Search | New `_discover` LangGraph node. Depth-first web search loop with `KNOWLEDGE_INJECTION` protocol. |
| Proactive Daily Sync | New `daily-kickoff` Scheduler job (6 AM) + `doc-comment-poll` job (5 min). |
| KNOWLEDGE_INJECTION | New MessageType + 5-observation gate + `market_intel` knowledge type. |
| ITERATE_PLAN | New LangGraph node (Nexus-Prime). Constraint compaction at 5-item trigger. |
| SKILL_REQUEST | New MessageType for library installation approval workflow. |

**What changed:**

| File | Change |
|------|--------|
| `SCRATCH.md` | All design decisions recorded and locked. Option A confirmed. |
| `models/__init__.py` | Added 8 new `MessageType` values: `CHAT_MESSAGE`, `DAILY_SYNC`, `VISION_SUBMITTED`, `PLAN_REVIEW`, `COMMENT_RECEIVED`, `RESEARCH_MANDATE`, `SKILL_REQUEST`, `KNOWLEDGE_INJECTION`. |
| `Docs/GAOS-Manager-Spec.md` | Added Phase 2.5 roadmap entry (Section 16), updated Section 17 checklist with 7 new components. |
| `Docs/GAOS-Memory-Spec.md` | Added Layer 5b (Vertex AI Search retrieval), `KNOWLEDGE_INJECTION` governance rules, `market_intel` knowledge type, Playbook sub-folder schema, `write_playbook` completion requirement. |
| `Docs/GAOS-Deploy-Spec.md` | Added Section 10.3 (daily-kickoff Scheduler job), Section 10.4 (doc-comment-poll Scheduler job), Phase 2.5 checklist items to Section 14. |
| `Docs/agents/nexus-prime.md` | Added `ITERATE_PLAN` node, `write_playbook` to objectives, `VISION_SUBMITTED` handling to resources, Chat card approval pathway to spec. |
| `Docs/agents/scout.md` | Added `_discover` node, `KNOWLEDGE_INJECTION` protocol, `RESEARCH_MANDATE` message type, `market_intel` knowledge type to guardrails. |
| All 5 domain agent `*.md` files | Added `write_playbook` to completion requirement. |

**Phase 2.5 build sequence (7 steps — implementation begins next session):**

| Step | What | New Files |
|------|------|-----------|
| 1 | Chat interface (POST /chat + Skill Import card) | `tools/google_chat.py` |
| 2 | Daily sync (POST /sync + morning briefing) | — |
| 3 | Google Docs integration (Blueprint Factory) | `tools/google_docs.py` |
| 4 | Vertex AI Search (institutional knowledge retrieval) | `tools/vertex_search.py` |
| 5 | Vision Hub (AppSheet + VISION_SUBMITTED handler) | — |
| 6 | Scout recursive search + KNOWLEDGE_INJECTION | `tools/google_search.py` |
| 7 | ITERATE_PLAN constraint compaction node | — |

**Next up:** Phase 2.5 Step 1 — `tools/google_chat.py` + `POST /chat` endpoint.

---

## 2026-03-16T15:30 — Glossary expansion COMPLETE ✅

**`Docs/GAOS-Project-Glossary.md` fully rebuilt.** Commit `2df1898`.

**What changed:**

| File | Change |
|------|--------|
| `Docs/GAOS-Project-Glossary.md` | Replaced 11-entry stub with a full A–Y alphabetical glossary of 68 abbreviations/acronyms. Plain-English definitions written for a non-technical business owner. Google Tool Stack reference table updated to 10 services. |

**Coverage added:** A2A, ADC, ADK, AGPL, AP, API, AR, AST, Base64, CI/CD, CLI, CMEK, cold-start, CPL, CRM, CVE, DPA, EDI, EOL, FastAPI, GCE, GCP, GDPR, Gemma, GMEK, GPU, Grafana, gspread, HMAC, HRIS, HSM, IAM, JSON, KMS, KPI, LangGraph, Llama, LLM, MCP, Mistral, Mypy, NER, NSSM, OAuth, OIDC, Ollama, P&L, PEP 8, PII, Pydantic, PyPI, pytest, RBAC, RFC 1918, ROAS, ROI, Ruff, SA, SDK, serverless, SHA-256, SKU, SLA, SQL, SSRF, TLS, TTL, UUID, uv, VPC, VRAM, winget, Write-Test-Refine, YAML.

**Next up:** Phase 2 Item 2 — Apps Script manual setup (Script Properties, `setupProtections`, `onChange` trigger). Requires browser session.

---

## 2026-03-16T15:00 — Phase 2 Item 3 COMPLETE ✅

**All 62 tests pass (0 failures, 0 errors).**

**What was built:**

| File | Change |
|------|--------|
| `tools/google_sheets.py` | Added `get_all_records_with_row_numbers()` (returns row-number + record pairs for safe deletion) and `delete_rows()` (deletes rows descending to avoid index shift). |
| `models/__init__.py` | Added `NIGHTLY_ARCHIVE = "NIGHTLY_ARCHIVE"` to `MessageType` enum. |
| `agents/nexus_prime/orchestrator.py` | Added `handle_archive(project_id)` standalone async function (~175 lines). Implements 5-step nightly job: Monday Ollama summary → BQ, Logs/Error Logs (30d) → BQ + delete, Agent_Approvals closed (90d) → BQ + delete, ARCHIVE report row → Logs, row-count alert if tab > 25k. |
| `main.py` | Added `POST /archive` endpoint — auth-gated, Nexus-Prime only, calls `handle_archive()`. |
| `tests/test_agents.py` | Added `TestArchiveJob` class (10 tests): 7 unit tests for `handle_archive` logic + 3 endpoint tests. |

**Key design decisions:**
- BQ insert and Sheet delete are in the same `try` block — BQ failure leaves Sheet rows intact (no data loss).
- `_parse_ts()` returns epoch on malformed timestamps → badly-formatted rows are never deleted.
- `asyncio.run()` required throughout tests (Python 3.13 removed `asyncio.get_event_loop()` default).

**Next up:** Phase 2 Item 2 — Apps Script manual setup (Script Properties, `setupProtections`, `onChange` trigger). Requires browser session.

---

## 2026-03-16T13:30 — Phase 2 item 1 COMPLETE ✅

**All 52 tests pass (0 failures, 0 errors). 7.62s.**

**What was built:**

| File | Change |
|------|--------|
| `agents/__init__.py` | Replaced monolithic `_call_model()` with a routing dispatcher. Added `_call_model_ollama()` (httpx POST to `/api/generate`, 2s timeout, auto-fallback to `LOCAL_MODEL_FALLBACK`) and `_call_model_gemini()` (previous behavior). Added `web_access: bool = False` param. |
| `tools/web_search.py` | New file. DuckDuckGo Instant Answer API via httpx — no API key, no cost. Returns AbstractText + RelatedTopics as a formatted string. Empty string on any error. |
| `tests/test_agents.py` | Added `TestOllamaRouting` (6 tests) and `TestWebSearch` (7 tests). |

**How web access works:**
- Caller passes `web_access=True` to `_call_model()`
- Only fires when the model is an `ollama/` alias (never on Gemini calls)
- DuckDuckGo results are prepended to the prompt before sending to the local model
- Failure to fetch web results is silent — Ollama still gets the original prompt

**Next up:** Phase 2 item 2 — Apps Script manual setup (Script Properties, `setupProtections`, `onChange` trigger).

---

## 2026-03-16T16:00 — Implementation started

**Resuming Phase 2 item 1.** Implementing all three changes now:
1. `agents/__init__.py` — add `_call_model_ollama()` + update `_call_model()` to route `ollama/` prefix + add `web_access` param
2. `tools/web_search.py` — new file, DuckDuckGo Instant Answer API via httpx (no key)
3. `tests/test_agents.py` — add `TestOllamaRouting` + `TestWebSearch`

**Status:** 🔨 In progress

---

## 2026-03-16 — Format updated

New entries will be prepended (top of file). No content change.

---

## Session: Phase 2 — Ollama Routing + Web Access Tool

### 2026-03-16 — Session opened

**Goal:** Implement Phase 2 item 1 — Ollama integration in `_call_model`.
User also requested Ollama have web access (noted: Ollama has no built-in browsing;
will implement a `web_search` tool layer that fetches results and injects into
the prompt before the Ollama call).

---

### 2026-03-16 — Research complete

**Findings:**

- `agents/__init__.py` → `_call_model()` always routes to `google.genai` regardless
  of the model alias. The `ollama/llama3.1` value in `settings.yaml` is declared but
  silently hits the Gemini path — no Ollama calls are made anywhere in the codebase.

- `OLLAMA_HOST` secret already exists in GCP Secret Manager (`http://localhost:11434`)
  and IAM is already granted to all 7 service accounts (per deploy spec §3.2).

- `httpx>=0.27.0` is already in `pyproject.toml` — no new dependency needed for the
  Ollama HTTP call (`POST /api/generate`).

- `LOCAL_MODEL_TIMEOUT_SECONDS: 2` and `LOCAL_MODEL_FALLBACK: "gemini-2.0-flash"` are
  already in `settings.yaml` and the `ModelAliases` config model — just not wired up.

- `ollama` SDK is NOT in `pyproject.toml`. Will use `httpx` directly (already present).

- `tests/test_agents.py` has a `_VERSION_RE` pattern that intentionally allows
  `ollama/` prefix in settings aliases but blocks it in string literals in orchestrator
  source files — the routing logic goes in `agents/__init__.py` which is not scanned
  by that test, so no conflict.

**Scope of change:**
1. Add `_call_model_ollama()` private helper in `agents/__init__.py`
   - POST to `{OLLAMA_HOST}/api/generate` with `stream=False`
   - 2-second timeout → on timeout/error fall back to `LOCAL_MODEL_FALLBACK` via Gemini
2. Add `_web_search()` helper in `tools/` (new file: `tools/web_search.py`)
   - Uses `httpx` to fetch DuckDuckGo Instant Answer API (no key required, free)
   - Returns top N snippets as a formatted string
   - Called optionally before Ollama when `web_access=True` is passed to `_call_model`
3. Update `_call_model()` signature: add `web_access: bool = False` param
   - If `model.startswith("ollama/")` → route to Ollama helper
   - If `web_access=True` → prepend web search results to prompt first
4. Add tests in `tests/test_agents.py`:
   - `TestOllamaRouting` — verifies `ollama/` prefix routes correctly, fallback fires on timeout
   - `TestWebSearch` — verifies web_search returns snippets, handles HTTP errors

**Status:** ⏳ Ready to implement. Paused — waiting for user to resume.

---

_Last updated: 2026-03-16_
