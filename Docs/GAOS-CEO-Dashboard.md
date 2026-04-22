# GAOS CEO Dashboard

Reference for the Grafana CEO Overview dashboard — what it shows, where each metric comes from, and how often it updates.

> **Dashboard file:** `dashboard/grafana/dashboards/ceo-overview.json`
> **UID:** `gaos-ceo-overview`
> **Grafana auto-refresh:** 30 seconds
> **Default time range:** Last 7 days (configurable in the top-right Grafana picker)

---

## ⚠️ PLANNED — Business KPI Section (High Priority)

The following panels are planned and not yet implemented. They require a business data source (BQ table, external API, or manual feed — TBD) and a design decision on data ownership before they can be built.

| Panel | Metric | Priority |
|-------|--------|----------|
| **Revenue** | Total revenue (period TBD — MTD / weekly / trailing 30d) | 🔴 High |
| **Expenses** | Total operating expenses for the same period | 🔴 High |
| **Net Profit** | Revenue − Expenses (computed) | 🔴 High |
| **Leads** | New leads generated (period TBD) | 🔴 High |
| **Conversions** | Leads converted to customers (period TBD) | 🔴 High |
| **Acquisition Cost** | Total spend ÷ new customers (computed) | 🔴 High |
| **Retention** | % of customers active vs. churned (period TBD) | 🔴 High |

**Open decisions before implementation:**
- Data source for financial figures (QuickBooks / Stripe / manual BQ table?)
- Data source for leads and conversions (CRM / Sheets / Pub/Sub event stream?)
- Refresh cadence (real-time vs. nightly batch?)
- Which Grafana panel type (stat tile, time series, table?)

When these are built, update this document and `dashboard/grafana/dashboards/ceo-overview.json`, add the new BQ tables to the §5 quick-reference table, and register the data source in §3.

---

## 1. Layout Overview

The dashboard is organised top-to-bottom into five logical sections (business KPI section to be added — see above):

| Section | Rows (approx.) | What it answers |
|---------|---------------|-----------------|
| **Agent Status** | Top full-width table | Are all agents alive and what are they doing? |
| **Key Stats** | Four stat tiles | How busy / expensive / stale is the queue? |
| **Trends** | Two bar charts | How has cost and evolution activity moved over time? |
| **Staging / Live** | Tables + stat tiles | What is in the approval and knowledge queues right now? |
| **Infrastructure Health** | Stat tiles + tables + bar chart | Are dependencies healthy and are circuit breakers tripping? |

---

## 2. Panels — Detail

### 2.1 Agent Status (full-width table)

| Column | Source field | Notes |
|--------|-------------|-------|
| agent | `agent_id` | One row per agent; latest heartbeat only |
| status | `status` | Color-coded: IDLE=green, WORKING=yellow, PARKED/ESCALATED=orange, ERROR=red |
| current objective | `current_objective` | Free-text field written by the agent on every heartbeat |
| open proposals | `open_proposals` | Count of pending Approval Gate proposals for this agent |
| last error | `last_error` | Most recent error message, if any |
| timestamp | `timestamp` | When the row was written |
| mins since heartbeat | computed | Green <5 min · Yellow 5–15 min · **Red ≥15 min** |

**BQ table:** `morphic-gaos-prod.aos_logs.status_snapshots`
**Query strategy:** Window function — `ROW_NUMBER() OVER (PARTITION BY agent_id ORDER BY timestamp DESC)` keeps only the latest row per agent.

---

### 2.2 Key Stats Row (four stat tiles)

| Tile | Value | BQ table | Threshold |
|------|-------|----------|-----------|
| **Approval Queue — Oldest Age** | Minutes since the oldest pending approval was submitted | `aos_logs.staging_approvals` WHERE `status = 'pending'` | Green <60 min · Yellow 60–240 min · **Red ≥240 min** |
| **Tasks Completed (24h)** | Count of rows written to `task_outcomes` in the last 24 hours | `aos_logs.task_outcomes` | Blue (no alert threshold) |
| **Cost This Week (USD)** | `SUM(cost_usd)` since the start of the current ISO week (Monday) | `aos_logs.task_outcomes` | Green (no alert threshold) |

> **Cost data note:** `cost_usd` values in `task_outcomes` reflect real computed costs including token rates and thinking budget — see `config/settings.yaml` for active Gemini pricing.

> **Note:** "Approval Queue — Oldest Age" uses `PARSE_TIMESTAMP` on a string-encoded ISO-8601 timestamp column. The stat turns red at 4 hours — meaning a proposal has been waiting for human action longer than a full work block.

---

### 2.3 Trend Charts (two bar charts)

#### Weekly Cost (USD, Last 8 Weeks)

- Groups `task_outcomes.cost_usd` by ISO week label (`%Y-W%V`).
- Covers the 8 complete weeks preceding today.
- **BQ table:** `aos_logs.task_outcomes`

#### Evolution Tasks by Agent (Last 30 Days)

- Groups `evolution_tasks` by calendar day and `agent_id`, stacked.
- Shows which agents have been generating the most self-improvement proposals and when.
- **BQ table:** `aos_logs.evolution_tasks`

---

### 2.4 Staging / Live Section

These panels read from **staging tables** — BigQuery tables that are populated by a periodic sync from Google Sheets (≤5 minute delay). They reflect the current state of the human-review queues.

#### Live Approval Queue (≤5 min delay) — table

Full listing of the most recent 100 rows in `staging_approvals`, newest first.

| Column | Meaning |
|--------|---------|
| id | Proposal ID |
| agent_id | Which agent submitted the proposal |
| status | Pending / Approved / Rejected / Deployed / Needs Revision (color-coded) |
| issue | Description of the problem the agent is trying to solve |
| trigger_reason | What caused the evolution proposal |
| stopping_constraint | The guardrail that halted the current iteration |
| iterations_run | How many self-improvement cycles have run on this proposal |

**BQ table:** `morphic-gaos-prod.aos_logs.staging_approvals`

#### Pending Approvals (live) — stat tile

Count of rows in `staging_approvals` where `status` is `'pending'` or empty.

| Threshold | Value |
|-----------|-------|
| Green | 0–2 |
| Yellow | 3–9 |
| **Red** | ≥10 |

**BQ table:** `morphic-gaos-prod.aos_logs.staging_approvals`

#### Pending Knowledge (live) — stat tile

Count of rows in `staging_pending_knowledge` where `status` is `'pending'` or empty.

| Threshold | Value |
|-----------|-------|
| Green | 0–4 |
| Yellow | 5–19 |
| **Red** | ≥20 |

**BQ table:** `morphic-gaos-prod.aos_logs.staging_pending_knowledge`

#### Live Log Feed — last 50 (real-time) — table

Most recent 50 entries from the Cloud Logging sink table: `timestamp`, `agent_id`, `severity` (as `level`), `message`. Populated by every `_log_cloud()` call — reflects live agent activity with ~30-second Grafana poll lag.

**BQ table:** `morphic-gaos-prod.aos_logs.gaos_agents` (date-partitioned Cloud Logging export, auto-created by `gaos-logs-bq-sink`)

> **Note:** `staging_logs` (previously used here) was always empty because `_log_cloud()` never writes to Sheets. This panel now reads from the live BQ sink.

#### Live Error Feed — last 20 (real-time) — table

Most recent 20 entries with severity `ERROR`, `CRITICAL`, `ALERT`, or `EMERGENCY`: `timestamp`, `agent_id`, `severity` (as `level`), `message`.

**BQ table:** `morphic-gaos-prod.aos_logs.gaos_agents` filtered by `severity IN ('ERROR','CRITICAL','ALERT','EMERGENCY')`

> **Note:** `staging_errors` (previously used here) was always empty. The `agent_id` and `message` fields are extracted from the `jsonPayload` column in the BQ sink.

---

### 2.5 Infrastructure Health Section

#### Gmail Watch — Mins Until Expiry — stat tile

Gmail push watches expire after 7 days. This tile shows how many minutes remain before the next renewal is needed, computed from the last successful `setup_watch` call in `api_call_log`.

| Threshold | Meaning |
|-----------|---------|
| **Red** <720 min (12 h) | Renew immediately — push notifications will stop |
| Yellow 720–2880 min (12 h – 48 h) | Renewal overdue soon |
| Green ≥2880 min (48 h) | Safe |

**BQ table:** `morphic-gaos-prod.aos_logs.api_call_log` WHERE `api_name = 'gmail' AND operation = 'setup_watch' AND success = true`

#### Circuit Breakers — Open Count — stat tile

Number of external dependencies whose most recent circuit breaker state is `OPEN`. An open circuit means the agent has stopped sending calls to that dependency while it recovers.

| Threshold | Meaning |
|-----------|---------|
| Green | 0 open circuits |
| Yellow | 1–2 open |
| **Red** | ≥3 open |

**BQ table:** `morphic-gaos-prod.aos_logs.circuit_breaker_events`

#### API Health (24h / 7d / All-Time) — table

One row per `api_name`. Columns:

| Column | Meaning | Threshold |
|--------|---------|-----------|
| calls_24h | Total calls in last 24 hours | — |
| success% 24h | `COUNTIF(success) / COUNT(*)` over 24 h | Red <90% · Yellow 90–99% · Green ≥99% |
| calls_7d | Total calls in last 7 days | — |
| success% 7d | Same ratio over 7 days | Same thresholds |
| calls_all | All-time call count | — |
| avg latency (ms) | `AVG(latency_ms)` all-time | Green <1 s · Yellow 1–5 s · **Red** ≥5 s |

**BQ table:** `morphic-gaos-prod.aos_logs.api_call_log`

> **Schema note:** Many queries in the dashboard use `CAST(... AS FLOAT64)` or `CAST(... AS TIMESTAMP)` for columns written as strings or integers to ensure Grafana handles the types correctly. For example, some cost calculations in the `api_call_log` may require extracting values from metadata using `JSON_VALUE`.

#### API Calls — Success vs Failure (Last 24h) — bar chart

Stacked bar chart, one bar per `api_name`. Green = success count, Red = failure count. Sorted descending by total call volume.

**BQ table:** `morphic-gaos-prod.aos_logs.api_call_log`

#### Circuit Breaker Events (last 50) — table

Chronological log of the 50 most recent state transitions (`CLOSED → OPEN`, `OPEN → HALF_OPEN`, etc.) across all agents and resource keys. `new_state` is color-coded: CLOSED=green, HALF_OPEN=yellow, OPEN=red.

**BQ table:** `morphic-gaos-prod.aos_logs.circuit_breaker_events`

---

## 3. Data Sources

All panels use a single Grafana data source:

| Property | Value |
|----------|-------|
| Plugin | `grafana-bigquery-datasource` |
| UID | `bigquery-morphic-gaos` |
| GCP project | `morphic-gaos-prod` |
| BQ dataset | `aos_logs` |
| Query mode | Raw SQL (all panels) |
| Location | US |

---

## 4. Update Cadence

| Data path | How it gets there | Lag |
|-----------|------------------|-----|
| `status_snapshots`, `approval_history`, `task_outcomes`, `evolution_tasks`, `api_call_log`, `circuit_breaker_events` | Agents write directly to BigQuery via `tools/bigquery.py` | Near real-time (seconds) |
| `staging_approvals`, `staging_pending_knowledge` | Periodic sync from Google Sheets (Approval and Knowledge queues) | ≤5 minutes |
| `gaos_agents` | Cloud Logging BQ sink (`gaos-logs-bq-sink`) — exports every `_log_cloud()` call directly from Cloud Logging | ~seconds (no Sheets sync hop) |
| Grafana browser refresh | Dashboard `"refresh": "30s"` — Grafana re-runs all BQ queries every 30 seconds | 30 s polling cycle |

**Effective freshness for most panels:** 30–60 seconds.
**Effective freshness for staging panels (approvals/knowledge):** up to 5 minutes 30 seconds (Sheets sync lag + one Grafana cycle).
**Effective freshness for log panels (Live Log Feed, Live Error Feed):** 30–60 seconds — these read directly from the BQ sink, no Sheets hop.

---

## 5. BigQuery Table Quick Reference

| BQ Table | Panels that read it |
|----------|-------------------|
| `aos_logs.status_snapshots` | Agent Status |
| `aos_logs.approval_history` | Approval Queue — Oldest Age |
| `aos_logs.task_outcomes` | Tasks Completed (24h) · Cost This Week · Weekly Cost (8 Weeks) |
| `aos_logs.evolution_tasks` | Evolution Tasks by Agent (30 Days) |
| `aos_logs.staging_approvals` | Live Approval Queue · Pending Approvals (live) |
| `aos_logs.staging_pending_knowledge` | Pending Knowledge (live) |
| `aos_logs.gaos_agents` | Live Log Feed · Live Error Feed |
| ~~`aos_logs.staging_logs`~~ | *(superseded by `gaos_agents` — always empty, not read by any panel)* |
| ~~`aos_logs.staging_errors`~~ | *(superseded by `gaos_agents` — always empty, not read by any panel)* |
| `aos_logs.api_call_log` | Gmail Watch Expiry · API Health · API Calls Success vs Failure |
| `aos_logs.circuit_breaker_events` | Circuit Breakers Open Count · Circuit Breaker Events |

---

## 6. Accessing the Dashboard

The dashboard is provisioned automatically from `dashboard/grafana/dashboards/ceo-overview.json` via the Grafana provisioning config in `dashboard/grafana/provisioning/`. Any changes to the JSON file take effect on the next Grafana container restart.

To add or modify a panel, edit `ceo-overview.json` directly — do not use the Grafana UI save button, as it will not persist across container restarts.

---

_Last updated: 2026-04-20_
