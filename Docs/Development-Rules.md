# Development Rules — Morphic-G AOS

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

**Rule:** Every tool call — `insert_row()`, `get_secret()`, `publish_message()`, `load_domain_memory()`, `log_cloud()` — must receive `project_id` explicitly. Never use a global or module-level default.

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

**Rule:** Do not commit code that breaks the existing test suite. Run `pytest` before every commit. The current baseline is **62 tests, 0 failures**.

If a new feature legitimately makes an existing test incorrect (e.g., an interface change), update the test in the same commit — not in a follow-up.

```powershell
# Run before every commit
pytest --tb=short
```

---

## 12. WORKLOG.md Is Updated Before and After Every Work Block

**Rule:** Every development session must open with a WORKLOG entry (`## YYYY-MM-DDTHH:MM — Started`) and close with a completion entry. The entry must capture: what was done, what files changed, what tests were added, and what's next.

New entries go at the **top** of WORKLOG.md (most recent first).

---

## 13. Documentation Must Reflect Reality — Updated After Every Completed Task

**Rule:** When a phase task is completed, all documents that describe, reference, or depend on that work must be updated in the same work session — not deferred. A task is not done until the docs match what was actually built.

**What "affected documents" means:**

| Completed work | Documents to update |
|----------------|-------------------|
| New tool or helper function | `GAOS-Tools-Spec.md`, `GAOS-Manager-Spec.md` (if referenced), `Development-Rules.md` (if a new rule applies) |
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

**What not to do:**
- Do not stop at an arbitrary "seems like enough for one session" point and leave work half-done
- Do not pause to ask for permission to continue steps that were already implicitly authorized by the original request
- Do not abandon a working approach mid-execution because it turned out to be more steps than anticipated

**The standard:** A task is done when it is done — tests pass, docs are updated, and the deliverable matches the original intent. Duration is not a stopping condition. Proximity to success is.

---

_Last updated: 2026-03-16_
