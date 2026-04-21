# GAOS To-Do List

Working task list. Items are ordered by dependency — do not reorder.
See `Docs/Automation-Wish-List.md` for the full Wish List and item details.

---

## Phase 0 — Unblock (this week)

- [ ] **Close Phase 4** — Check GCP Billing for the 7-day cost window and check off `GAOS-Deploy-Spec.md §4e`. The only remaining Phase 4 exit-criteria box.
- [ ] **n8n pilot (BI5)** — Create free n8n Cloud account. Build Option E: GA4 → KPI Sheet → daily digest email. Run for 14 days. This validates n8n before it is used for any channel automation. See `GAOS-n8n-Integration-Spec.md §5 Option E`.
- [ ] **Add Google Search secrets** — Create `GOOGLE_SEARCH_API_KEY` and `GOOGLE_SEARCH_CX` in Secret Manager (`morphic-gaos-prod`). Required before any Scout `RESEARCH_MANDATE` (MC1+MC2) can run. Get the Custom Search Engine ID from [cse.google.com](https://cse.google.com) and the API key from Google Cloud Console → Credentials.

---

## Phase 0.5 — GAOS Housekeeping (before MC8)

Code is complete for all items in this phase. Remaining work is GCP provisioning
and the Doctor/observability improvements below.

**Already implemented (code in repo, need GCP run):**
- `replace_rows()` in `tools/bigquery.py` ✅
- `handle_sheets_sync()` + `POST /sheets-sync` ✅
- Grafana panels 10–14 in `ceo-overview.json` ✅
- All 5 scheduler jobs defined in `provision_schedulers.py` ✅
- `tests/test_sheets_sync.py` — 9 tests all passing ✅

**GCP provisioning steps (run once, requires ADC):**
- [x] **Run `scripts/create_staging_tables.py`** — Creates 6 tables in `morphic-gaos-prod.aos_logs`: `staging_approvals`, `staging_logs`, `staging_errors`, `staging_pending_knowledge`, `api_call_log`, `circuit_breaker_events`. Idempotent. ✅ 2026-04-20 — all 6 tables ready.
- [x] **Run `scripts/provision_schedulers.py`** — Provisions all 5 jobs: `gaos-archive`, `gaos-daily-sync`, `gaos-sheets-sync`, `gaos-gmail-renew-watch`, `gaos-daily-digest`. Idempotent. Resolves Gmail watch auto-renewal (HIGHEST PRIORITY). ✅ 2026-04-20 — all 5 PATCHED to current nexus-prime URL.

**Still needs code:**
- [x] **GAOS-Doctor daily cron + email report** — Run as a **Cloud Run Job** (not `POST /doctor` on Nexus-Prime) so the health check works even when all 7 orchestrators are down. Cloud Scheduler triggers the Job at `0 7 * * *` (7:00 AM PT). Steps: (1) add a `__main__` entry point to `scripts/gaos_doctor.py` that runs all 8 checks, collects results, and exits non-zero on any FAIL; (2) add `send_doctor_report()` that formats the pass/warn/fail summary as a plain-text email and sends via `tools/gmail.py` `send_email()`; (3) add a `gaos-doctor` Cloud Run Job definition (`Dockerfile.doctor` or reuse the main image with `--command`); (4) provision a `gaos-doctor-daily` Cloud Scheduler job targeting the Cloud Run Job (not a Cloud Run service URL); (5) add tests to `tests/test_gaos_doctor.py`.
- [x] **Doctor Check 9 + 10: open circuit breakers and scheduler job inventory** — (9) Query `aos_logs.circuit_breaker_events` for any resource whose last recorded state is OPEN; WARN per open circuit. (Grafana shows this visually but Doctor has no check — so the daily email is blind to it.) (10) Verify all expected Cloud Scheduler jobs exist and are enabled via the Scheduler API; FAIL if any are missing or paused. Add both to `scripts/gaos_doctor.py` and update the check count in `Docs/GAOS-Doctor.md`.
- [x] **Pub/Sub push endpoint staleness check** — Rule 27.3. Implemented in `74d59fb`. `_check_pubsub_endpoint_staleness()` in `scripts/observability_loop.py`: resolves live nexus-prime URL via Cloud Run Admin API, compares against all 8 push subscription `pushEndpoint` values, logs WARNING per mismatch. Runs every cycle (continuous) and on `--once`.

---

## Phase 1 — Research

- [x] **Write `Docs/GAOS-Marketing-Channel-Spec.md`** — ✅ 2026-04-20 — Spec created covering MC1–MC8 phases, Scout mandate payloads, HUMAN DECISION gate, platform API matrix, and Approval Gate designs.
- [ ] **MC1 — Scout channel audience mandate** — Send `RESEARCH_MANDATE` to Scout: which platforms does the SL10 B2B maintenance audience (facility managers, building operators, service teams) actually use? Output: ranked platform list with audience size, organic reach potential, content format breakdown per platform.
- [ ] **MC2 + MC3 — Competitor audit + API inventory (parallel)**
  - MC2: Scout crawls top 10–15 competitors on the MC1-recommended platforms. Output: competitive matrix — posting frequency, dominant formats, engagement rates, content gaps, 3–5 underserved angles SL10 can own.
  - MC3: Manual + Scout research on which platforms expose usable publishing APIs. YouTube: YES. LinkedIn Pages: limited. Meta Graph: yes, but app review required. TikTok: limited. Output: platform-capability matrix with OAuth scopes and approval timelines.
- [ ] **HUMAN DECISION — Approve channel strategy** — Review MC1 + MC2 output. Select 1–2 platforms to launch first. Everything in Phase 2 is gated on this decision.

---

## Phase 2 — Setup

- [ ] **MC4 — Beacon generates channel profile content** — Beacon produces bio, about text, keyword tags, and banner brief for each approved platform in SL10 brand voice. Full batch submitted to the Approval Gate as a single proposal. Nothing published until Approved.
- [ ] **n8n — Set up platform OAuth credentials** — For each API-capable platform: create OAuth app, obtain credentials, store in n8n credential vault. **Start LinkedIn and Meta app registrations in Week 1** — developer app review can take 1–3 weeks and will block MC5 if deferred.
- [ ] **MC5 — Channel setup automation (n8n)** — Where APIs allow, automate channel/page creation and initial profile configuration using the MC4-approved content. Non-API platforms get a generated manual-setup checklist. Triggers BI5 Phase B node activation for that platform's metrics columns.

---

## Phase 3 — Content Pipeline

- [ ] **MC6 — Beacon generates 30-day seed content calendar** — Platform-specific content for 30 days per active channel (post copy, image prompt, video brief, hashtags, timing). Single batch Approval Gate proposal. Runs in parallel with MC5.
- [ ] **MC7 — Content publishing + scheduling workflow (n8n)** — n8n posts approved content on schedule via Buffer or direct platform API (per MC3 findings). Extends M3 social repurposing — long-form content auto-reformatted per platform.
- [ ] **MC8 — Extend BI5 with channel metrics (n8n)** — Activate the pre-scaffolded YouTube columns in BI5. Add LinkedIn impressions/engagement rate. Bring channel data into the Grafana CEO dashboard. **Prerequisite: BI5 must have run cleanly for 14 days first.**

---

## CEO Dashboard — Business KPI Section (High Priority)

Prerequisite decisions before the Revenue / Expenses / Leads / Conversions / Acquisition Cost / Retention / Net Profit panels can be built. See `Docs/GAOS-CEO-Dashboard.md` (Planned section).

- [ ] **Decide financial data source** — Determine where Revenue, Expenses, and Net Profit figures come from. Options: QuickBooks API, Stripe dashboard export, or a manually-maintained BQ table. Decision drives schema and ingest method.
- [ ] **Decide CRM / leads data source** — Determine where Leads, Conversions, Acquisition Cost, and Retention figures come from. Options: CRM API (HubSpot, Salesforce, etc.), Google Sheets feed, or a Pub/Sub event stream from GAOS agents.
- [ ] **Decide refresh cadence** — Real-time (event-driven BQ writes) or nightly batch (Cloud Scheduler job)? Drives panel design and whether a new tool wrapper is needed.

---

## Notes

| Dependency | Risk |
|-----------|------|
| LinkedIn/Meta API review | Apply in Week 1 — not Week 3. Up to 3-week approval window. |
| BI5 14-day validation | MC8 cannot start until BI5 is running cleanly. |
| `scaffold_agent()` (Track 2 Gap 1) | Only needed if MC6 content volume requires a dedicated Beacon sub-agent. Defer until first 30-day calendar is generated and volume is measurable. |

_Last updated: 2026-04-17 (added Phase 0.5 Grafana sync items and Google Search secrets from SCRATCH.md audit)_
