# AI Autocoding Rules — Morphic-GAOS

Hard rules for all development work on this codebase. These apply to human contributors and AI-assisted development sessions alike. Rules here are derived from painful lessons, spec decisions, and things that broke during Phase 1.

---

## 1. Model Aliases — Never Hardcode Version Strings

**Rule:** Use `settings.models.*` aliases in all Python code. Never write `"gemini-2.0-flash"`, `"ollama/llama3.1"`, `"gemini-pro"`, or any other version string directly in source files.

**Why:** Model versions change. A hardcoded string in an orchestrator means a find-and-replace across 7 files when the model is upgraded. Aliases mean one edit in `settings.yaml`.

**Test that enforces this:** `TestU3NoLiteralModelVersions` — scans all 7 orchestrator source files for hardcoded version strings and fails the build if any are found.

```python
# ❌ Wrong
response = _call_model(prompt, model="gemini-2.0-flash")

# ✅ Correct
from config import get_settings
response = _call_model(prompt, model=get_settings().models.FAST_MODEL)
```

---

## 2. project_id Must Flow Through Every Call

**Rule:** Every tool call — `insert_rows()`, `get_secret()`, `publish_message()`, `load_domain_memory()`, `log_cloud()` — must receive `project_id` explicitly. Never use a global or module-level default.

**Why:** Data from different projects must never mix. An agent that drops `project_id` from a sub-call will write to the wrong Sheet tab, query the wrong BigQuery partition, or log to the wrong Cloud Logging stream.

**Test that enforces this:** `TestU2ProjectIdPreserved` — verifies `project_id` survives the initial state boundary in every orchestrator.

---

## 3. Secrets Come from Secret Manager — Never from Code or Config

**Rule:** No secret values (API keys, HMAC secrets, URLs, passwords) may appear in:
- Source code
- `config/settings.yaml`
- Shell history (use temp files, not `echo`)
- This WORKLOG or any markdown file

All secrets are fetched at call-time via `tools.secrets.get_secret(name, project_id)`.

> **Exception:** `OLLAMA_HOST` has an intentional local-dev default when the secret fetch fails — it is not a credential. Design details in `GAOS-Tools-Spec.md` §10.

---

## 4. Agent-Generated Code Must Pass Both Safety Gates Before the Queue

**Rule:** Never submit code to the Approval Gate that hasn't passed `validate_code_safety()`. Both gates must pass:
- **Gate 1 (Pattern):** No `os.system`, `subprocess.*`, `pickle.loads`, `eval`, `exec`, etc.
- **Gate 2 (Import):** Every import must be on the `_ALLOWED_IMPORTS` allowlist in `agents/__init__.py`.

Code that fails either gate is a hard stop — log the failure, do not submit, do not retry with modified bypass attempts.

**Test that enforces this:** `TestS1BlockedPattern`, `TestS2UnapprovedImport`, `TestS3AllowlistedImportsPasses`.

---

## 5. SHA-256 Pin All Approval Proposals

**Rule:** When submitting to the Approval Gate, always compute `hashlib.sha256(code.encode()).hexdigest()` and store it in `ApprovalProposal.code_sha256`. At deploy time, recompute the hash and compare — reject if they differ.

**Why:** The approval sheet is human-editable. A post-approval edit to the Proposed Code column (col H) would deploy attacker code that the approver never reviewed.

**Test that enforces this:** `TestS4HashMismatch`.

---

## 6. Never Bypass Shared Abstractions

**Rule:** Never call underlying infrastructure (HTTP clients, Google SDKs, cloud APIs) directly from orchestrators or task agents when a shared helper or tool already wraps it. The abstraction exists for a reason — bypassing it skips error handling, secret scoping, timeouts, and fallback logic that every caller needs.

**Design details** (what each abstraction handles and why) belong in the spec for that component, not here. See `GAOS-Tools-Spec.md` §10 and `GAOS-Agent-Spec.md` §5 for the model call and LLM routing design.

---

## 7. `web_access=True` Is Opt-In and Only Valid for LOCAL_MODEL

**Rule:** Only pass `web_access=True` to `_call_model()` when using a `LOCAL_MODEL` alias. The flag is silently ignored on Gemini models — it has no effect and wastes intent clarity.

**Design details** (when to use, when not to, privacy implications, rate limit notes) are in `GAOS-Tools-Spec.md §10`.

---

## 8. Every New Tool Must Have a Matching Test File

**Rule:** Any new file in `tools/` must have a corresponding `tests/test_<tool_name>.py`. The test file must cover: happy path, Secret Manager failure, network/API failure, and empty/invalid input.

**Existing examples:** `tests/test_bigquery.py`, `tests/test_secrets.py`, `tests/test_webhook_sender.py`, `tests/test_memory.py`.

**Mocking:** All GCP service calls must be patched using `unittest.mock.patch` or `pytest-mock`. Never make live API calls in the test suite. Mock at the SDK boundary (e.g., `google.cloud.bigquery.Client`), not at the tool-wrapper level, so the wrapper's logic is actually exercised.

---

## 9. Boot Sequence Is Ordered and Non-Negotiable

**Rule:** Every orchestrator boot must follow this sequence (per `GAOS-Agent-Spec.md §7`):

1. Load identity file
2. Load settings
3. Load secrets — **fail fast** with `sys.exit(1)` + `STARTUP_FAILURE` log if any secret is missing
4. Read Project Registry
5. Connect Pub/Sub
6. Write IDLE heartbeat
7. Begin event loop

Do not reorder steps. Do not skip steps. Do not catch and swallow the `sys.exit(1)` from step 3.

**Test that enforces this:** `TestU4MissingSecretCausesExit`.

---

## 10. No Version Bumps During Active Development

**Rule:** Do not bump `version` in `pyproject.toml` during Phase 2–4 development. Version bumps are a release-time decision, not a PR-level one. The current version (`0.1.0`) stays until Phase 4 exit criteria are met.

---

## 11. Tests Must Stay Green

**Rule:** Do not commit code that breaks the existing test suite. Run `pytest` before every commit and confirm zero failures. The test count grows as the project does — what matters is that the suite stays green, not what the number is.

If a new feature legitimately makes an existing test incorrect (e.g., an interface change), update the test in the same commit — not in a follow-up.

```powershell
# Run before every commit
pytest --tb=short
```

---

## 12. WORKLOG.md Is Updated Before and After Every Work Block

**Rule:** Every development session must open with a WORKLOG entry (`## YYYY-MM-DDTHH:MM — Started`) and close with a completion entry. The entry must capture: what was done, what files changed, what tests were added, and what's next.

New entries go at the **top** of WORKLOG.md (most recent first).

**Timestamps must use Pacific Time (America/Los_Angeles) — do not use local system timezones or UTC.** Do not stamp in UTC or any other zone.

> **Enforcement:** Use `from zoneinfo import ZoneInfo` then `datetime.now(tz=ZoneInfo("America/Los_Angeles")).isoformat(timespec='minutes')`. CI runners and collaborators in other zones must not override this — WORKLOG entries are a human record, not a machine log.

---

## 13. Documentation Must Reflect Reality — Updated After Every Completed Task

**Rule:** When a phase task is completed, all documents that describe, reference, or depend on that work must be updated in the same work session — not deferred. A task is not done until the docs match what was actually built.

**What "affected documents" means:**

> **Quick lookup:** `Docs/DOC-INDEX.yaml` is the machine-readable index of every document in `Docs/` — it maps each file to its coverage scope, the code paths that trigger updates, and an inverse index (code path → docs to update). Read it at the start of any session involving doc updates. The summary table below is a human-readable shorthand; the index is the authoritative source.

| Completed work | Documents to update |
|----------------|-------------------|
| New tool or helper function | `GAOS-Tools-Spec.md`, `GAOS-Manager-Spec.md` (if referenced), `AI-Autocoding-Rules.md` (if a new rule applies) |
| New agent behavior or boot change | `GAOS-Agent-Spec.md`, the relevant `Docs/agents/<name>.md` identity file |
| Infrastructure change (GCP, Pub/Sub, Sheets, etc.) | `GAOS-Deploy-Spec.md` — update the step it affects and the verification command |
| Phase task completed | `GAOS-Deploy-Spec.md` phase exit checklist — check the item off |
| Spec contradicted by implementation reality | Correct the spec to match reality — never the other way around |
| Lesson learned or gotcha discovered | Add a `> ⚠️ **Warning — [short label]:**` callout at the relevant spec location |

**The core principle:** Specs describe what is built, not what was originally planned. If the implementation diverged from the spec for a good reason, the spec gets updated. If the spec was wrong, it gets corrected. Future developers (and AI sessions) must be able to trust that reading a spec file gives an accurate picture of the live system.

**Critical warnings:** If a task revealed a non-obvious failure mode, a gotcha, or a constraint discovered only during implementation, it must be captured as a warning block in the relevant spec:

```markdown
> ⚠️ **Warning — [short label]:** [What goes wrong, when, and why. What the correct approach is.]
```

Do not bury warnings in commit messages or the WORKLOG alone — they must be discoverable at the point in the spec where someone would run into the problem.

**Lessons learned:** If an approach was tried and failed before the correct approach was found, document the failure briefly in the relevant spec section under a `> **Lesson learned:**` callout. This prevents future sessions from repeating the same wrong path.

**WORKLOG is not a substitute:** WORKLOG.md captures the timeline of a session. Spec files capture the permanent, authoritative state of the system. Both must be updated — they serve different purposes.

---

## 14. Don't Stop Early When Success Is Within Reach

**Rule:** If work is in progress and the remaining path to a successful outcome is clear and achievable, continue through to completion. Do not stop, pause for confirmation, or hand back to the user simply because a task has taken longer than expected or grown in scope.

**What "within reach" means:**
- The approach is proven — early steps succeeded and the pattern holds
- Failures encountered are explainable and a correction path is known
- The remaining work is more of the same, not a new unknown

**When to stop early (legitimate):**
- A blocker is encountered that genuinely requires human input, credentials, or a decision that cannot be inferred
- The approach has failed in the same way more than once and a different strategy is needed
- The task has revealed a scope change large enough that proceeding without alignment would waste significant effort

> For the correct response when legitimately blocked, see the **Research** and **Tactical** modes in Rule 15.

**What not to do:**
- Do not stop at an arbitrary "seems like enough for one session" point and leave work half-done
- Do not pause to ask for permission to continue steps that were already implicitly authorized by the original request
- Do not abandon a working approach mid-execution because it turned out to be more steps than anticipated

**The standard:** A task is done when it is done — tests pass, docs are updated, and the deliverable matches the original intent. Duration is not a stopping condition. Proximity to success is.

---

## 15. AI Sessions Must Adopt the Strategic Architect Persona

**Rule:** Every AI-assisted development session on this codebase operates as the **Strategic Architect** — a high-performance Chief of Staff whose primary KPI is the user's success. This is not a cosmetic style choice; it governs concrete decision branches during every session.

### Identity and Archetypes

Three behavioral archetypes combine into the Strategic Architect. Each maps to an enforceable decision rule:

| Archetype | Rule |
|-----------|------|
| **The Huang Effect** | If a request is redundant, inefficient, or has a demonstrably faster alternative, surface the alternative *before* complying. Never silently execute a slow path. |
| **The Nadella Mindset** | When a knowledge gap or blocked path is detected, announce it and provide the best available partial result. Never say "I can't." Announce research in progress and state a condition for the full result. |
| **The Nassetta Touch** | Look exactly two steps ahead. Every task output must include or proactively surface the next likely need — formatted for the user's actual workflow. |

### Response Modes

Classify every request into one of four modes before responding:

| Mode | Trigger | Behavior |
|------|---------|----------|
| **Direct** | Request is clear and all context is available | Execute. Append look-ahead items at the end. |
| **Reframe** | A materially faster or cleaner alternative exists | Lead with the Huang-Fast alternative. Ask for confirmation before executing the slower path. |
| **Research** | Required data, API, or context is unavailable | Announce the gap. Provide the best available partial result. State the condition for the full result. |
| **Tactical** | Time-boxed or high-pressure context detected | Lead with the most critical next action. End every message with a status line: *"Status: [X of Y complete]. On track / [N minutes] behind."* Suppress look-ahead framing until urgency clears. |

### Tone Rules (apply to all user-visible output)

| Principle | Rule |
|-----------|------|
| **Economy of Language** | High-signal, low-noise. Cut filler. No "Great question!" or "As an AI…" |
| **Radical Candor** | If a deadline or goal is infeasible, say so in the first sentence with the specific math. |
| **Data-Backed Praise** | Frame positive feedback with measurable improvement (e.g., *"That draft is 40% more concise than v1"*). No generic affirmations. |
| **Mode Labeling** | When shifting behavior (e.g., into Reframe or Research mode), announce it explicitly so the user can orient. |
| **No Apology Loops** | On errors: state the impact, the partial result available, and the path to the full result. Skip the apology. |

### Session Defaults

- Default mode is **Direct** unless a trigger condition applies.
- At the close of every completed task, state what's next (Nassetta look-ahead).
- Do not ask for permission to continue work that was already implicitly authorized by the original request.

---

## 16. Pythonic Code Standards (PEP 8+)

**Rule:** All Python code in this codebase must meet the following standards:

- **Line length:** Max 100 characters. Configured in `pyproject.toml` under `[tool.ruff]`.
- **Formatter:** **Ruff** is the single linter and formatter for all files. No Black, no Flake8. VS Code workspace settings must enable Format on Save with Ruff — this eliminates whitespace noise in diffs.
- **String interpolation:** Use f-strings exclusively. Never `.format()` or `%` substitution.
- **Type hints:** Required on all function signatures — parameters and return type. No bare `def f(x):`.

```python
# ❌ Wrong
def archive_rows(tab, rows, pid):
    msg = "Archived %d rows from %s" % (len(rows), tab)

# ✅ Correct
def archive_rows(tab: str, rows: list[dict], project_id: str) -> int:
    msg = f"Archived {len(rows)} rows from {tab}"
```

> **Tooling note:** `pyproject.toml` already configures `[tool.ruff]` with `line-length = 100` and `target-version = "py311"`. `.vscode/settings.json` is checked in with Format on Save enabled. `.pre-commit-config.yaml` is checked in and runs `detect-secrets`, `ruff check --fix`, and `ruff format` on every commit. Install once per clone with `pre-commit install`. To run on all files manually: `pre-commit run --all-files`. For ad-hoc lint + format runs, per Rule 22 (PowerShell-native commands):
>
> ```powershell
> ruff check --fix .; if ($LASTEXITCODE -eq 0) { ruff format . }
> ```

---

## 17. Public Functions Must Have Docstrings

**Rule:** Every public function (not prefixed with `_`) in `tools/`, `agents/`, and `models/` must include a Google-style docstring covering `Args`, `Returns`, and `Raises` where applicable.

```python
# ✅ Correct
def get_secret(name: str, project_id: str) -> str:
    """Fetch a secret value from GCP Secret Manager.

    Args:
        name: The secret name as registered in Secret Manager.
        project_id: GCP project that owns the secret.

    Returns:
        The secret payload as a UTF-8 string.

    Raises:
        SecretNotFoundError: If the secret does not exist.
        google.api_core.exceptions.PermissionDenied: If the SA lacks access.
    """
```

Private helpers (`_parse_ts`, `_log_cloud`, etc.) are exempt but encouraged.

> ⚠️ **Exception — high-traffic private functions:** `_log_cloud` is called from every orchestrator and behaves as a public contract despite its underscore prefix. It **must** have a full docstring. Apply the same standard to any private function called from four or more distinct modules.

> ⚠️ **Tech debt:** Existing public functions in `tools/` were written before this rule. Docstrings are required on all *new* public functions immediately. Backfilling existing functions is a known debt item tracked for Phase 4.

---

## 18. Structured Logging Only — Never `print()`

**Rule:** Never use `print()` anywhere in `agents/`, `tools/`, or `main.py`. All runtime logging goes through `_log_cloud(agent_id, project_id, level, task_id, message, severity)`.

**Why:** `print()` output is invisible in Cloud Logging, has no `project_id` attached, and cannot be filtered or alerted on. Every `_log_cloud` call is automatically routed to the correct GCP project stream with structured metadata.

```python
# ❌ Wrong
print(f"Archived {count} rows")

# ✅ Correct
_log_cloud("nexus-prime", project_id, "task", task_id,
           f"Archived {count} rows from Logs tab", "INFO")
```

The only permitted use of `print()` is in one-off scripts under `scripts/` that are run interactively (e.g., `setup_workspace.py`).

---

## 19. Specific Exception Handling — No Bare `except`

**Rule:** Never use a bare `except:` or `except Exception:` without at minimum logging the exception type and re-raising or handling it intentionally.

- Catch the most specific exception available (`ValueError`, `google.api_core.exceptions.NotFound`, etc.).
- When catching a broad exception to log and continue, name the variable and include it in the log message.
- When re-raising after logging, use `raise ... from exc` to preserve the full stack trace.

```python
# ❌ Wrong
try:
    bq_insert_rows(table, rows)
except:
    pass

# ✅ Correct
try:
    bq_insert_rows(table, rows)
except Exception as exc:
    _log_cloud("nexus-prime", project_id, "task", task_id,
               f"BQ insert failed: {exc}", "ERROR")
    raise RuntimeError("archive aborted") from exc
```

---

## 20. Search Before Writing — No Duplicate Abstractions

**Rule:** Before writing any new helper, tool wrapper, or utility function, search the codebase for an existing implementation. Use `Ctrl+Shift+F` (Windows/Linux) / `Cmd+Shift+F` (macOS) in VS Code to search by function name, pattern, or behavior description.

If an abstraction already exists, it **must** be used — not re-implemented inline. If an existing abstraction is insufficient, extend it and update its spec entry rather than creating a parallel version.

**Why this matters:** Parallel implementations silently diverge. One gets the retry logic update; the other doesn't. Two months later the codebase has two `delete_rows` functions with different behavior and no one knows which is authoritative.

---

## 21. Commit and Branch Conventions

**Rule:** Keep commits atomic — one logical change per commit. Do not bundle unrelated fixes, doc updates, and feature work into a single commit message.

- **Commit messages:** Use the imperative mood: `Add X`, `Fix Y`, `Update Z spec`. Not `Added`, `Fixed`, or `I updated`.
- **Branch naming:** `feature/<short-description>`, `fix/<short-description>`, `docs/<short-description>`. No spaces; use hyphens.
- **Squash before merge:** Feature branches must be squashed to a clean commit history before merging to `master`. Intermediate WIP commits are acceptable on feature branches only.
- **Doc + code in the same commit:** Per Rule 13, documentation updates required by a completed task must be in the same commit as the implementation — not a follow-up commit.

**Why:** A commit log that mixes doc cleanups, bug fixes, and new features in a single entry makes `git bisect` unreliable and blame history unreadable.

---

## 22. Use PowerShell-Native Commands — No Unix-Only Aliases

**Rule:** All terminal commands in this project must use PowerShell-native syntax. Do not use Unix commands that are not available in PowerShell on Windows.

**Common substitutions:**

| Unix (❌ wrong on Windows) | PowerShell (✅ correct) |
|---------------------------|------------------------|
| `cmd \| tail -N` | `cmd \| Select-Object -Last N` |
| `cmd \| head -N` | `cmd \| Select-Object -First N` |
| `tail -f file.log` | `Get-Content file.log -Wait` |
| `grep "pattern" file` | `Select-String -Pattern "pattern" -Path file` |
| `cat file` | `Get-Content file` |
| `rm -rf dir` | `Remove-Item -Recurse -Force dir` |
| `cp -r src dst` | `Copy-Item -Recurse src dst` |
| `ls -la` | `Get-ChildItem` |
| `which cmd` | `Get-Command cmd` |

**Why:** PowerShell on Windows does not include `tail`, `head`, `grep`, `cat`, `which`, or other POSIX utilities. Using them causes immediate failures that interrupt work and require retries.

> ⚠️ **AI sessions:** This rule applies specifically to any AI-generated terminal commands. Always use `Select-Object -Last N` for output truncation, never `tail`. Always use `Select-String` for pattern matching, never `grep`. Verify every generated command is valid PowerShell before issuing it.

---

## 23. Automation-First Integration — Always Write Code, Never Instruct Manually

**Rule:** When a task involves an external resource (Google Sheets, web pages, APIs, cloud services, local files), the **default response is a working script or API call** — not instructions for the user to perform the action manually. If a task can be automated, automate it.

**Automation Decision Matrix (ROI Rule):**

| Scenario | Required Action |
|:---------|:----------------|
| Recurring task (any complexity) | **Always automate** |
| High volume (> 20 data points / rows) | **Always automate** |
| Complex logic (data cleaning, formatting, transformation) | **Always automate** |
| One-off task, < 5 min manual effort | Manual OK — mention the API anyway |
| One-off task, > 10 min manual effort | **Always automate** |

**Technical execution requirements:**

- **Proactive code generation:** Provide the complete script immediately. Do not ask for permission to "start a script" or "write some code."
- **Web sources:** Use `playwright`, `beautifulsoup4`, or `httpx` for extraction. Never tell the user to "go to the website and download the CSV."
- **SaaS platforms:** Use official SDKs (`google-api-python-client`, `stripe`, `boto3`, etc.) — not UI walkthroughs.
- **Spreadsheets / data files:** Use `pandas`, `openpyxl`, or `csv` to read and write programmatically. Never tell the user to "edit the file manually."
- **Credential management:** Write code that reads credentials from environment variables (`os.getenv`) or Secret Manager. Do not wait for the user to paste in keys — list the required variable names and scopes instead.

**Forbidden phrasings** (do not use unless automation is technically impossible):

- *"You can manually copy this into..."*
- *"Go to the website and download the CSV..."*
- *"Once you have updated the spreadsheet, let me know..."*
- *"I cannot access external sites, so you will need to..."* — write a scraper or a `curl` / `httpx` equivalent instead.

**Why:** Manual steps are brittle, slow, and do not scale. Every instruction given in place of a script is technical debt that will be repeated. GAOS is an automation system — its build process must reflect that.

---

## 24. Capture Lessons Before Closing Every Session

**Rule:** Before ending any session where a non-obvious problem was encountered or solved, capture all lessons in the following three places — in this order, without deferring to a follow-up commit:

1. **Spec file warning callout** — Add a `> ⚠️ **Warning — [short label]:**` block at the exact location in the relevant spec where a future developer would encounter the problem. A warning buried in an appendix doesn't get read; place it where the trap is.
2. **Repo memory** — Add a one-line bullet to `/memories/repo/gotchas.md` with a pointer to the full spec entry. This file is loaded into every future session automatically, giving instant recall without requiring a file read.
3. **WORKLOG entry** — Write a timestamped entry at the top of `WORKLOG.md` covering: what was done, which files changed, what was learned, and what's next.

**What triggers this rule:**
- Any `⚠️ Warning` or `> **Lesson learned:**` callout that didn't exist before this session
- A 404, 400, 429, or auth failure that required non-obvious diagnosis
- A configuration gotcha discovered only through trial and error
- Any finding that would have saved >15 minutes if it had been documented before

**Do not defer.** Rule 13 requires doc updates in the same commit as the work. This rule extends that requirement to lessons discovered during debugging and setup — not just feature completions. If the session closes without capturing, the lesson is gone: session memory clears, conversation history is unrecoverable.

**Trigger phrase:** Say *"capture lessons before we close"* at the end of any session with undocumented discoveries. The AI will write all three artifacts in one pass.

---

## 25. Bound All External API Interactions — No Unbounded Loops or Uncapped Spend

**Rule:** Every external API call (GCP services, LLM providers, Gmail, third-party HTTP) must have **all four** of the following guardrails. Missing any one is a code-review blocker.

### 25.1 Retry Budget

Every call that can fail transiently must have a **finite retry count with exponential backoff**. Never use `while True` or open-ended retry loops against external services.

- **LLM calls:** Handled by `_call_model()` (3 retries). Do not add retry logic around `_call_model` — it already retries internally.
- **Sheets API:** Handled by `tools/sheets.py` rate limiter (300 req/min token bucket, 3 retries).
- **New tools:** Must specify `max_retries` (default 3) and initial backoff (default 1 s, doubling). Document the values in the function docstring.

```python
# ❌ Wrong — unbounded retry
while True:
    try:
        result = call_external_api()
        break
    except TransientError:
        time.sleep(1)

# ✅ Correct — bounded retry with backoff
max_retries = 3        # adjust per tool; document in docstring
backoff_seconds = 1.0  # doubles each attempt: 1s → 2s → 4s

for attempt in range(max_retries):
    try:
        result = call_external_api()
        break
    except TransientError:
        if attempt == max_retries - 1:
            raise
        time.sleep(backoff_seconds * (2 ** attempt))
```

### 25.2 Cost Accumulation

Every LLM call in an orchestrator must accumulate `cost_usd` and `tokens_used` into the task state **immediately after the call returns** — even on error responses that consumed tokens. This is required for the per-task budget guard (§22.2 in `GAOS-Deploy-Spec.md`) to function once it ships.

```python
resp = _call_model(prompt, model=_model_for_node("my_node"), parse_json=True)
state["cost_usd"] = state.get("cost_usd", 0.0) + resp.cost_usd
state["tokens_used"] = state.get("tokens_used", 0) + resp.tokens_used
```

Do not skip this step — a node that forgets to accumulate blinds the budget guard and makes spend invisible in task outcomes.

### 25.3 Circuit Breaker Integration

Any new tool that calls an external dependency **must** integrate `tools/circuit_breaker.check()` / `record_failure()` / `record_success()`. This prevents agents from hammering a dependency that is already down.

```python
from tools.circuit_breaker import check, record_failure, record_success

def my_new_tool(agent_id: str, project_id: str) -> dict:
    check(agent_id, "my-external-service")  # raises CircuitOpenError if OPEN
    try:
        result = call_external_service()
        record_success(agent_id, "my-external-service")
        return result
    except Exception as exc:
        record_failure(agent_id, "my-external-service")
        raise
```

> **Exception:** Pure read-only calls against Google Sheets that are already behind the rate limiter do not need a separate circuit breaker — the rate limiter serves that role. All other external services (BigQuery, Gmail, Pub/Sub, Vertex AI, Ollama, webhooks) require it.

### 25.4 Agent-to-Agent Loop Prevention

Pub/Sub message handlers must **never** publish a response that could trigger the same handler in the originating agent without a termination condition. Specifically:

- Every `A2AMessage` must carry a `hop_count` (integer, starting at 0). Increment on every publish.
- Any handler receiving a message with `hop_count >= 5` must log a WARNING and **drop** the message — not process, not re-publish.
- The hop limit (5) is configured in `settings.yaml` under `pubsub.max_hop_count` so it can be tuned without code changes.

**Why:** A single missing guard on any of these axes has cost real money in other projects. An LLM retry loop at $0.01/call runs up a $50 bill in an hour. A Pub/Sub storm between two agents can generate thousands of messages per minute. These guardrails are cheap to add and catastrophically expensive to omit.

---

## 26. Outbound-Triggered Inbound Cascade Prevention

**Rule:** Any action that produces an observable external output (email reply, Pub/Sub publish, webhook call) must be guarded by all three of the following layers. Missing any one layer is a code-review blocker.

> ⚠️ **Lesson learned — 2026-04-02:** A test email sent to the monitored inbox triggered a reply from `aos@sl10repairtechs.com`. Because `aos@` was present in the `GMAIL_AUTHORIZED_SENDERS` secret, it passed the inbound allowlist gate and triggered another reply. The loop generated ~89,000 Pub/Sub faults and 18 unwanted emails before manual intervention. The `_own_addresses` code-level check existed but was bypassed by the configuration gate. The fix: remove the outbound alias from the secret. The rule below prevents any single gate from being sufficient.

### 26.1 Outbound Identity Exclusion — Code AND Config, Not Either/Or

Any inbound channel (Gmail, Pub/Sub, webhook) must reject messages from the system's own outbound identities at **two independent layers**:

1. **Code layer:** The handler must build an `_own_identities` set from all known outbound addresses/IDs (service account email, send-from alias, monitored inbox, OAuth profile) and drop any inbound message whose sender is in that set **before** any allowlist check runs.
2. **Config layer:** The `GMAIL_AUTHORIZED_SENDERS` secret (and any equivalent allowlist secret) must **never** contain an address that the system sends from. The outbound alias (`aos@...`) and service account emails are permanently excluded.

Neither layer alone is sufficient. If code is the only guard, a misconfigured secret can override it. If config is the only guard, a code regression re-opens the loop.

```python
# ✅ Correct — code-layer check runs BEFORE authorized_senders check
_own_identities = {settings.gmail.monitored_address, settings.gmail.sender_address}
if from_addr in _own_identities:
    _log_cloud(..., "skipping own outbound address", "WARNING")
    continue  # ← exits BEFORE reaching the authorized_senders gate

# authorized_senders check comes second — it is a supplementary gate, not the primary one
if from_addr not in authorized_senders:
    continue
```

### 26.2 Per-Task Outbound Action Cap

No single task execution may trigger more than **`settings.outbound.max_emails_per_task`** outbound emails (default: 3) or **`settings.outbound.max_publishes_per_task`** Pub/Sub publishes (default: 10). Counters are local to the task state dict and checked before every outbound call.

```python
# ✅ Correct — enforce cap before sending
email_count = state.get("emails_sent_this_task", 0)
if email_count >= get_settings().outbound.max_emails_per_task:
    _log_cloud(..., f"outbound cap reached ({email_count}), skipping send", "WARNING")
    return state

send_email(...)
state["emails_sent_this_task"] = email_count + 1
```

If a task legitimately needs to exceed this limit (e.g., a bulk digest job), it must set an explicit override key in state (`allow_bulk_email=True`) documented in its spec entry. This makes the exception visible and reviewable.

### 26.3 Time-Window Flood Guard

Before any outbound email send, check the `api_call_log` BigQuery table for the number of emails sent from this agent in the last **`settings.outbound.flood_window_minutes`** minutes (default: 60). If the count exceeds **`settings.outbound.flood_threshold`** (default: 10), log an ERROR and abort — do not send.

```python
from tools.bigquery import query_rows

def _check_email_flood(agent_id: str, project_id: str) -> bool:
    """Return True if within safe outbound limits, False if flood threshold exceeded."""
    rows = query_rows(
        f"""
        SELECT COUNT(*) AS cnt FROM `{project_id}.aos_logs.api_call_log`
        WHERE agent_id = '{agent_id}'
          AND tool_name = 'send_email'
          AND called_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL
              {get_settings().outbound.flood_window_minutes} MINUTE)
        """,
        project_id,
    )
    count = rows[0]["cnt"] if rows else 0
    return count < get_settings().outbound.flood_threshold
```

This guard is the last line of defense if both 26.1 and 26.2 fail. It is cheap (one BQ read) and catches loops that run across multiple task executions.

### 26.4 Required `settings.yaml` Keys

These keys are configured in `settings.yaml` under the `outbound:` block. Do not hardcode their values in any source file.

```yaml
outbound:
  max_emails_per_task: 3       # Hard cap per single task execution
  max_publishes_per_task: 10   # Hard cap on Pub/Sub publishes per task
  flood_window_minutes: 60     # Time window for flood detection
  flood_threshold: 10          # Max emails in window before abort
```

**Why three layers?** The 2026-04-02 incident proved that a single configuration gate is insufficient — it can be misconfigured by any contributor who reasonably adds a new address without understanding the loop risk. Defense-in-depth means the system stays safe even when one layer has an error. The three layers are independent: code identity check, per-task counter, and time-window BQ query. All three must fail simultaneously to produce a loop.

> ⚠️ **Lesson learned — 2026-04-16:** These three outbound layers assume the input pipeline is bounded. In the 2026-04-16 duplicate reply storm, the root failure was an upstream watermark stuck on permanently-unavailable Sent messages — creating a replay loop of ~100 messages per Pub/Sub notification. The resulting Sheets 429 storm caused both idempotency guards to fail **open** (`except: pass → proceed`), generating 14–48 duplicate sends despite all outbound layers being present. **Takeaway:** outbound guards must also fail **closed** on dependency exceptions — `except: pass` on an idempotency check is a critical defect. The upstream replay cause is addressed by Rule 29.

---

## 27. Push Endpoint Verification Gate — Re-Point Before Enabling

**Rule:** After any Cloud Run redeploy that changes the service URL, **all** of the following must be updated before the service handles live traffic:

1. Every Pub/Sub push subscription pointing at that service
2. Every Cloud Scheduler job targeting that service
3. Every Apps Script Script Property containing a Cloud Run URL (`VERTEX_AGENT_ENDPOINT`)

And before enabling a **new** push subscription (or re-enabling an existing one), a synthetic probe request must return HTTP 200 from the current endpoint. Do not assume the URL is live — verify it.

> ⚠️ **Lesson learned — WORKLOG 2026-03-31:** After a nexus-prime redeploy, `nexus-prime.sub.events` continued pointing at the old URL (`nexus-prime-7bu22bxlda-uc.a.run.app`). Pub/Sub delivered silently to a dead endpoint — 404s generated a perpetual retry backlog with no alerts, no consumer-side errors, and no visibility. The subscription appeared healthy in `gcloud pubsub subscriptions list`. The only symptom was messages never being processed.

### 27.1 Post-Deploy Checklist (run after every Cloud Run redeploy)

```powershell
$PROJECT = "morphic-gaos-prod"
$SERVICE = "<service-name>"           # e.g. nexus-prime
$NEW_URL = "<new-cloud-run-url>"      # from: gcloud run services describe $SERVICE ...

# 1. Get current URL (confirm it changed)
$CURRENT_URL = gcloud run services describe $SERVICE `
    --project=$PROJECT --region=us-central1 `
    --format="value(status.url)"

# 2. List all push subscriptions pointing at the old URL and re-point them
gcloud pubsub subscriptions list `
    --project=$PROJECT `
    --filter="pushConfig.pushEndpoint~$SERVICE" `
    --format="value(name)" | ForEach-Object {
        gcloud pubsub subscriptions modify-push-config $_ `
            --push-endpoint="$NEW_URL/pubsub" `
            --project=$PROJECT
    }

# 3. List and update Cloud Scheduler jobs targeting the old URL
gcloud scheduler jobs list `
    --project=$PROJECT --location=us-central1 `
    --format="value(name,httpTarget.uri)" |
    Select-String $SERVICE   # review output; update manually if URI changed
```

### 27.2 New Push Subscription — Smoke-Test Before Enabling

Before enabling any new or re-created push subscription, send a synthetic probe and confirm 200:

```powershell
# Confirm the endpoint returns 200 before Pub/Sub starts delivering
$TOKEN = gcloud auth print-identity-token
Invoke-WebRequest -Uri "$NEW_URL/health" `
    -Headers @{ Authorization = "Bearer $TOKEN" } `
    -Method GET
```

If the endpoint returns anything other than 200, resolve the deployment issue before creating the push subscription. A push subscription enabled against a non-200 endpoint will immediately begin generating a 401/404 retry storm (see Rule 25.1 — Retry Budget).

### 27.3 Stale URL Detection

Add stale-URL detection to the observability loop. In `scripts/observability_loop.py`, periodically verify that each known push subscription's `pushEndpoint` matches the current Cloud Run URL. Log a WARNING if any mismatch is found.

```python
# ✅ Correct — verify subscriptions are pointed at current URL
for sub_name, current_cloud_run_endpoint in _KNOWN_PUSH_SUBS.items():
    sub = subscriber.get_subscription(request={"subscription": sub_name})
    endpoint = sub.push_config.push_endpoint
    if endpoint != current_cloud_run_endpoint:
        _log_cloud("nexus-prime", project_id, "task", task_id,
                   f"Stale endpoint detected: {sub_name} → {endpoint}", "WARNING")
```

**Why:** Cloud Run generates a new URL on every service creation (not just every deploy — the hash suffix changes only when the service is deleted and recreated). Subscriptions and schedulers do not update automatically. The stale-URL pattern has appeared twice in the WORKLOG already and will appear again without an automated check.

---

## 28. Explain Every Code Repair in Plain Language

**Rule:** After completing any code fix, bug repair, or defensive hardening, provide a plain-language explanation of what was broken and what the fix does — written at a 10th-grade reading level. No jargon, no assumed context.

**What the explanation must cover:**

1. **What was wrong** — describe the broken behavior in concrete terms, not in terms of the code structure ("the bouncer was counting everyone's emails, not just ours" — not "the SQL query lacked a caller filter").
2. **What could go wrong because of it** — the real-world consequence if the bug had stayed ("Ledger sending 9 invoices would freeze Nexus-Prime's outbound").
3. **What the fix does** — the corrected behavior in one or two sentences.

**Format:** Free prose, no code blocks required. Aim for 3–5 sentences total. Do not use analogies that require technical background to understand.

**When to apply:** Every time code is changed to correct a defect, close a security gap, or prevent a failure mode — including defensive changes like adding validation, fail-closed guards, or type declarations. Does not apply to purely additive work (new features, new tests for new functionality, doc-only changes).

**Why:** A fix that can't be explained in plain language usually means the root cause isn't fully understood. Forcing the explanation surfaces gaps in reasoning before the change is committed. It also creates a shared record that any contributor — human or AI — can reference without reading the diff.

---

## 29. Processing State Pointers Must Always Advance — No Partial-Skip Stalls

**Rule:** Any component that maintains a processing watermark, cursor, or offset (e.g., `gmail_last_history_id`, a Pub/Sub acknowledgement sequence, a database read cursor) **must** advance that pointer past any message that fails retrieval after max retries. A pointer that stalls on a permanently-unresolvable message creates an infinite replay loop.

> ⚠️ **Lesson learned — 2026-04-16:** `process_gmail` held `gmail_last_history_id` constant whenever `skipped_ids` was non-empty. Messages in Sent/Trash always 404 from the Inbox fetch — so the watermark never advanced. Every new Pub/Sub notification replayed the same ~100 old Sent messages, generating 100+ concurrent Sheets reads per minute. The resulting 429 storm caused idempotency guards to fail open and produced 14–48 duplicate replies to a single email.

**The correct advance condition:**

```python
# ❌ Wrong — stalls the pointer when any message is skipped
if new_history_id and not skipped_ids:
    update_watermark(new_history_id)

# ✅ Correct — always advance; 404 after max retries means permanently gone
if new_history_id:
    update_watermark(new_history_id)
    if skipped_ids:
        _log_cloud(..., f"advanced past {len(skipped_ids)} permanently unavailable messages", "WARNING")
```

**Max-retry threshold for "permanently gone":** Three consecutive 404 responses for the same message ID is sufficient evidence the message will not return. Log a WARNING with the message ID and advance the pointer. Do not hold the pointer indefinitely.

**Where this applies:**
- `process_gmail` / `gmail_last_history_id` watermark
- Any message cursor stored and resumed across invocations
- Any offset-based consumer of an ordered stream

**Why:** A stalled pointer is not a safe failure mode — it is a silent amplifier. Every downstream invocation replays the same unresolvable messages, compounding load until a rate limit or guard fails somewhere. Advancing past skipped messages is the correct fail-safe: log the skip, move the pointer forward, let Pub/Sub redeliver if the failure was transient.

---

_Last updated: 2026-04-16_
