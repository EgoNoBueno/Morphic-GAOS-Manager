# Build Notes

Implementation notes written for the authors of external resources that directly influenced GAOS architecture decisions. Each entry describes what we learned, what we built, and what we skipped.

---

## Chapter 8 — Autonomous Systems Design
*OpenClaw Paradigm Book — chunhualiao/openclaw-paradigm-book*
*Implemented: 2026-03-27*

Hi,

We reviewed Chapter 8 of the OpenClaw Paradigm Book as part of an architecture review for [Morphic-GAOS](https://github.com/EgoNoBueno/Morphic-GAOS-Manager), a multi-agent orchestration system built on Google Cloud (LangGraph, Cloud Run, Pub/Sub, Vertex AI, BigQuery). The chapter covers autonomous systems design including self-healing, goal-oriented behavior, multi-agent coordination, and safety layers. Four concrete ideas translated directly to shipped code.

**Our starting point**

GAOS is already deeply autonomous in the §8.2 Conditional Autonomy sense: seven domain-specialist agents operate independently, escalate to a human (via the Approval Gate and Google Chat) only when they hit an unresolvable state, and run entirely on GCP without operator intervention during normal operation. What we lacked was formal lifecycle state tracking, protection against accumulated state corruption in long-running flows, circuit protection on failing external dependencies, and a generic output quality check distinct from the code-proposal coherence gate we already had.

---

**What we shipped**

*Phoenix checkpoint and recovery pattern (§8.11)*

The Phoenix Pattern addresses a real failure mode in long-running orchestration flows: an unhandled exception or partial tool-call failure can leave the agent state dict in an internally inconsistent state. Without a recovery mechanism, the LangGraph state machine re-enters the event loop on the next message with stale or corrupt values, producing unpredictable behavior.

We implemented `tools/phoenix.py` with four public functions:
- `validate_state()` — checks that required fields are non-empty and the serialized state is under 512 KB
- `save_checkpoint()` — serializes the state to JSON, computes a SHA-256 hash, and streams the row to `aos_logs.agent_checkpoints` in BigQuery (best-effort; BQ failure is non-fatal to the calling agent)
- `load_checkpoint()` — queries the last 5 checkpoints for an agent, hash-verifies each, and returns the first valid one
- `phoenix_recover()` — the entry point: validates current state, and if corrupted, restores from the last known-good checkpoint; raises `CheckpointCorruptedError` if none exists

The hash-on-load check was a deliberate design decision: `aos_logs.agent_checkpoints` is writable by the agent service account and technically human-editable via the GCP console. A tampered checkpoint would deploy corrupt state into the next execution and be nearly impossible to debug. The SHA-256 pin closes that.

*Formal agent lifecycle state machine (§8.23)*

Agents already have informal lifecycle states (boot sequences, heartbeats, LangGraph node transitions) but no formally typed values or structured transition logging. We added `AgentState` (a `str` enum) to `agents/__init__.py` with nine canonical states:

```
INIT → PLANNING → EXECUTION → OBSERVATION → HEALING → SYNTHESIS → COMPLETED
                                          ↘ ESCALATION
                                                      ↘ IDLE (on complete or park)
```

`log_state_transition(agent_id, project_id, task_id, from_state, to_state, reason)` fires a structured `_log_cloud` entry with `log_type="state_transition"` and both state values in the `extra` payload, making the OODA loop fully auditable in Cloud Logging and filterable by `jsonPayload.from_state` / `jsonPayload.to_state` in Grafana. The Grafana dashboard can now expose the complete state trajectory of every task, not just the terminal outcome.

*Circuit breaker for external dependency calls (§8.3.3)*

Long-running agents that call external tools (BigQuery, Sheets, Pub/Sub, the Gemini API) have no protection today against hitting a dependency that's soft-failing — a dead endpoint with a 10-second timeout burns the entire Cloud Run request budget and eventually starves the event loop. We implemented `tools/circuit_breaker.py` as a thread-safe, in-memory circuit breaker:

- Three states: `CLOSED` (normal), `OPEN` (tripped), `HALF_OPEN` (probe after cooldown)
- `check(agent_id, resource_key)` raises `CircuitOpenError` if the circuit is open and the cooldown hasn't elapsed; transitions to `HALF_OPEN` when cooldown expires
- `record_failure()` / `record_success()` update the counter and state
- Per-instance in-memory state is intentional: Cloud Run agents are single-process, single-thread from the perspective of this loop. We don't need cross-instance coordination (which would require a distributed cache) — we need to prevent *this* instance from hammering a dead resource.
- Default threshold: 3 consecutive failures. Default cooldown: 300 seconds.

The HALF_OPEN state is the only non-trivial implementation choice: after cooldown, we allow exactly one probe call before deciding whether to reopen or close. This matches the standard circuit breaker pattern and avoids the failure mode where cooldown expiry immediately re-floods a still-recovering service.

*Output coherence check (§8.3.1 Semantic Validation)*

We already had `_validate_proposal_coherence()` in `agents/__init__.py` as a pre-approval gate for code proposals. What was missing was a general-purpose quality check that any agent can call to verify that a generated output actually addresses the task goal — not just that it's syntactically valid.

`validate_output_coherence(goal, output, agent_id, project_id)` uses `LOCAL_MODEL` (Ollama, offline) to score the output against the goal on relevance, completeness, and coherence, returning `{"passed": bool, "confidence": float, "reason": str}`. If Ollama is unavailable, it passes with `confidence=0.5` and logs a warning — the caller's main path is never blocked.

The offline constraint was a deliberate choice consistent with our existing `_validate_proposal_coherence` design: the output being evaluated is operational content. Sending it to a cloud LLM (Gemini) would incur both cost and data egress risk. Ollama handles it locally.

---

**What we skipped and why**

- *Tree-of-Thoughts planning (§8.12.1):* Latency and token cost are prohibitive for GAOS's reactive event loop. The correct fix for multi-path planning uncertainty is better prompt design on single-pass calls, not N-branch generation + critic selection on every decision.
- *Cross-model verification (§8.20.1):* For critical approvals running three independent models (Claude + GPT + Gemini) with consensus before execution is a sound pattern. But the GAOS Approval Gate already requires a human reviewer — adding model consensus on top of human approval would be defending a gate that already has the strongest possible guard. Revisit in Phase 5 if approval latency tolerances allow it.
- *Stigmergic task queues (§8.16.3):* Replacing or augmenting Pub/Sub with a `tasks/pending/` directory poll is an interesting resilience pattern but implies a shared filesystem that doesn't exist in our Cloud Run architecture. A GCS-backed equivalent is possible but adds a new dependency for a resilience benefit we'd only feel at agent scale far beyond current projections.
- *Ghost/stealth patterns (§8.24):* Not in scope. All GAOS tool calls use authenticated GCP APIs. The privacy concern addressed by PII-redaction at the edge (`8.24.3`) is worth tracking but requires evidence of PII-exposure risk in our current operational content scope before implementing.
- *Red-Team Guardian (§8.16.2):* Sound for Phase 4 — a scheduled agent that attempts to bypass `validate_code_safety()` via prompt injection would stress-test our static analysis gates against real adversarial inputs rather than crafted fixtures. Tagged for Phase 4.

---

**Results**

558 tests passing (up from 511). New test files:
- `tests/test_circuit_breaker.py` — 22 tests covering all states, transitions, cooldown, and isolation
- `tests/test_phoenix.py` — 25 tests covering validation, save/load round-trip, hash verification, corrupt-state recovery, and BigQuery failure resilience

---

## Chapter 7 — Cron and Scheduled Automation Patterns
*OpenClaw Paradigm Book — chunhualiao/openclaw-paradigm-book*
*Implemented: 2026-03-27*

Hi,

We've been working through the OpenClaw Paradigm Book as part of an ongoing architecture review for [Morphic-GAOS](https://github.com/EgoNoBueno/Morphic-GAOS-Manager), a multi-agent orchestration system built on Google Cloud (LangGraph, Cloud Run, Pub/Sub, Vertex AI, BigQuery). Chapter 7's section on scheduled automation patterns surfaced two concrete gaps in our implementation that we shipped fixes for.

**Our starting point**

GAOS runs two Cloud Scheduler jobs against a single Cloud Run service (Nexus-Prime): a nightly 2 AM archive sweep that moves aged Google Sheets rows to BigQuery cold storage, and a 6 AM morning briefing that assembles a daily status card via Google Chat. Both are standalone `async def` functions invoked directly by HTTP POST from Cloud Scheduler — they run outside the LangGraph state machine for simplicity.

**What we shipped**

*Idempotency guard for the archive job (§7.3 / §7.9.2)*

Chapter 7's framing of overlap prevention (§7.22.3) and the incremental processing checkpoint pattern (§7.9.2) prompted us to audit `handle_archive()` for retry safety. Cloud Scheduler retries on 5xx or timeout — if both the original and the retry run reach the Sheets read before either completes deletion, BigQuery gets duplicate rows in `aos_logs.task_outcomes`. We had no guard against this.

The fix was low-ceremony: at the top of `handle_archive()`, we read the Logs tab for any row where `agent_id == "nexus-prime"` AND `level == "ARCHIVE"` AND timestamp is within the last 4 hours. If found, the function logs a WARNING and returns `{"skipped": True}` immediately. The idempotency key was already being written by step 4 of the function — we just weren't checking it at entry. Two new tests cover the skip path and the normal-flow path. Suite is at 511 tests.

*Scheduled job version control (§7.4 / IaC)*

Chapter 7's recommendation to manage scheduler configurations in IaC matched a real gap: our `infra/main.tf` had zero `google_cloud_scheduler_job` resources. The two jobs existed only in the GCP console, meaning a new project deployment would have no scheduled jobs at all.

Rather than adding Terraform resources (which would require `terraform apply` against a live state file), we added `scripts/provision_schedulers.py`. The script follows the pattern of our other one-time bootstrap scripts (`setup_secrets.py`, `provision_missing_folders.py`) — it's idempotent, reads project config from `settings.yaml`, resolves the nexus-prime Cloud Run URL dynamically via the Cloud Run Admin API, and creates or patches the two Cloud Scheduler jobs using `googleapiclient`. It also ensures `nexus-prime-sa` holds `roles/run.invoker` on the Cloud Run service. Run it once per project after `terraform apply`.

**What we skipped**

The chapter's adaptive scheduling section (§7.7) and circuit breaker patterns (§7.5.2) don't map to our current setup — Cloud Scheduler is the scheduler, and making schedules adaptive would require an external controller layer that's out of scope. The `gaos_doctor.py` diagnostic script has no autonomous alerting capability, but it's a manual CLI tool by design and adding a `/health` endpoint that Cloud Scheduler pings would be a larger architectural change for a later phase.

---

## Chapter 6 — File Coordination and Memory Patterns
*OpenClaw Paradigm Book — chunhualiao/openclaw-paradigm-book*
*Implemented: 2026-03-27*

Hi,

We've been reading through the OpenClaw Paradigm Book as part of an ongoing architecture review for [Morphic-GAOS](https://github.com/EgoNoBueno/Morphic-GAOS-Manager), a multi-agent orchestration system built on Google Cloud (LangGraph, Cloud Run, Pub/Sub, Vertex AI, BigQuery). Chapter 6 was directly actionable for us, and I wanted to share what we took from it and what we actually shipped.

**Our starting point**

GAOS already had a layered memory architecture that maps reasonably well to the chapter's three-tier mental model — a Vertex AI Memory Bank for curated semantic knowledge (your "MEMORY.md" analog), BigQuery for episodic task history, and a `Pending_Knowledge` Google Sheet tab as the staging buffer for candidate learnings. Agents boot stateless on Cloud Run and load their domain memory once at startup.

Two gaps in our implementation stood out after reading Chapter 6.

---

**Gap 1: Recency-blind truncation (§6.7 — Contextual Loading / Token Budget)**

Your `ContextBudget` class introduced something we hadn't thought carefully about: *which* content gets dropped when you're over budget matters as much as *how much* gets dropped. We had a token budget guard in our boot-time memory loader, but it truncated entries in whatever order the Vertex AI API returned them — no recency awareness whatsoever. An agent could boot with its newest, most relevant knowledge silently evicted and its oldest, potentially stale entries intact.

**What we implemented:** A single pre-sort step before the budget guard runs. All memory entries are now sorted newest-first by `created_at` before truncation. When budget pressure hits, the oldest entries are dropped first. The implementation handles API versions that don't expose `created_at` with a graceful no-op fallback.

---

**Gap 2: No automatic path from experience to curated memory (§6.8–6.9 — Progressive Summarization + Heartbeat-Driven Maintenance)**

This was the more substantive finding. GAOS agents accumulate task history in BigQuery continuously, but the only way for that experience to reach the curated memory layer was through human-approved proposals — and those only happened when an agent explicitly surfaced a learning. There was no automated distillation loop.

Your progressive summarization hierarchy (raw observations → daily summaries → weekly insights → curated long-term memory) gave us the right framing. We adapted it to fit our architecture's constraints: we have a nightly archive job (`handle_archive()`) that already runs at 2AM and reads recent log data. That became our distillation trigger.

**What we implemented:** A new "progressive distillation" step in the nightly archive job. After the archive sweep completes, the job reads back the last 24 hours of log messages, groups them by agent, and for any agent that generated ≥ 5 messages (enough signal to be worth summarizing), it calls a local LLM to distill 2–3 actionable lessons. Those lessons are written to `Pending_Knowledge` — the staging layer — which means a human must approve them before they reach the curated memory layer. This was a deliberate choice: automatic distillation writes *proposals*, not *facts*. The confidence gate and approval gate remain intact.

The local LLM constraint was important to us: distillation runs on Ollama (offline, no data egress, no cost) because we're summarizing operational logs that may contain business-sensitive data.

---

**What we skipped and why**

- **File coordination / FileLock (§6.11):** Not applicable — we have no shared filesystem. Cloud-native APIs handle concurrent access.
- **Vector backends (§6.10):** Vertex AI Memory Bank already covers semantic retrieval; adding ChromaDB or Qdrant would create split-brain memory with no clear benefit.
- **PII redaction (§6.5):** No evidence of PII in our current memory content scope. Worth revisiting if that changes.
- **ContextBudget as a reusable class:** Our boot-time memory load is the only multi-source context assembly point today, so promoting the pattern to a full class would be premature. We took the underlying principle (sort before truncating) and applied it directly.

---

**Results**

509 tests passing. The distillation step ran cleanly in our test suite against mocked infrastructure — each agent's domain context (beacon→marketing, ledger→accounting, etc.) routes to the right memory domain, and LLM failures are non-blocking so a downed Ollama instance never blocks the archive sweep.

The chapter's core insight — that memory isn't just about what you store, it's about what survives under pressure and what gets regularly distilled into durable signal — translated directly into two concrete improvements. Thanks for writing it.

---

## Chapter 3 — Skill Architecture Patterns in Practice
*OpenClaw Paradigm Book — chunhualiao/openclaw-paradigm-book*
*Reviewed: 2026-03-27*

Hi,

Chapter 3 is a case-study chapter — it works through six concrete skill implementations (health-check, founder-coach, ai-proposal-generator, Discord, gog, and the claude-usage/early-compact pair) and uses them as vehicles to illustrate eight pattern categories: Tool-Based Error Recovery, File-Based Memory, Skill Blueprint, Guardrail-First Safety, Environment-First Configuration, Micro-Skill Architecture, Gateway-Mediated Multi-Agent, and a comparative table across all of them.

**Our starting point**

GAOS maps to most of these patterns structurally, though we arrived at them independently. Domain-locking (seven agents each with narrow, non-overlapping scope) is the Micro-Skill Architecture applied at the agent level. The `validate_code_safety()` AST gate plus SHA-256 pin is Guardrail-First Safety applied to code execution proposals. Secret Manager plus `settings.yaml` model aliases instead of hardcoded values is Environment-First Configuration. Pub/Sub as the sole inter-agent channel, mediated by Nexus-Prime, is the Gateway-Mediated Multi-Agent Pattern. `gaos_doctor.py`'s BigQuery staleness query is a lightweight form of the health-check pattern.

**What was actually useful**

The chapter's most practically interesting concept was the **OK/WARN/FAIL tri-state diagnostic model** from the health-check skill. GAOS currently reports binary IDLE/RUNNING heartbeats without a nuanced degradation tier. An agent can be "running" while one of its cloud dependencies is soft-failing — the current status model doesn't surface that. However, after reviewing the existing implementation, we concluded this belongs at the observability layer (Grafana/Cloud Logging alerts, not heartbeat payload changes) once deployment stabilizes. Not a Phase 2 code change.

**The anti-patterns section (§3.10) was the most directly reinforcing part of the chapter.** Three anti-patterns were documented there:

- *Monolithic Skill:* We'd already solved this. Domain-locking enforces narrow scope at the agent level by architectural constraint, not by convention.
- *Hard-Coded Path:* GAOS is actively hardening this with `TestU3NoLiteralModelVersions` — a test that scans all seven orchestrator source files for literal model version strings and fails the build if any are found.
- *Silent Failure:* All GCP API wrappers return structured results with explicit status, output, and error categorization. Silent failure never propagates through our tool layer.

The `claude-usage` / `early-compact` cost-awareness micro-skills have no direct analog in GAOS — Cloud Run agents are stateless and load memory once at boot from Vertex AI. There is no per-session context window to compress. The CLI Bridging pattern (gog skill) is also inapplicable — all GAOS inputs arrive via Google Chat or Pub/Sub. No CLI translation layer exists or is needed.

**What we implemented**

Nothing new — chapter 3 functioned primarily as a pattern vocabulary validation. The main outcome was reinforced confidence that our existing architectural decisions align with the canonical pattern shapes identified here. A note was added to revisit the tri-state health model as a post-Phase 2 observability improvement.

**Results**

Test count unchanged. No implementation gaps identified that weren't already solved by existing architecture or deferred to a later phase.

---

## Chapter 4 — The Soul.md Pattern
*OpenClaw Paradigm Book — chunhualiao/openclaw-paradigm-book*
*Reviewed: 2026-03-27*

Hi,

Chapter 4 introduces Soul.md as a "constitutional document" for AI agents: a human-readable Markdown file that defines an agent's Name, Core Truths, Style, Boundaries, and Continuity strategy. The core premise is that LLM-based agents have emergent behavioral characteristics that deserve intentional, declarative shaping rather than being left to default model behavior — and that shaping should happen through a persistent, version-controlled file that is injected into the system prompt at runtime.

**Our starting point**

GAOS has a parallel construct: `Docs/agents/<name>.md` identity files and `Docs/about-me.md`. These describe each agent's role, domain scope, constraints, and behavioral principles. They're version-controlled and reviewed in the same commits as code changes. However, there is a meaningful architectural difference: our identity files are human-facing documentation. In OpenClaw's Soul.md pattern, the file content is loaded into the agent's runtime system prompt — the model *reads* the soul on every session start and is behaviorally governed by its contents.

In GAOS, agent behavior is governed structurally — LangGraph state machine, code-level domain-locking, `validate_code_safety()` allowlist, Pub/Sub routing rules — rather than declaratively via an injected persona prompt. The model receives task-specific prompts but not a persistent "constitutional" prompt that re-establishes identity on every call.

**The genuine gap**

The **Boundaries** concept at the identity level is the most interesting delta. OpenClaw encodes constraints like "Ask before acting externally" in Soul.md, and those map directly to tool permission policies. In GAOS, we achieve the same outcome through code: the `validate_code_safety()` AST gate enforces execution boundaries, the Approval Gate enforces code proposal boundaries, and domain-locking enforces inter-agent communication boundaries. Our "soul" is expressed as invariants in code rather than declarations in Markdown.

The **Continuity** section of Soul.md is the part closest to something we actively implement. It declares how an agent should manage its memory files across sessions ("Read memory/YYYY-MM-DD.md at session start. Update it before closing."). Our equivalent is the boot-time `load_domain_memory()` call and the IDLE heartbeat write — agents load domain context at start and contribute back via the nightly archive flow. The mechanism exists; it's just not declared in a human-readable per-agent identity file.

**What we implemented**

Nothing new from this chapter — deliberately. Adding a runtime-injected Soul.md prompt per agent would require wiring the identity file into every `_call_model()` invocation across all seven orchestrators. That's a broad surface area change with unclear incremental value: our code-level constraints are more enforceable than prompt-level constraints. An LLM can "forget" a system prompt when context pressure is high; an AST gate cannot be bypassed by persuasive phrasing.

The one pattern worth carrying forward is the **layered Soul.md concept** from §4.6.2 — a shared corporate-soul with per-agent overlays. That maps cleanly to a future `global identity + domain identity` structure we could formalize without changing runtime behavior. Tagged for Phase 4.

**What we skipped and why**

- *Runtime Soul injection:* Structural enforcement is more reliable than declarative enforcement for a system that executes real GCP side effects.
- *A/B testing Soul.md variations:* We don't have the session volume or user-facing feedback loop to make this meaningful at this stage.
- *Soul Generator / Templates:* Seven agents with stable, well-defined identities. Template generation is premature.

**Results**

Test count unchanged. The chapter gave us useful vocabulary for describing our existing identity architecture and a clear articulation of why we chose structural over declarative enforcement. The identity files we have are the right artifact; the question of whether they should be runtime-injected is a Phase 4 decision.

---

## Chapter 5 — Multi-Agent Orchestration Patterns
*OpenClaw Paradigm Book — chunhualiao/openclaw-paradigm-book*
*Implemented: 2026-03-27*

Hi,

Chapter 5 covers the orchestration design space exhaustively — gateway-mediated (OpenClaw's primary pattern), peer-to-peer, hierarchical, market-based, and swarm approaches — then examines state management, session lifecycle, communication protocols, failure handling, security, and performance scaling across all of them.

**Our starting point**

GAOS implements a gateway-mediated architecture that the chapter would place in the "moderately coupled" tier. Nexus-Prime is the gateway: all inter-agent coordination runs through it via Pub/Sub, Google Sheets, and BigQuery. Tier 2 agents (Beacon, Ledger, Pursuit, Foreman, Steward, Scout) are domain specialists that never communicate with each other directly. The `A2AMessage` Pub/Sub envelope is our standardized message format. BigQuery tables are the shared state store for task history and observability.

**What we implemented**

One idea from §5.10.1 — Quality Assurance Through Multi-Agent Review — was directly actionable. The chapter described review cycles where draft content passes through agents checking factual accuracy, structural coherence, and alignment with project goals before finalization. We adapted this to our code proposal workflow.

The GAOS Approval Gate already enforces a human-in-the-loop check on all proposed code. What was missing was a pre-approval automated quality pass — a lightweight coherence check to catch proposals that are structurally valid but semantically incoherent before they reach the human reviewer.

**What we shipped:** `_validate_proposal_coherence()` in `agents/__init__.py`. It runs two checks against every `ApprovalProposal` before it enters the approval queue:

1. *Structural check:* The `issue` field must be ≥ 20 characters (a meaningful problem statement). If code is present, `stopping_constraint` must be non-empty — the agent must declare why the proposed code is safe to deploy.
2. *Semantic check:* An offline Ollama call asks whether the code and stated issue are coherent. A `QUALITY WARNING` is appended to `stopping_constraint` on failure — prominently visible to the human reviewer — but the proposal is not rejected automatically. Human judgment remains final.

The function is wired into `propose_gate` in `nexus_prime/orchestrator.py` and covered by seven tests (QP1-QP7) in `tests/test_agents.py`.

**What we skipped and why**

- *Performance reputation tracking:* The market-based coordination section suggested routing tasks to highest-performing agents based on historical scores. In GAOS, routing is by domain, not by reputation. Ledger always handles accounting tasks because that's its mandated scope, not because it's the best performer on some leaderboard. Reputation scoring would be a false signal.
- *Heartbeat watchdog:* Already implemented in `gaos_doctor.py`. The BigQuery staleness query catches agents that haven't written a heartbeat within the expected window.
- *Formal cross-agent context propagation:* GAOS agents maintain isolated context by design. Each agent loads its own domain memory at boot; there is no cross-agent context sharing. Adding a formal "context packet" to the `A2AMessage` envelope would increase payload size unpredictably against Pub/Sub's 10MB hard limit, and violate the isolation that domain-locking provides.
- *Capability registry:* Seven fixed agents with stable, hardcoded capability sets. Dynamic service discovery is over-engineering at this scale.

**Results**

503 tests passing post-implementation. The `_validate_proposal_coherence()` function added a meaningful quality gate to the proposal workflow with zero risk to the human approval layer, which remains the final authority on all proposed changes.
