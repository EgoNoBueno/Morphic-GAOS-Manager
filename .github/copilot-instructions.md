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

**Timestamps must use the local system timezone — never UTC.** The project is operated from UTC−3. Do not stamp in UTC or any other zone.

> **Enforcement:** Use `datetime.now(tz=datetime.timezone(datetime.timedelta(hours=-3))).isoformat(timespec='minutes')` or a helper that reads the local zone. CI runners and collaborators in other zones should not override this — WORKLOG entries are a human record, not a machine log.

---

## 13. Documentation Must Reflect Reality — Updated After Every Completed Task

**Rule:** When a phase task is completed, all documents that describe, reference, or depend on that work must be updated in the same work session — not deferred. A task is not done until the docs match what was actually built.

**What "affected documents" means:**

| Completed work | Documents to update |
|----------------|-------------------|
| New tool or helper function | `GAOS-Tools-Spec.md`, `GAOS-Manager-Spec.md` (if referenced), `AI-Autocoding-Rules.md` (if a new rule applies) |
| New agent behavior or boot change | `GAOS-Agent-Spec.md`, the relevant `Docs/agents/<name>.md` identity file |
| Infrastructure change (GCP, Pub/Sub, Sheets, etc.) | `GAOS-Deploy-Spec.md` — update the step it affects and the verification command |
| Phase task completed | `GAOS-Deploy-Spec.md` phase exit checklist — check the item off |
| Spec contradicted by implementation reality | Correct the spec to match reality — never the other way around |
| Lesson learned or gotcha discovered | Add a `> ⚠️ **Note:**` callout at the relevant spec location |

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

> **Tooling note:** `pyproject.toml` already configures `[tool.ruff]` with `line-length = 100` and `target-version = "py311"`. `.vscode/settings.json` is checked in with Format on Save enabled. A `.pre-commit-config.yaml` running `ruff check --fix` and `ruff format` is not yet present — this is a tracked Phase 4 task. Until it exists, run both commands manually before committing. Per Rule 22 (PowerShell-native commands), the `&&` chaining operator requires **PowerShell 7+**; use the following PS 5.1-compatible sequence instead:
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

_Last updated: 2026-03-18_
