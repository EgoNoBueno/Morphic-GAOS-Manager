# GAOS-Doctor

Diagnostic tool for Morphic-G AOS. Verifies system health across all infrastructure layers.

## Quick Start

```powershell
# From repo root, .venv activated, ADC configured (gcloud auth application-default login)
.venv\Scripts\python.exe scripts/gaos_doctor.py
```

Exit code 0 = all checks passed. Exit code 1 = one or more checks failed.

## What It Checks (33 checks total)

| # | Check Group | Checks |
|---|-------------|--------|
| 1 | **Sheet Connectivity** | Project Registry tab readable via gspread |
| 2 | **Pub/Sub** | All 8 topics exist; spot-check 3 key subscriptions; total sub count |
| 3 | **Secret Manager** | All 6 required secrets accessible: `GEMINI_API_KEY`, `OLLAMA_HOST`, `WEBHOOK_HMAC_SECRET`, `WEBHOOK_URL`, `GOOGLE_SEARCH_API_KEY`, `GOOGLE_SEARCH_CX` |
| 4 | **Cloud Run /health** | All 7 services return HTTP 200 (authenticated via `nexus-prime-sa` impersonation) |
| 5 | **Vertex AI Corpora** | All 7 RAG corpora exist and respond: accounting, admin, global, marketing, operations, research, sales |

## Prerequisites

- `.venv` activated
- ADC configured with Drive + Sheets scopes (see `GAOS-Deploy-Spec.md §0.4`)
- User has `roles/iam.serviceAccountTokenCreator` or `roles/iam.serviceAccountUser` on `nexus-prime-sa` for SA impersonation (health endpoint check)

## Known Limitations

- **Cloud Run /health** uses `gcloud auth print-identity-token --impersonate-service-account=nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com`. User ADC (OAuth refresh token) cannot produce OIDC ID tokens directly — SA impersonation is required for local dev. This is expected behavior; the CI/CD pipeline and production Pub/Sub push use proper SA identities.

- **Pub/Sub subscription count** expects 22 (per the Deploy Spec); 23 is also valid if an extra test subscription was created. The check passes if count > 0 and key subscriptions are present.

- **Billing cost check** (`≤ $5/month`) is not automated — requires 7+ days of production traffic data and manual review in [GCP Billing console](https://console.cloud.google.com/billing). Budget alert is set at $10/month (`SL10 Cloud Dev Budget`).

## Last Run

**2026-03-21 — 33/33 passed.** Baseline clean on Phase 4 bootstrap day.

## Planned Enhancements (Phase 5)

- Add `--repair` flag: auto-create missing Pub/Sub topics, re-seed missing secrets prompt
- Add `--deep` flag: test a full Pub/Sub round-trip (publish + verify delivery)
- Export results as structured JSON for CI/CD assertion
- Integrate into GitHub Actions as a post-deploy health gate

