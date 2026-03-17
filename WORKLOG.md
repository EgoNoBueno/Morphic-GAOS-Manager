# GAOS Work Log

Active work session. Updated in real time — refresh or keep open in VS Code.
**Most recent entries are at the top.**

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
