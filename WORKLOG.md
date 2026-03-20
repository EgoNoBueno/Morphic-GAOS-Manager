# GAOS Work Log

Active work session. Updated in real time — refresh or keep open in VS Code.
**Most recent entries are at the top.**

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
- `models/__init__.py` — Added `MonologueFrame` Pydantic model (7 fields: `task_id`, `project_id`, `knowledge_gap_detected`, `knowledge_gap_description`, `partial_result_available`, `response_mode`, `reasoning_summary`, `timestamp`)
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
