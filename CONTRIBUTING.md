# Contributing to Morphic-G AOS

Thank you for your interest in contributing. This document covers everything you need to know before opening a pull request — code standards, spec conventions, testing requirements, and the review process.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [What We're Looking For](#what-were-looking-for)
- [Getting Started](#getting-started)
- [Project Structure](#project-structure)
- [Spec Contribution Guidelines](#spec-contribution-guidelines)
- [Code Contribution Guidelines](#code-contribution-guidelines)
- [Testing Requirements](#testing-requirements)
- [Security Rules (Non-Negotiable)](#security-rules-non-negotiable)
- [Pull Request Process](#pull-request-process)
- [Commit Message Format](#commit-message-format)
- [What Gets Rejected](#what-gets-rejected)

---

## Code of Conduct

Be direct, be constructive, assume good intent. Critique ideas and implementations — not people. Contributions that are disrespectful, dismissive, or hostile will be closed without review.

---

## What We're Looking For

The project is in active spec-and-build mode. High-value contributions right now:

| Type | Examples |
|------|---------|
| **Bug fixes** in spec documents | Incorrect API signatures, wrong IAM role names, contradictions between spec files |
| **Real-world corrections** | Gotchas discovered during implementation (like the four added in `edcbb42`) |
| **Phase 1 implementation** | `tools/google_sheets.py`, `tools/secrets.py`, `tools/pubsub.py` per the Tools Spec |
| **Test coverage** | Unit and integration tests from the checklists in `GAOS-Agent-Spec.md §9` |
| **Identity file improvements** | Clarifying agent guardrails, adding concrete examples to Knowledge Sources |

Lower priority (do not open PRs for these without prior discussion):
- Refactoring code that is not yet written
- Adding new agents or domains beyond the six defined
- Replacing Google services with non-Google equivalents
- Changing the approval gate security model

---

## Getting Started

### 1. Fork and clone

```bash
git clone https://github.com/<your-username>/Morphic-GAOS-Manager.git
cd Morphic-GAOS-Manager
git remote add upstream https://github.com/EgoNoBueno/Morphic-GAOS-Manager.git
```

### 2. Set up your environment

```powershell
uv venv
uv pip install google-cloud-secret-manager google-cloud-pubsub gspread pydantic \
               google-adk langgraph google-cloud-bigquery google-cloud-logging \
               google-cloud-aiplatform "google-genai>=1.0.0" \
               pytest pytest-mock ruff
```

> Use `google-genai>=1.0.0` — not `google-generativeai`. See `GAOS-Deploy-Spec.md §0.3` for the full SDK note.

### 3. Set up ADC (for integration tests)

Follow `GAOS-Deploy-Spec.md §0.4`. You need your own OAuth Desktop Client ID — the default gcloud client blocks the `spreadsheets` scope. Do **not** set `GOOGLE_APPLICATION_CREDENTIALS`.

### 4. Create a feature branch

```bash
git checkout -b fix/sheets-tab-quoting
# or
git checkout -b feat/tools-google-sheets
```

Use the prefix conventions: `fix/`, `feat/`, `spec/`, `test/`, `docs/`.

### 5. Sync before opening a PR

```bash
git fetch upstream
git rebase upstream/master
```

---

## Project Structure

```
Morphic-GAOS-Manager/
├── README.md
├── CONTRIBUTING.md
├── .gitignore
├── config/
│   └── settings.yaml          # The only place model aliases and project IDs live
├── tools/                     # Shared tool modules (Phase 1 build target)
│   ├── secrets.py
│   ├── google_sheets.py
│   ├── pubsub.py
│   ├── drive.py
│   ├── webhook_sender.py
│   └── project_registry.py
├── src/
│   └── agents/
│       ├── tier1/nexus-prime/ # Root orchestrator
│       └── tier2/             # Domain orchestrators (ledger, beacon, etc.)
├── tests/
│   ├── unit/
│   └── integration/
└── Docs/
    ├── GAOS-Manager-Spec.md   # Master blueprint — read this first
    ├── GAOS-Agent-Spec.md     # Agent construction requirements
    ├── GAOS-Memory-Spec.md    # Memory architecture
    ├── GAOS-Tools-Spec.md     # Tool module API reference
    ├── GAOS-Deploy-Spec.md    # Infrastructure provisioning
    ├── GAOS-Nexus-Prime-Spec.md
    └── agents/                # Identity files (persona, guardrails, knowledge sources)
```

**Read before touching anything:**
1. `Docs/GAOS-Manager-Spec.md` — the authoritative design document
2. The spec file most relevant to your change
3. Existing tests in `tests/` for the module you're touching

---

## Spec Contribution Guidelines

Spec files are the source of truth for the entire project. Errors in specs produce bugs at build time and security holes at runtime. Apply the same scrutiny to spec changes as to code changes.

### Formatting rules

- All spec files use Markdown. No HTML inside spec files except for tables where Markdown syntax is insufficient.
- Section numbers must not be changed without updating all cross-references in other spec files.
- Code blocks in spec files must specify the language: ` ```python `, ` ```bash `, ` ```yaml `.
- The `§` symbol (§) is used for section cross-references throughout the project. Use it consistently: `§3.2`, not `section 3.2` or `Section 3.2`.

### When correcting a spec

- Quote the original text in your PR description.
- State the symptom that revealed the error (a real-world failure, a contradiction with another spec section, or a tested implementation).
- If the fix changes behavior (not just wording), note which Phase the change affects.
- Do not make stylistic edits in the same commit as a correctness fix.

### When adding new content

- New sections must link back to the master spec section that motivates them.
- Do not add requirements that are not already implied by the system design. Scope creep in specs becomes mandatory work for implementers.
- If you're adding a WARNING or NOTE callout, it should describe a failure mode you have actually encountered, not a hypothetical.

---

## Code Contribution Guidelines

### Non-negotiable rules (from `GAOS-Agent-Spec.md` and `GAOS-Manager-Spec.md §15`)

These rules are enforced in code review. PRs that violate them will not be merged regardless of how useful the feature is:

1. **No hardcoded model strings.** All model references must use `settings.yaml` aliases (`LOCAL_MODEL`, `FAST_MODEL`, `DEEP_MODEL`). No literal `"gemini-2.0-flash"` in Python code.

2. **`project_id` is mandatory everywhere.** Every function that touches a Google service (Sheets, Pub/Sub, BigQuery, Secret Manager, Drive) must accept and forward `project_id`. No ambient project context from environment variables or module globals.

3. **Tools never call LLMs.** Modules in `tools/` are I/O wrappers only. They do not import `google.genai`, `google.adk`, or `langgraph`. Intelligence is applied by the agent before or after the tool call.

4. **Raise, don't swallow.** Tools raise typed exceptions on failure (see `GAOS-Tools-Spec.md` error types). Silent `return None` on API failure is forbidden. The caller decides how to handle errors.

5. **Import allowlist.** Agent code may only import from this list: `google`, `vertexai`, `langchain`, `pydantic`, `datetime`, `json`, `re`, `math`, `typing`, `collections`, `itertools`, `functools`, `logging`, `gspread`. If your implementation needs something outside this list, open a discussion first.

6. **Blocklisted calls are forbidden.** Do not use: `os.system`, `subprocess`, `eval`, `exec`, `__import__`, `open` (for arbitrary paths), `importlib`, `ctypes`, `socket`. These are caught by the static analysis gate and the PR will be rejected.

7. **No secrets in code.** No API keys, credentials, or tokens in any committed file. All secrets go through `tools/secrets.get_secret()`. If you accidentally commit a secret, rotate it immediately — do not rely on git history rewriting.

8. **Tab names with spaces must use embedded single quotes.** Range strings like `"Sales by Product!A2:D"` will 400. Use `"'Sales by Product'!A2:D"`. See `GAOS-Tools-Spec.md §3` Tab Name Quoting Rule.

### Python style

- Format with `ruff format` before committing. Config: line length 100, target Python 3.11.
- Lint with `ruff check`. Resolve all warnings; do not suppress with `# noqa` without a comment explaining why.
- Type annotations are required on all public functions. `Any` is not permitted at tool boundaries.
- Docstrings are required on all public functions in `tools/`. Follow the format in `GAOS-Tools-Spec.md` — Args, Returns, Raises.
- No bare `except:` clauses. Catch specific exception types.

```bash
# Run before every commit
ruff format .
ruff check .
pytest tests/unit/
```

### Agent construction

Before submitting any agent implementation, verify it satisfies every item in the appropriate checklist:
- Tier 2 (orchestrator): `GAOS-Agent-Spec.md §8.1` (17-item checklist)
- Tier 3 (task agent): `GAOS-Agent-Spec.md §8.2` (10-item checklist)

Include the checklist as a comment block at the top of your PR description with every item explicitly checked or noted.

---

## Testing Requirements

All PRs that touch `tools/` or `src/agents/` must include tests. The minimum bar is:

### For `tools/` modules

- Unit tests for every public function
- At least one test for each error type listed in the spec
- At least one test for the retry/backoff path (transient 429)
- Mock all external API calls — unit tests must not make live network requests

```python
# Example: testing the raise-on-missing-secret behavior
def test_get_secret_raises_secret_not_found(mocker):
    mocker.patch(
        "google.cloud.secretmanager.SecretManagerServiceClient.access_secret_version",
        side_effect=NotFound("secret not found"),
    )
    with pytest.raises(SecretNotFoundError):
        get_secret("MISSING_SECRET", "test-project")
```

### For agents

Include at minimum the five universal unit tests from `GAOS-Agent-Spec.md §9.1`:

| Test | Pass condition |
|------|---------------|
| `test_project_id_forwarded` | Every tool call in the agent receives `project_id` |
| `test_hard_stop_on_iteration_cap` | Agent halts and escalates when `step_count` hits the limit |
| `test_model_alias_only` | No literal model version string appears in the agent's code |
| `test_cost_tracked` | `AgentOutput.cost_usd` is non-zero after a model call |
| `test_startup_failure_on_missing_secret` | Agent exits cleanly with `STARTUP_FAILURE` log when a secret is missing |

### For spec-only changes

No tests required, but include the symptom that revealed the error (reproduction steps or a code snippet that triggered the failure).

---

## Security Rules (Non-Negotiable)

These apply to all contributors without exception.

**Never commit:**
- API keys, tokens, or passwords in any form
- Service account JSON key files
- `.env` files or any file containing environment variable values
- OAuth client secrets (`oauth-client.json`)

**The `.gitignore` already excludes these paths.** If git stages a file that looks like it might contain credentials, stop and check before committing.

**If you accidentally commit a secret:**
1. Do NOT push if you haven't already
2. Remove it with `git reset HEAD~1` and `git checkout -- <file>`
3. If already pushed: rotate the credential immediately, then rewrite history (`git filter-repo`) and force-push

**SHA-256 pinning:** Any code that proposes changes to agent behavior at runtime must include a `code_sha256` field. Do not circumvent the hash verification in `promote()`. This is a core security control.

**Do not open issues or PRs that contain:** real API keys (even "test" keys), real service account credentials, or real HMAC secrets for the production deployment.

---

## Pull Request Process

1. **Open an issue first** for anything non-trivial — a new module, a behavior change, or a significant spec correction. This avoids duplicate work and lets us discuss trade-offs before implementation.

2. **Keep PRs small and focused.** One logical change per PR. A PR that fixes a bug, adds a feature, and cleans up unrelated code will be asked to split.

3. **PR description must include:**
   - What changed and why
   - Which spec section(s) the change relates to
   - For code PRs: the checklist from the relevant spec
   - For spec PRs: the original text, the symptom that revealed the problem, and the corrected text

4. **All CI checks must pass** before review (ruff, pytest unit tests, import allowlist scan).

5. **One approving review** is required before merge. The maintainer may request changes; address them with new commits (do not force-push a PR branch after review starts).

6. **Squash merge** is used on merge to keep the master history linear and readable.

---

## Commit Message Format

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short description>

[optional body — what and why, not how]

[optional footer — e.g., Closes #42]
```

**Types:**

| Type | When to use |
|------|------------|
| `feat` | New functionality |
| `fix` | Bug fix in code or spec |
| `spec` | Spec-only change (no code) |
| `test` | Adding or fixing tests |
| `docs` | README, CONTRIBUTING, comments |
| `refactor` | Code restructuring without behavior change |
| `chore` | Dependency bumps, tooling config |
| `security` | Security fix — use this type for anything touching auth, secrets, HMAC, or the allowlist |

**Scope** is the module or spec file: `tools/sheets`, `tools/secrets`, `agent/ledger`, `spec/deploy`, `spec/tools`, `nexus-prime`, etc.

**Examples:**

```
fix(tools/sheets): embed single quotes in tab names with spaces

Sheets API returns 400 on range strings like "Sales by Product!A2:D".
Tab name constants must be stored as "'Sales by Product'" so the range
resolves to 'Sales by Product'!A2:D. Added Tab Name Quoting Rule to
GAOS-Tools-Spec.md §3.

spec(deploy): add ADC setup section with OAuth Desktop client requirement

The default gcloud client ID blocks the spreadsheets scope. Documented
the full setup flow including the GOOGLE_APPLICATION_CREDENTIALS gotcha.

security(tools/secrets): raise SecretAccessDenied instead of returning None

Silent None on PermissionDenied allowed agents to proceed with missing
credentials. All callers now get a typed exception and must handle it.
```

---

## What Gets Rejected

These are firm no-merges. Save everyone time by checking this list before opening a PR:

- Hardcoded model version strings in Python code
- Any use of `os.system`, `subprocess`, `eval`, or `exec`
- `google-generativeai` as a dependency (EOL — use `google-genai>=1.0.0`)
- Imports outside the allowed list without prior discussion
- Tests that mock the system under test (e.g., mocking `google_sheets.append_row` in a test of `google_sheets.append_row`)
- `# type: ignore` or `# noqa` without a comment explaining the exception
- PRs that touch more than one unrelated concern
- Spec changes without a stated symptom or justification
- Any file that contains a real credential, even in a comment

---

## Questions?

Open a [GitHub Discussion](https://github.com/EgoNoBueno/Morphic-GAOS-Manager/discussions) for design questions, implementation questions, or anything you're not sure about before writing code. Issues are for bugs and concrete feature requests.
