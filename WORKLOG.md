# GAOS Work Log

Active work session. Updated in real time — refresh or keep open in VS Code.
**Most recent entries are at the top.**

---

## 2026-03-17 — Phase 2.5 Step 1: Google Chat Tool + POST /chat COMPLETE ✅

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
