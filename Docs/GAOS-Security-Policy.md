# GAOS Zero Trust Security Policy for Agentic Systems

**Morphic-G AOS** — Organizational security policy governing all AI agents, tools, and orchestration infrastructure.

> **Scope:** This policy applies to all seven GAOS orchestrators (`beacon`, `foreman`, `ledger`, `nexus-prime`, `pursuit`, `scout`, `steward`), all tools in `tools/`, all Apps Script components, and any generated or approved skills deployed to Vertex AI.
>
> **Authority:** Violations of any **MUST** requirement are hard-stops. Violations of **SHOULD** requirements require a documented exception filed as a Priority-4 Approval Gate proposal.
>
> **Related specs:** `GAOS-Manager-Spec.md` §14–15, `GAOS-Privacy-Spec.md`, `AI-Autocoding-Rules.md` §4–5, `GAOS-Deploy-Spec.md` §3.

---

## Why Agentic Systems Require a Dedicated Policy

Traditional software operates deterministically: identical inputs produce identical outputs, and every execution path is written by a human. AI agents break both assumptions.

1. **Probabilistic output** — The same prompt may produce different reasoning and different tool calls on different runs. Static code review cannot enumerate every possible execution path.
2. **Autonomous tool execution** — Agents act on goals, not instructions. An agent given a goal can chain tool calls the developer never anticipated.
3. **Expanded attack surface** — Every tool endpoint, Pub/Sub topic, Sheet tab, and LLM prompt is a potential attack vector. This system has 22 Pub/Sub subscriptions, 14 tool wrappers, and an LLM boundary at each model call.
4. **Amplification risk** — A compromised agent does not stop. It continues operating at machine speed until stopped externally. The blast radius of a compromised autonomous agent is orders of magnitude larger than a compromised script.

Zero Trust is the correct mental model: **never trust, always verify** — for agents, their inputs, their tools, and their outputs.

---

## 1. Identity and Access Management

### 1.1 Non-Human Identities

**Policy:** Every agent MUST have a unique, dedicated service account. No credential sharing.

| Agent | Service Account |
|-------|----------------|
| `nexus-prime` | `nexus-prime-sa@<project>.iam.gserviceaccount.com` |
| `beacon` | `beacon-sa@<project>.iam.gserviceaccount.com` |
| `foreman` | `foreman-sa@<project>.iam.gserviceaccount.com` |
| `ledger` | `ledger-sa@<project>.iam.gserviceaccount.com` |
| `pursuit` | `pursuit-sa@<project>.iam.gserviceaccount.com` |
| `scout` | `scout-sa@<project>.iam.gserviceaccount.com` |
| `steward` | `steward-sa@<project>.iam.gserviceaccount.com` |

- Shared credentials are **prohibited**. A security event must be attributable to exactly one agent with no ambiguity.
- Service accounts are not granted the `roles/owner` or `roles/editor` project-level roles under any circumstances.
- SA keys MUST NOT be generated or exported. All authentication uses Workload Identity Federation (Cloud Run → federated identity) or ADC via the metadata server. There are no JSON key files in the repository or in Secret Manager.

### 1.2 Principle of Least Privilege

**Policy:** Every agent MUST have only the IAM permissions required to complete its defined function, and no others.

Verified via `gcloud run services get-iam-policy` after every deployment. No agent has `allUsers` bindings.

Current minimum permission sets are defined in `GAOS-Deploy-Spec.md` §3.2 and enforced by `infra/main.tf`.

**Forbidden escalations:**
- Agents MUST NOT call `gcloud iam` or equivalent SDK methods to modify their own IAM bindings.
- Agents MUST NOT request or accept `roles/iam.serviceAccountAdmin`, `roles/owner`, or `roles/editor`.
- Self-escalation detected in generated code is an automatic Gate 2 failure (`validate_code_safety()`) and triggers a hard stop.

### 1.3 Secret Access

**Policy:** Secrets are fetched at call-time via `tools.secrets.get_secret(name, project_id)`. They MUST NOT be:

- Hardcoded in source code
- Present in `settings.yaml` or any config file
- Logged in structured logs or WORKLOG
- Passed as environment variables in plaintext to Cloud Run

Violation detection: `detect-secrets` pre-commit hook runs on every commit. Any finding blocks the push.

> **Exception:** `OLLAMA_HOST` has a local-dev default when Secret Manager is unavailable. This is not a credential and does not represent a secret exposure.

### 1.4 Audit Trail for Identity Events

All of the following identity-related events are logged via `_log_cloud()` with `severity="SECURITY"` and must never be suppressed:

| Event ID | Trigger |
|----------|---------|
| `HMAC_FAILURE` | Webhook signature mismatch — potential replay or forgery |
| `JWT_VERIFY_FAIL` | Chat JWT verification failed — potential impersonation |
| `STARTUP_FAILURE` | Missing secret at boot — potential misconfiguration or secret deletion |
| `CODE_HASH_MISMATCH` | Post-approval code tamper — potential insider attack |
| `APPROVAL_RBAC_BLOCK` | Approver tier insufficient for proposal priority |

> **Escalation threshold:** If `HMAC_FAILURE` events exceed 5 in any 10-minute window, Nexus-Prime MUST publish a Priority-5 `ESCALATION`. This pattern indicates active probing or replay attack.
>
> If `APPROVAL_RBAC_BLOCK` events exceed 3 in any 30-minute window, Nexus-Prime MUST publish a Priority-4 `ALERT`.

---

## 2. Prompt Injection Prevention

Prompt injection is the primary attack vector against LLM-based systems. An attacker embeds instructions in content the agent reads (email bodies, document text, spreadsheet cells, search results, tool responses) to hijack the agent's goal.

### 2.1 Input Boundaries

**Policy:** Data that originates outside the system boundary MUST be treated as untrusted input and MUST NOT be spliced directly into system prompts without explicit structural separation.

Structural separation means the injection-vulnerable content is passed as a distinct, labeled field — never concatenated into the prompt preamble that carries authority.

**GAOS implementation:** The `DEEP_MODEL` prompt uses:
```python
f"""
=== SYSTEM CONTEXT ===
{system_context}

=== UNTRUSTED EXTERNAL CONTENT ===
{external_data}
"""
```

The agent MUST be instructed to treat the `UNTRUSTED EXTERNAL CONTENT` section as data only — never as commands.

### 2.2 Tool Response Sanitization

**Policy:** Tool responses that include external content (web search results, scraped text, email bodies) MUST NOT be forwarded verbatim into the next LLM prompt without a content marker indicating provenance.

The following tools return potentially injected content. They MUST include a provenance marker in their response:

| Tool | Risk source | Required marker |
|------|------------|-----------------|
| `tools/web_search.py` | Search result snippets | `[WEB_RESULT]` prefix |
| `tools/google_search.py` | External web content | `[WEB_RESULT]` prefix |
| `tools/google_docs.py` | Human-authored Doc content | `[DOC_CONTENT]` prefix |
| `tools/vertex_search.py` | RAG corpus content | `[CORPUS_CONTENT]` prefix |

> **Current status:** Provenance markers are a SHOULD for Phase 4, required by Phase 5. Track as a known hardening gap.

### 2.3 Monitoring for Injection Signals

Nexus-Prime MUST log a `PROMPT_INJECTION_SUSPECTED` event (severity: HIGH) when any tool response contains patterns consistent with attempted injection:

- Strings containing `SYSTEM:`, `IGNORE PREVIOUS`, `NEW INSTRUCTION:`, `[INST]`
- Embedded role-switching tokens characteristic of instruction injection

This log event MUST be published as a Priority-4 `ALERT` to the owner space.

---

## 3. Code Safety Gates

All agent-generated code passes through two static-analysis gates before any human reviews it. Both gates are enforced in `validate_code_safety()` in `agents/__init__.py`. Failure at either gate is a hard stop — code is never submitted to the Approval Gate.

### Gate 1 — Blocked Patterns (AST Walk)

The following call patterns are unconditionally blocked:

| Pattern | Risk |
|---------|------|
| `os.system`, `os.popen` | Shell command execution |
| `subprocess.call`, `subprocess.run`, `subprocess.Popen` | Process spawning |
| `pickle.loads`, `pickle.load` | Arbitrary object deserialization |
| `eval`, `exec`, `compile`, `__import__` | Dynamic code evaluation / import bypass |
| `__builtins__`, `ctypes.` | Sandbox escape |
| `socket.connect` | Raw network access |

### Gate 2 — Import Allowlist

Every `import` and `from … import` statement must use a module from the approved set. The list is hardcoded as `_ALLOWED_IMPORTS` in `agents/__init__.py`. Adding any entry requires a Priority-4 Approval Gate proposal and a passing test run.

**Permitted groups:**
- Standard library: `datetime`, `json`, `math`, `re`, `uuid`, `hashlib`, `time`, `typing`, `dataclasses`, `collections`, `functools`, `itertools`, `pathlib`, `enum`, `abc`, `copy`, `textwrap`
- Google Cloud SDKs: `google.cloud.bigquery`, `google.cloud.logging`, `google.cloud.pubsub`, `google.cloud.secretmanager`, `google.cloud.storage`, `google.adk`, `google.genai`, `google.auth`
- Third-party: `gspread`, `pydantic`, `yaml`, `langgraph`
- Internal: `config`, `models`, `tools`, `agents`

### Gate 3 — SHA-256 Integrity Pinning

When code passes both gates and is submitted to the Approval Gate, the SHA-256 hash of the proposed code is computed and stored in `Agent_Approvals.code_sha256` (col M). At deploy time (`syncSkillsToVertex`), the hash is recomputed and compared. A mismatch triggers `BLOCKED_TAMPERED` and a Priority-5 alert — the sheet cell is protected from non-owner edits, but hash verification is the cryptographic backstop.

---

## 4. Human-in-the-Loop Controls

### 4.1 Approval Gate

Agents MUST NOT deploy code to Vertex AI autonomously. Every proposed skill change goes through:

1. `validate_code_safety()` — both gates must pass (hard stop otherwise)
2. SHA-256 hash computed and stored
3. Row written to `Agent_Approvals` Sheet tab with `Status = Pending`
4. Chat card notification to owner space
5. Human reviews, approves, rejects, or requests revision
6. `syncSkillsToVertex` hash-verifies and deploys on approval

No bypass exists. The Sheet column is owner-protected.

### 4.2 Priority-Based Escalation

The `MessageType.ESCALATION` and `MessageType.ALERT` message types exist for agents to request human attention. All escalations surface in the owner's Chat space.

| Priority | Description | Response SLA |
|----------|-------------|--------------|
| 5 — Critical | Security event, hash tamper, active attack signal | Immediate |
| 4 — High | RBAC block threshold, injection suspected, import gate fail | Within 1 hour |
| 3 — Standard | Budget threshold, unexpected agent state | Within 24 hours |
| 2 — Informational | Config change proposal, model upgrade | Within 72 hours |
| 1 — Low | General operational events | Best effort |

### 4.3 Hard-Stop Behavior

A `hard_stop_triggered = True` state causes the agent to:
1. Log the event via `_log_cloud()` with severity `CRITICAL`
2. Publish a Priority-5 `ESCALATION` to Nexus-Prime
3. Cease processing the current task
4. **Not** retry, not attempt a workaround, not silently continue

Hard stops are not catchable errors — they are permanent halts on the triggering task. The task state is set to `failed` and the owner is notified.

---

## 5. Data Protection

### 5.1 Data Classification

| Category | Examples | Storage | Leaves GCP? |
|----------|----------|---------|-------------|
| **Operational** | Invoices, inventory, HR records | Google Sheets, BigQuery | No |
| **Reasoning traces** | Monologue frames, task history | BigQuery `aos_logs` | No |
| **Agent credentials** | API keys, HMAC secrets | Secret Manager only | No |
| **Approved knowledge** | Business rules, skill files | Vertex AI Memory Bank, Drive | Inference only |
| **Prompt payloads** | Task instructions + context sent to Gemini | Vertex AI inference endpoint | Yes — GCP DPA |

### 5.2 Data Residency

All operational data is stored in `us-central1` (Cloud Run, Pub/Sub) and `us-east1` (BigQuery) within the `morphic-gaos-prod` GCP project. No data is stored in third-party systems outside the GCP project boundary except:

- LLM inference payloads sent to Vertex AI Gemini models (governed by GCP Data Processing Addendum — Google does not use this data to train models)
- Ollama — inference on local hardware only, no external transmission

### 5.3 Data Loss Prevention Signals

Agents MUST log a `DATA_EXFILTRATION_SUSPECTED` event (severity: CRITICAL) if a tool call returns data that matches:

- Secret Manager secret names (`GEMINI_API_KEY`, `WEBHOOK_HMAC_SECRET`, etc.)
- GCP project IDs in a plaintext form being written to an external endpoint
- API key patterns (`AIza...`, bearer tokens) appearing in any Pub/Sub message payload

This event triggers an immediate Priority-5 escalation. The task is hard-stopped.

### 5.4 Retention and Archival

| Data | Active retention | Archive retention |
|------|-----------------|-------------------|
| Sheet Logs tab rows | 30 days | 24 months (BigQuery) |
| Approval Gate history | 90 days | 24 months (BigQuery) |
| Cloud Logging entries | 7 days | Not retained |
| Monologue frames | Indefinite (BigQuery) | N/A |

Retention is enforced by the nightly archive job (`POST /archive`). Manual deletion of archive data from BigQuery requires an owner-approved Priority-3 proposal.

---

## 6. Observable Traces and Audit Logging

### 6.1 Structured Logging Requirement

Every agent action that results in an external side effect (Sheet write, BQ insert, Pub/Sub publish, LLM call, secret access) MUST produce a structured `_log_cloud()` entry with:

- `agent_id` — which agent
- `project_id` — which project boundary
- `task_id` — correlation ID for the task chain
- `message` — human-readable description of the action and outcome
- `severity` — one of `DEBUG | INFO | WARNING | ERROR | CRITICAL`

`print()` is prohibited in all agent and tool code. All output goes through `_log_cloud()`.

### 6.2 Reasoning Traces

The `MonologueFrame` data model captures:
- What the agent was thinking at each decision node
- Which tools it considered and why it selected or rejected them
- The reasoning for any escalation or hard stop

These traces are persisted to `BigQuery.aos_logs.monologue_frames`. They are not optional — they are the audit record that enables post-incident analysis.

### 6.3 Cross-Domain Message Tracing

All inter-agent Pub/Sub messages include `task_id` and `correlation_id`. The Observability Log Sheet tab records every cross-domain message with both IDs, enabling full trace reconstruction for any given task chain.

---

## 7. Boundary Enforcement and Sandboxing

### 7.1 Network Isolation

All 7 Cloud Run services run with `--no-allow-unauthenticated`. No service is reachable from the public internet without a valid OIDC token from an authorized identity. Inbound paths:

| Path | Caller | Auth |
|------|--------|------|
| `/pubsub` | Pub/Sub push subscription | OIDC token — `pubsub-push-sa` |
| `/sync` | Apps Script / manual E2E | OIDC token — `nexus-prime-sa` |
| `/chat` | Google Chat | Google-signed JWT |
| `/health` | Cloud Run liveness probe | OIDC token |
| `/archive`, `/ttl-sweep`, `/poll-comments`, `/daily-kickoff` | Cloud Scheduler | OIDC token |

### 7.2 Acceptable Agency Scope

Each agent is constrained to a defined set of tools and Pub/Sub topics. Agents MUST NOT:

- Publish to topics outside their defined subscription set
- Write to Sheet tabs owned by other agents
- Call GCP APIs that are not listed in their `intra/main.tf` IAM bindings
- Invoke Vertex AI model endpoints not aliased in `settings.models.*`

Any tool call that would exceed these boundaries fails with an IAM error, which is logged and escalated.

### 7.3 Pub/Sub Topic Ownership

Each agent owns exactly one inbound topic (`agent.<name>.events`) and the subscriptions on it. Cross-domain communication goes through Nexus-Prime or a designated relay topic. Agents do not subscribe to each other's topics directly.

---

## 8. Threat Detection and Response

### 8.1 Real-Time Monitoring Signals

The following conditions MUST trigger an alert:

| Signal | Threshold | Alert Priority |
|--------|-----------|---------------|
| `HMAC_FAILURE` events | >5 in 10 minutes | Priority 5 (Critical) |
| `APPROVAL_RBAC_BLOCK` events | >3 in 30 minutes | Priority 4 (High) |
| `CODE_HASH_MISMATCH` | Any single event | Priority 5 (Critical) |
| `PROMPT_INJECTION_SUSPECTED` | Any single event | Priority 4 (High) |
| Monthly GCP spend | >$5.00 | Priority 4 (High) |
| Agent heartbeat absent | >15 minutes | Priority 3 (Standard) |

### 8.2 Configuration Drift Detection

Agents that perform operations on infrastructure (BigQuery schema updates, Vertex AI corpus operations) MUST log the before-state and after-state of any structural change. Configuration drift is defined as any structural change not initiated by a human-approved Pub/Sub message or Approval Gate decision.

Model aliases (`settings.models.*`) MUST NOT be changed without a Priority-2 Approval Gate proposal. The nightly observability loop compares deployed model aliases against the last approved baseline and publishes a drift alert if they differ.

### 8.3 Access Pattern Anomalies

The nightly observability loop SHOULD compute per-agent baseline metrics:

- Mean tool calls per hour
- Mean LLM tokens per task
- Mean Secret Manager access rate

Deviations >3 standard deviations from the 7-day rolling mean trigger a Priority-3 alert. This provides early warning of both malfunction and compromise.

### 8.4 Incident Response

When a Priority-5 event fires:

1. Owner receives a Chat card notification immediately
2. The triggering agent's task is hard-stopped (no retry)
3. Owner investigates via Cloud Logging with the `task_id` from the alert
4. If compromise is confirmed: revoke the agent's SA key via `gcloud`, redeploy from the last known-good image, rotate any secrets the agent had access to
5. A post-incident summary is filed as an Approval Gate entry with `Status = Informational` within 24 hours

---

## 9. DevSecOps Integration

Security is integrated at every phase of the agent development lifecycle:

| Phase | Security controls |
|-------|------------------|
| **Plan** | Acceptable agency scope defined in agent identity file (`Docs/agents/<name>.md`) before coding begins |
| **Code** | Rule 16 (Ruff/type hints), Rule 4 (no dangerous patterns), Rule 8 (test file required for every tool) |
| **Test** | `TestS1`–`TestS4` in `tests/test_agents.py` enforce all four safety gates; `TestU3` blocks hardcoded model version strings; no live API calls in test suite |
| **Review** | `validate_code_safety()` runs before any code reaches a human reviewer; `detect-secrets` pre-commit hook blocks credential commits |
| **Deploy** | SHA-256 hash verified at deploy time; all 7 services confirmed `--no-allow-unauthenticated`; Workload Identity Federation — no SA key files |
| **Monitor** | `_log_cloud()` for all side effects; nightly observability loop; GAOS-Doctor health check (`scripts/gaos_doctor.py`); alert thresholds per §8.1 |
| **Govern** | Approval Gate for all code changes, model upgrades, and import allowlist expansions; 24-month BigQuery audit trail |

---

## 10. Compliance Posture

| Control | Implementation | Spec reference |
|---------|---------------|----------------|
| Access control | Per-agent SA, no `allUsers`, RBAC via Approval Gate tiers | `GAOS-Deploy-Spec.md §3.2` |
| Audit logging | `_log_cloud()` for all side effects, BigQuery 24-month retention | `GAOS-Manager-Spec.md §15.1` |
| Data encryption at rest | GCP default (AES-256) for Sheets, BigQuery, Secret Manager, Drive | `GAOS-Privacy-Spec.md §2.1` |
| Data encryption in transit | TLS 1.2+ enforced by Cloud Run; HTTPS only | GCP default |
| Secret management | Secret Manager only; no plaintext in code or config | `AI-Autocoding-Rules.md §3` |
| Code integrity | SHA-256 pinning at proposal submission; verified at deploy | `AI-Autocoding-Rules.md §5` |
| Least privilege | IAM bindings scoped to minimum required permissions per agent | `infra/main.tf` |
| Incident response | Priority-5 hard stop → owner alert → SA revocation path | §8.4 above |
| Human oversight | Approval Gate mandatory for all code deployment | `AI-Autocoding-Rules.md §4` |

---

## 11. Policy Maintenance

This document is a living policy. It MUST be updated when:

- A new agent is added (update §1.1 identity table, §7.2 scope definition)
- A new tool is added (update §2.2 provenance table if the tool accesses external content)
- An import is added to `_ALLOWED_IMPORTS` (update §3, Gate 2)
- A new alert threshold is established (update §8.1)
- An incident occurs (update §8.4 with findings; add gotcha to `gotchas.md` if non-obvious)

Policy changes require a Priority-2 Approval Gate proposal and a matching update to this document in the same commit.

---

_Last reviewed: 2026-03-23_
_Owner: EgoNoBueno_
_Next scheduled review: 2026-06-23_
