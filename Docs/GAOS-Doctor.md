# GAOS-Doctor

Diagnostic tool for Morphic-G AOS. Verifies system health across all infrastructure layers.

## Quick Start

```powershell
# From repo root, .venv activated, ADC configured (gcloud auth application-default login)
.venv\Scripts\python.exe scripts/gaos_doctor.py
```

Exit code 0 = all checks passed (or WARN-only). Exit code 1 = one or more FAIL checks.

## What It Checks (10 groups, ~50 individual checks)

| # | Check Group | Description |
|---|-------------|-------------|
| 1 | **Sheet Connectivity** | Project Registry tab readable via gspread |
| 2 | **Pub/Sub** | All 8 topics exist; spot-check 3 key subscriptions (`nexus-prime.sub.ledger`, `nexus-prime.sub.approvals`, `scout.sub.foreman`); total subscription count ≥ 22 |
| 3 | **Secret Manager** | All 6 required secrets accessible: `GEMINI_API_KEY`, `OLLAMA_HOST`, `WEBHOOK_HMAC_SECRET`, `WEBHOOK_URL`, `GOOGLE_SEARCH_API_KEY`, `GOOGLE_SEARCH_CX` |
| 4 | **Cloud Run /health** | All 7 services return HTTP 200 (authenticated via `nexus-prime-sa` impersonation) |
| 5 | **Vertex AI Corpora** | All 7 RAG corpora exist and respond: accounting, admin, global, marketing, operations, research, sales |
| 6 | **Agent Heartbeat Recency** | BQ `aos_logs.status_snapshots` — WARN if last heartbeat > 60 min, FAIL if > 240 min, for all 7 agents |
| 7 | **Monthly Cost (MTD)** | BQ `aos_logs.task_outcomes` — WARN if MTD cost > $5, FAIL if > $10 |
| 8 | **Error Themes (last 1h)** | Cloud Logging `gaos-agents` ERROR entries — bucketed by Rate Limiting / Auth Failures / Timeouts / Internal; WARN if 1–5 errors, FAIL if > 5 |
| 9 | **Circuit Breaker States** | BQ `aos_logs.circuit_breaker_events` — WARN for each resource whose last recorded state is OPEN (tool calls to that resource are blocked until cooldown elapses). Gracefully skips if table doesn't yet exist. |
| 10 | **Cloud Scheduler Job Inventory** | Verifies all 5 expected jobs exist and are not paused: `gaos-archive`, `gaos-daily-sync`, `gaos-sheets-sync`, `gaos-gmail-renew-watch`, `gaos-daily-digest`. FAIL if any are missing; WARN if any are PAUSED. |

## Prerequisites

- `.venv` activated
- ADC configured with Drive + Sheets scopes (see `GAOS-Deploy-Spec.md §0.4`)
- User has `roles/iam.serviceAccountTokenCreator` or `roles/iam.serviceAccountUser` on `nexus-prime-sa` for SA impersonation (Check 4)

## Known Limitations

- **Cloud Run /health** uses `gcloud auth print-identity-token --impersonate-service-account=nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com`. User ADC cannot produce OIDC ID tokens directly — SA impersonation is required for local dev. The CI/CD pipeline and production Pub/Sub push use proper SA identities.

- **Pub/Sub subscription count** expects ≥ 22. Current `morphic-gaos-prod` deployment has 25 subscriptions (confirmed 2026-03-21). The check passes if count ≥ 22 and all 3 spot-check subscriptions are present.

- **Heartbeat recency (Check 6)** is expected to WARN on an idle system. Cloud Run scales to zero after ~15 min of inactivity; agents only heartbeat on startup and during task processing. WARN (> 60 min) is normal when no traffic has arrived recently. Only FAIL (> 240 min) indicates a stuck or dead agent.

- **Check 7 (Monthly Cost)** reads from `aos_logs.task_outcomes` which tracks LLM call costs only — not Cloud Run compute, Pub/Sub, or BigQuery egress costs. For full billing, check the [GCP Billing console](https://console.cloud.google.com/billing) directly. Budget alert is set at $10/month (`SL10 Cloud Dev Budget`).

## Last Run

**2026-03-21 — 33/33 passed** (5-group version). Checks 6–8 were added after that baseline run; the current check count is approximately 43. Re-run to get a current count.

## Planned Enhancements (future scope)

- Add `--repair` flag: auto-create missing Pub/Sub topics, re-seed missing secrets prompt
- Add `--deep` flag: test a full Pub/Sub round-trip (publish + verify delivery)
- Export results as structured JSON for CI/CD assertion
- Integrate into GitHub Actions as a post-deploy health gate

