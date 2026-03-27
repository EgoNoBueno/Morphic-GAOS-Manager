# GAOS Skill Compliance Specification

**Morphic-G AOS** — Process for Reviewing and Importing External Skills

> A "skill" in this context is any externally sourced Python module — a tool module (`tools/`), an agent class, or a supporting utility — that was written outside this repository and is being considered for integration into the AOS environment.
>
> This document defines the mandatory review process that must be completed before any external skill is run inside the AOS. It is a companion to `GAOS-Agent-Spec.md` (engineering requirements) and `GAOS-Tools-Spec.md` (tool API contract), and supplements the static analysis gate defined in `GAOS-Manager-Spec.md §15.4`.

---

## How This Document Fits With GAOS-Agent-Spec.md

`GAOS-Agent-Spec.md` defines what a *completed, internally developed* agent must look like. This document addresses a different but closely related problem: **what to check when code arrives from outside the repository** before it is allowed to operate as part of the system.

The relationship is:

| Document | Scope | Primary Audience |
|---|---|---|
| `GAOS-Agent-Spec.md` | How to *build* a compliant agent from scratch | Developers writing new agents |
| `GAOS-Tools-Spec.md` | Public API contract for the tool layer | Developers writing/consuming tools |
| `GAOS-Skill-Compliance-Spec.md` (this file) | How to *validate* an externally sourced module | Anyone importing a skill; Nexus-Prime self-evolution loop |
| `GAOS-Manager-Spec.md §15.4` | Runtime static analysis gate (automated, post-import) | Automated gate; reference only |

The completion checklists in `GAOS-Agent-Spec.md §7` define *what a finished agent looks like*. The compliance review process here defines *the gates an imported skill must pass through first*. Once a skill clears all gates below, it then has a fresh obligation to satisfy the relevant `GAOS-Agent-Spec.md §7` checklist before it is considered deployable.

---

## 1. Skill Classification

Before beginning review, classify the incoming skill. Classification determines which sections of this process are mandatory.

| Classification | Description | Mandatory Gates |
|---|---|---|
| **Type A — Tool Module** | A `tools/`-style module: pure I/O wrapper, no LLM calls, no agent class | Gates 1, 2, 3, 5 |
| **Type B — Tier 3 Sub-Agent** | Stateless task runner; `Agent` subclass; `LOCAL_MODEL` only | Gates 1, 2, 3, 4, 5 |
| **Type C — Tier 2 Orchestrator** | Stateful agent with LangGraph, Pub/Sub, and Approval Gate integration | Gates 1, 2, 3, 4, 5, 6 |
| **Type D — Utility / Library** | A helper module (no Google API calls, no ADK classes) | Gates 1, 2, 5 |

If the classification is unclear, treat the skill as **Type C** (most restrictive) until proven otherwise.

---

## 2. Review Process Overview

The review proceeds through six sequential gates. A skill must pass every required gate — in order — before it may be merged. Any gate failure is a **blocking issue** that must be resolved before proceeding to the next gate.

```
[Source Acquisition]
       ↓
  Gate 1 — Supply Chain & Dependency Check
       ↓
  Gate 2 — Security Review
       ↓
  Gate 3 — Tool API Contract (Type A/B/C only)
       ↓
  Gate 4 — Agent Construction Review (Type B/C only)
       ↓
  Gate 5 — Static Analysis
       ↓
  Gate 6 — Integration Readiness (Type C only)
       ↓
[Approved for Integration — Begin GAOS-Agent-Spec.md §7 Checklist]
```

---

## 3. Source Acquisition

Before any code is read, record the following in your review notes:

| Field | Notes |
|---|---|
| Source repository URL | Must be a repository you control or have audited |
| Commit hash | Pin to an exact commit — never import from a floating branch HEAD |
| Author / maintainer | Is this a known, trusted source? |
| Licence | Must be MIT, Apache 2.0, or BSD — no GPL, AGPL, or proprietary |
| Last commit date | Skills more than 12 months out of date warrant extra scrutiny of dependencies |
| Stated purpose | One-sentence description of what the skill does |

> **Rule:** Never copy-paste code from a source you cannot re-audit. Import via a pinned Git submodule, a versioned release tarball, or direct file copy with the commit hash recorded. "I found it online" is not a trackable source.

---

## 4. Gate 1 — Supply Chain & Dependency Check

**Purpose:** Ensure the skill does not introduce new, unvetted dependencies into the project.

### 4.1 New Dependency Scan

List every `import` statement in the skill file(s). For each import not already present in `pyproject.toml`:

- [ ] Package exists on PyPI and is actively maintained (last release within 12 months)
- [ ] Package does not have known CVEs at the version that would be installed (check [osv.dev](https://osv.dev))
- [ ] Package is not a lookalike / typosquat of a trusted package (check spelling carefully)
- [ ] Licence is compatible (MIT / Apache 2.0 / BSD)

**Action:** Add approved new dependencies explicitly to `pyproject.toml` with a minimum version pin. Do not rely on transitive installs.

### 4.2 Import Allowlist Verification (Code-Executing Skills)

If the skill is intended to be called from within the Write-Test-Refine loop (or any context where Nexus-Prime or an orchestrator could cause it to run generated code), every `import` must appear in the allowlist in `GAOS-Manager-Spec.md §15.4`.

Any import not on the allowlist is an automatic **Gate 1 failure** until the allowlist is updated via the Approval Gate.

### 4.3 Gate 1 Sign-Off

| Check | Result | Notes |
|---|---|---|
| All imports identified | ☐ Pass / ☐ Fail | |
| No new CVEs | ☐ Pass / ☐ Fail | |
| Licences compatible | ☐ Pass / ☐ Fail | |
| `pyproject.toml` updated | ☐ Pass / ☐ N/A | |

---

## 5. Gate 2 — Security Review

**Purpose:** Identify vulnerabilities that could compromise the system, its data, or the wider GCP environment. This is the highest-priority gate.

### 5.1 Injection Vulnerabilities

- [ ] No user-supplied or externally sourced strings interpolated directly into API query strings (BigQuery SQL, Drive API `q=` parameter, Sheets A1 notation). All dynamic values must be parameterized (`@param` for BigQuery) or escaped per the patterns in `tools/drive.py`
- [ ] No `eval()`, `exec()`, or `compile()` calls on any string that originates from outside the process (agent messages, Sheet values, Drive file contents, Pub/Sub payloads)
- [ ] No shell command construction from user-supplied data (`os.system`, `subprocess.run`, etc.) — see `GAOS-Manager-Spec.md §15.4` blocklist

### 5.2 Credential Handling

- [ ] No hardcoded secrets, API keys, tokens, or GCP project IDs anywhere in the skill
- [ ] All secrets retrieved exclusively via `tools/secrets.get_secret()` — no direct `google.cloud.secretmanager` calls unless this *is* a replacement for `tools/secrets.py`
- [ ] No credential files written to disk — service account JSON is loaded from Secret Manager at runtime only
- [ ] No `GOOGLE_APPLICATION_CREDENTIALS` environment variable dependency

### 5.3 SSRF and Network Safety

- [ ] Any outbound HTTP(S) call validates that the destination URL is `https://` scheme
- [ ] Any outbound HTTP(S) call validates that the destination is not a private/internal IP address (RFC 1918: `10.x`, `172.16–31.x`, `192.168.x`; loopback: `127.x`; link-local: `169.254.x`)
- [ ] Webhook or callback URLs sourced from Secret Manager are validated before use (pattern established in `tools/webhook_sender.py`)

### 5.4 Path Traversal

- [ ] File or folder paths derived from external input are validated to reject `..` components before any file system or Drive API lookup
- [ ] Relative paths are constrained to a known root (e.g., the Drive `Knowledge/` folder — never allowed to escape above the root)

### 5.5 Data Integrity

- [ ] `json.loads()` on any externally sourced string is wrapped in `json.JSONDecodeError` handling
- [ ] Pub/Sub message decoding follows the `decode_push_message()` pattern in `tools/pubsub.py` — no raw `base64.b64decode()` without schema validation
- [ ] No `pickle`, `marshal`, or `yaml.load()` (unsafe) on untrusted data — use `yaml.safe_load()` only

### 5.6 Scope of Access

- [ ] The skill requests only the GCP API scopes it strictly requires
- [ ] If the skill requests full Drive access (scope: `googleapis.com/auth/drive`), confirm this is necessary and add a comment explaining why a narrower scope (`drive.file`, `drive.readonly`) cannot be used

### 5.7 Gate 2 Sign-Off

| Area | Result | File:Line | Notes |
|---|---|---|---|
| Injection | ☐ Pass / ☐ Fail | | |
| Credential handling | ☐ Pass / ☐ Fail | | |
| SSRF / network | ☐ Pass / ☐ Fail | | |
| Path traversal | ☐ Pass / ☐ Fail | | |
| Data integrity | ☐ Pass / ☐ Fail | | |
| Scope of access | ☐ Pass / ☐ Fail | | |

---

## 6. Gate 3 — Tool API Contract (Type A / B / C)

**Reference:** `GAOS-Tools-Spec.md §1`

**Purpose:** Ensure the skill's public API is compatible with the tool layer conventions that all agents depend on.

### 6.1 `project_id` Scoping

- [ ] Every public function that touches a Google service accepts `project_id` as a required parameter
- [ ] No function reads from a module-level global, environment variable, or `settings.yaml` directly for `project_id` — it always comes from the caller
- [ ] `project_id` is forwarded to every downstream tool call; it is never silently dropped

### 6.2 Error Contract

- [ ] All public functions raise module-defined, named exception classes on failure (e.g., `DriveReadError`, `TopicNotFoundError`) — not bare `Exception`, `RuntimeError`, or `ValueError`
- [ ] No function returns `None` to indicate failure — it raises
- [ ] `raise ... from exc` is present on all re-raises inside `except` blocks (no implicit exception chaining loss — ruff `B904`)

### 6.3 Retry Behaviour

- [ ] HTTP 429 (rate limit) and 5xx (server error) responses are retried with exponential backoff — minimum 3 attempts, minimum `time.sleep(2 ** attempt)` between retries
- [ ] 4xx client errors (except 429) are raised immediately without retry
- [ ] 403 Forbidden errors raise a `PermissionError`-derived exception with a clear message indicating which service account lacks which permission

### 6.4 Statelessness

- [ ] No module-level mutable state (caches, counters, connection objects) unless protected by a `threading.Lock` and the locking rationale is documented
- [ ] No LLM calls inside tool functions — tools are I/O wrappers only
- [ ] No side effects beyond the documented operation (e.g., a read function must not write; a write function must not delete unrelated data)

### 6.5 Batch Support

- [ ] If the skill performs an operation that callers might invoke in a loop (e.g., appending rows to a Sheet, publishing messages), a batch variant exists that completes the operation in a single API call

### 6.6 Gate 3 Sign-Off

| Check | Result | File:Line | Spec Ref |
|---|---|---|---|
| `project_id` on all public functions | ☐ Pass / ☐ Fail | | `GAOS-Tools-Spec.md §1` |
| Named exception classes | ☐ Pass / ☐ Fail | | `GAOS-Tools-Spec.md §1` |
| Retry with backoff | ☐ Pass / ☐ Fail | | `GAOS-Tools-Spec.md §1` |
| Stateless | ☐ Pass / ☐ Fail | | `GAOS-Tools-Spec.md §1` |
| Batch variants present | ☐ Pass / ☐ N/A | | `GAOS-Tools-Spec.md §1` |

---

## 7. Gate 4 — Agent Construction Review (Type B / C)

**Reference:** `GAOS-Agent-Spec.md §2`, `§3`, `§4`

**Purpose:** Ensure the skill is a well-formed ADK agent that can operate within the tier hierarchy.

### 7.1 Common Requirements (All Agent Types — `GAOS-Agent-Spec.md §2`)

- [ ] Subclasses Google ADK `Agent`
- [ ] `name` field: `snake_case`, unique across the system
- [ ] `description` field: one sentence, present
- [ ] `model` field: resolved from `settings.yaml` alias — **no hardcoded Gemini version strings** (e.g., `"gemini-1.5-pro"` is forbidden; `settings.FAST_MODEL` is required)
- [ ] `instruction` field: populated by `_load_identity_file(agent_name)` from `agents/__init__.py` — do **not** assign a raw string literal. `_load_identity_file()` automatically appends the Context Trio (owner business context, brand voice, and operating rules from `Docs/about-me.md`, `Docs/brand-voice.md`, `Docs/working-preferences.md`) after the agent-specific identity file — no additional wiring is required.
- [ ] `tools` field: explicit list — no wildcard, no inheritance from parent agent
- [ ] `AgentInput` and `AgentOutput` Pydantic schemas defined — no bare `dict` or `Any` typed agent boundary fields
- [ ] `task_id` and `project_id` present in both input and output schemas
- [ ] `cost_usd` tracked per model call and returned in `AgentOutput`
- [ ] Cloud Logging labels applied: `agent_id`, `project_id`, `log_type`, `task_id`
- [ ] Boot sequence (`GAOS-Agent-Spec.md §7`) implemented if this is a long-running agent

### 7.2 Tier 3 Sub-Agent (`GAOS-Agent-Spec.md §4`)

- [ ] Stateless — no LangGraph graph
- [ ] All errors caught and returned as `AgentOutput(status="escalated"|"failed")` — no unhandled exceptions propagate to the orchestrator
- [ ] Tool scope minimised — only tools required for the single stated task
- [ ] `LOCAL_MODEL` used — **no `DEEP_MODEL` calls anywhere**
- [ ] `project_id` inherited from `AgentInput` and forwarded to all tool calls

### 7.3 Tier 2 Orchestrator (`GAOS-Agent-Spec.md §3`)

- [ ] Identity file (`Docs/agents/<name>.md`) exists and contains all 8 required sections (`GAOS-Agent-Spec.md §3.1`)
- [ ] Universal Don'ts present in identity file (never self-approve; never write to another domain's tabs; never call `DEEP_MODEL` for routine tasks)
- [ ] LangGraph graph declared with at minimum all 7 required nodes: `plan`, `dispatch`, `collect`, `report`, `park`, `resume`, `escalate`
- [ ] Pub/Sub outbound topic named per convention: `<project_id>/agent/<name>/events`
- [ ] Subscriptions to Nexus-Prime broadcast and required cross-domain topics
- [ ] All published messages use `A2AMessage` schema (including `project_id`)
- [ ] Dashboard heartbeat writes at end of every work cycle with all required fields
- [ ] Approval Gate `park`/`resume` cycle implemented — no busy-waits on human approval
- [ ] Write-Test-Refine loop: iteration cap, TTL, cost cap, no-progress detector all enforced
- [ ] Evolved code proposals sent through the Approval Gate before deploy
- [ ] Model selection follows `GAOS-Agent-Spec.md §3.7` table; any `DEEP_MODEL` deviation has a justification comment

### 7.4 Gate 4 Sign-Off

| Check | Result | Tier Required | Spec Ref |
|---|---|---|---|
| ADK `Agent` subclass | ☐ Pass / ☐ Fail | All | `GAOS-Agent-Spec.md §2.1` |
| Model alias (no hardcode) | ☐ Pass / ☐ Fail | All | `GAOS-Agent-Spec.md §2.1` |
| Pydantic schemas, no bare dict | ☐ Pass / ☐ Fail | All | `GAOS-Agent-Spec.md §2.2` |
| `project_id` in I/O schemas | ☐ Pass / ☐ Fail | All | `GAOS-Agent-Spec.md §2.3` |
| Logging labels | ☐ Pass / ☐ Fail | All | `GAOS-Agent-Spec.md §2.4` |
| Stateless design | ☐ Pass / ☐ Fail | Tier 3 | `GAOS-Agent-Spec.md §4.2` |
| No `DEEP_MODEL` | ☐ Pass / ☐ Fail | Tier 3 | `GAOS-Agent-Spec.md §4.4` |
| Identity file complete | ☐ Pass / ☐ Fail | Tier 2 + 3 | `GAOS-Agent-Spec.md §3.1 / §4.1` |
| LangGraph 7-node minimum | ☐ Pass / ☐ Fail | Tier 2 | `GAOS-Agent-Spec.md §3.2` |
| Pub/Sub wiring | ☐ Pass / ☐ Fail | Tier 2 | `GAOS-Agent-Spec.md §3.3` |
| Dashboard heartbeat | ☐ Pass / ☐ Fail | Tier 2 | `GAOS-Agent-Spec.md §3.4` |
| Approval Gate integration | ☐ Pass / ☐ Fail | Tier 2 | `GAOS-Agent-Spec.md §3.5` |

---

## 8. Gate 5 — Static Analysis

**Purpose:** Machine-verifiable correctness and style conformance.

Run all commands from the repository root with the venv active.

```powershell
# Activate virtual environment first
& .venv\Scripts\Activate.ps1

# Step 1 — Ruff lint (project ruleset: E, F, I, UP, B, SIM; E501 ignored)
python -m ruff check <skill_file_or_directory>

# Step 2 — Ruff format check (do not auto-fix yet; review diffs manually)
python -m ruff format --check <skill_file_or_directory>

# Step 3 — Mypy type check
python -m mypy <skill_file_or_directory> --ignore-missing-imports

# Step 4 — Verify no call or pattern is in the blocklist (GAOS-Manager-Spec.md §15.4)
# Blocked builtins (caught by AST call detection in validate_code_safety()):
#   exec, eval, compile, __import__, breakpoint
# Blocked string patterns (substring match across the full code string):
#   os.system, os.popen, subprocess.call, subprocess.run, subprocess.Popen,
#   __builtins__, ctypes., socket.connect, pickle.loads, pickle.load
# Requests and other non-allowlisted imports are caught by Gate 2 (import allowlist),
# not by the pattern gate — they will fail the allowlist check, not this step.
python -m ruff check --select S <skill_file_or_directory>   # bandit-equivalent security rules

# Step 5 — Verify imports are sorted (ruff I001)
python -m ruff check --select I <skill_file_or_directory>
```

All five steps must exit with code `0` before Gate 5 is cleared.

**Auto-fix policy:** Run `python -m ruff check --fix` and `python -m ruff format` only after a human has reviewed the diff and confirmed the fixes are safe. Never auto-fix security or logic issues.

### 8.1 Gate 5 Sign-Off

| Tool | Exit Code | Notes |
|---|---|---|
| `ruff check` | ☐ 0 / ☐ Non-zero | |
| `ruff format --check` | ☐ 0 / ☐ Non-zero | |
| `mypy` | ☐ 0 / ☐ Non-zero | |
| Blocklist scan | ☐ Clean / ☐ Findings | |
| Import order | ☐ 0 / ☐ Non-zero | |

---

## 9. Gate 6 — Integration Readiness (Type C — Tier 2 Orchestrators)

**Purpose:** Confirm the orchestrator can be wired into the running AOS without disrupting existing agents.

### 9.1 Naming Uniqueness

- [ ] `Agent.name` is unique — does not conflict with any name in `Docs/agents/`
- [ ] Pub/Sub topic name follows the convention and does not collide with an existing topic

### 9.2 Sheet Tab Ownership

- [ ] The orchestrator's designated dashboard tab exists in the workbook (or a provisioning note is created in `GAOS-Deploy-Spec.md`)
- [ ] The orchestrator does not attempt to write to any tab owned by another orchestrator

### 9.3 Cross-Domain Subscriptions

- [ ] Any cross-domain Pub/Sub subscriptions required by the skill are listed in `GAOS-Manager-Spec.md §10.1` or a proposal has been submitted to add them

### 9.4 Boot Sequence Dry Run

Before deploying to Cloud Run, verify the boot sequence locally:

```powershell
# Start with STARTUP_ONLY=true to exercise steps 1-6 without entering the event loop
$env:STARTUP_ONLY = "true"
python -c "from agents.<name>.orchestrator import <AgentClass>; agent = <AgentClass>(); agent.boot()"
```

Expected: No exceptions; an IDLE heartbeat row appears in the Sheet within 60 seconds.

### 9.5 Gate 6 Sign-Off

| Check | Result | Notes |
|---|---|---|
| Name is unique | ☐ Pass / ☐ Fail | |
| Tab ownership correct | ☐ Pass / ☐ Fail | |
| Cross-domain subscriptions documented | ☐ Pass / ☐ N/A | |
| Boot sequence dry run | ☐ Pass / ☐ Fail | |

---

## 10. Review Output — Required Findings Table

After completing all required gates, produce a findings table in this format and attach it to the Approval Gate proposal row (see §11).

| # | Gate | Check | Result | File:Line | Spec Reference | Action Required |
|---|---|---|---|---|---|---|
| 1 | Gate 2 | No injection in query strings | ☐ Pass / ☐ Fail | `tools/x.py:42` | `GAOS-Tools-Spec.md §1` | Parameterize `name` value |
| 2 | Gate 3 | `project_id` on all public functions | ☐ Pass / ☐ Fail | `tools/x.py:10` | `GAOS-Tools-Spec.md §1` | Add `project_id` param to `fetch_data()` |
| … | | | | | | |

Every **Fail** row is a blocking issue. The skill may not be merged until all Fail rows are resolved and re-reviewed.

---

## 11. Approval Gate Submission

Once all required gates are cleared, submit the integration as a **Priority-3 proposal** through the standard Approval Gate:

1. Write a proposal row to `Agent_Approvals` with:
   - `proposal_type`: `skill_import`
   - `description`: Skill name, source repo, commit hash, classification (Type A/B/C/D)
   - `justification`: Why this skill is needed; which capability gap it fills
   - `review_summary`: Link to or inline copy of the Gate 1–6 findings table
   - `risk_level`: `low` (Type D), `medium` (Type A/B), `high` (Type C)
2. Attach the diff of changed files in the proposal row (or link to the PR).
3. Publish an `APPROVAL_REQUEST` to `agent/approvals/events` targeting `nexus-prime`.
4. Do not merge until the `Agent_Approvals` row status is `Approved`.

---

## 12. Post-Merge Obligations

After the proposal is approved and the skill is merged, the following must be completed before the skill is considered fully integrated:

1. **Update `GAOS-Agent-Spec.md §8`** — add the new agent's completion checklist entry to the Implementation Checklist in `GAOS-Manager-Spec.md §17` with status `In Progress`.
2. **Write unit tests** — satisfy `GAOS-Agent-Spec.md §9.1` (U1–U5) for all agent skills; `GAOS-Agent-Spec.md §9.3` if the skill is code-producing.
3. **Add integration tests** — for Tier 2 orchestrators, satisfy `GAOS-Agent-Spec.md §9.2` (I1–I6).
4. **Update `GAOS-Deploy-Spec.md`** — add any new secrets, Pub/Sub topics, or Sheet tabs that must be provisioned.
5. **Add identity file** — if not already present, create `Docs/agents/<name>.md` per `GAOS-Manager-Spec.md §18.2`. The Context Trio (`about-me.md`, `brand-voice.md`, `working-preferences.md`) is appended automatically by `_load_identity_file()` — no additional wiring is required in the identity file or the orchestrator.

---

## 13. Reference Index

| Topic | Location |
|---|---|
| Tool API design principles | `GAOS-Tools-Spec.md §1` |
| Agent tier obligations table | `GAOS-Agent-Spec.md §1` |
| Universal agent requirements | `GAOS-Agent-Spec.md §2` |
| Tier 2 orchestrator requirements | `GAOS-Agent-Spec.md §3` |
| Tier 3 sub-agent requirements | `GAOS-Agent-Spec.md §4` |
| Agent completion checklists | `GAOS-Agent-Spec.md §8` |
| Testing requirements | `GAOS-Agent-Spec.md §9` |
| Static analysis gate / import blocklist | `GAOS-Manager-Spec.md §15.4` |
| Approval Gate architecture | `GAOS-Manager-Spec.md §14` |
| Agent identity file template | `GAOS-Manager-Spec.md §18.2` |
| Pub/Sub topic topology | `GAOS-Manager-Spec.md §10` |
| Secrets management | `GAOS-Manager-Spec.md §15.1` |
| Write-Test-Refine loop constraints | `GAOS-Manager-Spec.md §13.1` |
| Infrastructure provisioning | `GAOS-Deploy-Spec.md` |
| Nexus-Prime construction requirements | `GAOS-Nexus-Prime-Spec.md` |
| Agent behavioral identity + `think` node spec | `GAOS-Persona-Spec.md` |
| Deployer and end-user onboarding | `GAOS-Onboarding-Spec.md` |
