"""
Create seed knowledge files in the Google Drive Knowledge/ folder.
Reads drive_folder_id from config/settings.yaml.
"""
from __future__ import annotations

import yaml
from pathlib import Path

import google.auth
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"

SCOPES = ["https://www.googleapis.com/auth/drive"]

SEED_FILES = {
    "policies/expense_approval_policy.md": """\
# Expense Approval Policy

## Purpose
Define spending thresholds that require human approval before Ledger or other agents commit funds.

## Thresholds
- **< $100** — Agent may process autonomously
- **$100–$999** — Requires Tier 3 approver (Manager)
- **$1,000–$4,999** — Requires Tier 2 approver (Director)
- **$5,000+** — Requires Tier 1 approver (Owner)

## Recurring Expenses
Pre-approved recurring expenses (subscription renewals, standing vendor POs) bypass the gate
if the amount is within 10% of the last approved amount and the vendor is on the approved list.

*Update this document as business rules change.*
""",
    "policies/vendor_payment_terms.md": """\
# Vendor Payment Terms

## Standard Terms
- **Net 30** — default for all new vendors
- **Net 15** — available for vendors offering early-pay discount ≥ 2%
- **Due on receipt** — utilities and SaaS subscriptions only

## Late Payment
Do not approve any payment past due date without escalating to Owner tier.

*Update this document as contracts change.*
""",
    "policies/data_retention_policy.md": """\
# Data Retention Policy

## Sheet Data
- `Logs` tab: keep 90 days; rows older than 90 days archived to BigQuery nightly
- `Agent_Approvals` tab: keep 1 year; archive to `approval_history` BigQuery table
- `Error Logs` tab: keep 30 days; archive to `task_outcomes` BigQuery table

## BigQuery
- `task_outcomes`: 30-day partition expiration
- `evolution_tasks`: 365-day partition expiration
- `approval_history`: 730-day partition expiration
- `observability_weekly`: indefinite (manually managed)

## Drive Documents
- Active policies/procedures/workflows: indefinite
- Archived versions: 2 years, then delete

*Do not delete data outside these rules without Owner approval.*
""",
    "policies/communications_policy.md": """\
# Communications Policy

## Outbound Communications
Agents may NOT send external communications (email, SMS, API calls to third-party platforms)
without explicit approval unless the communication is:
1. A status webhook to the configured WEBHOOK_URL (internal)
2. A read-only API call for data retrieval

## Approval Gate
Any outbound message to a customer, vendor, or partner must go through the
`Agent_Approvals` tab and receive human approval before sending.

*Update as integration partners are added.*
""",
    "policies/research_policy.md": """\
# Research Policy

## Allowed Methods
- Web search via approved tools only
- Internal Drive document review
- BigQuery historical data queries

## Citation Requirements
All research outputs must include:
- Source URL or document path
- Date accessed
- Confidence score (1–5)

## Prohibited
- Scraping competitor customer data
- Accessing non-public systems without explicit authorization

*Update as Scout's tool set expands.*
""",
    "procedures/invoice_matching.md": """\
# Invoice Matching Procedure

## Steps
1. Retrieve open invoices from `Accounting` tab (Status = "Pending Match")
2. Retrieve recent bank transactions from source
3. Match by amount (±$0.01) and date (±3 business days)
4. Mark matched invoices as "Matched" and record transaction reference
5. Flag unmatched invoices older than 30 days for Owner review

## Escalation
If match confidence < 90%, escalate via `Agent_Approvals` with both records attached.
""",
    "procedures/lead_scoring_criteria.md": """\
# Lead Scoring Criteria

## Score Components (0–100)
| Factor | Weight | Notes |
|--------|--------|-------|
| Company size (employees) | 20 | >50 = full points |
| Budget signal | 30 | Explicit budget stated |
| Timeline | 25 | <90 days to decision |
| Fit to ICP | 25 | Industry + product match |

## Thresholds
- **80–100**: Hot — immediate follow-up within 24h
- **60–79**: Warm — follow-up within 3 business days
- **< 60**: Nurture — monthly check-in

*Update weights quarterly based on close rate data.*
""",
    "procedures/inventory_reorder_trigger.md": """\
# Inventory Reorder Trigger

## Default Rules
- Reorder when on-hand quantity falls below **2× average weekly sales** for the SKU
- Default reorder quantity: **4× average weekly sales** (4-week supply)
- Lead time buffer: add supplier lead time (days) to reorder point calculation

## Overrides
SKU-specific overrides are stored in the `Shipping and Receiving` tab,
columns `reorder_point` and `reorder_qty`.

## Escalation
Orders > $1,000 go through the Approval Gate before the PO is sent.
""",
    "procedures/document_filing.md": """\
# Document Filing Procedure

## Drive Folder Structure
```
Knowledge/
├── workflows/    — Multi-step cross-agent processes
├── procedures/   — Step-by-step single-task instructions
├── policies/     — Rules agents must follow
└── archive/      — Superseded versions of updated documents
```

## Filing Rules
- New procedures → `procedures/`
- New policies → `policies/`
- New process flows → `workflows/`
- When updating an existing document: move old version to `archive/` first,
  then write the new version in the original location

## Naming Convention
`<topic>_<type>.md` — lowercase, underscores, no spaces
""",
    "procedures/competitive_intelligence.md": """\
# Competitive Intelligence Methodology

## Monitoring Cadence
- Weekly: pricing page changes for top 5 competitors
- Monthly: product feature comparison update
- Quarterly: full competitive landscape review

## Sources (approved)
- Public websites and press releases
- LinkedIn company updates (public)
- Industry reports stored in Drive

## Output Format
File findings in `Research Products` Sheet tab with:
- competitor name, date, finding summary, source URL, impact rating (1–5)

*Update competitor list in this document as market changes.*
""",
    "workflows/ap_reconciliation.md": """\
# Accounts Payable Reconciliation — Month-End Workflow

## Trigger
Run on the last business day of each month, initiated by Ledger.

## Steps
1. **Ledger** retrieves all `Accounting` tab rows where Status = "Pending" and date ≤ month-end
2. **Ledger** runs invoice matching procedure (`procedures/invoice_matching.md`)
3. Unmatched items → escalate to Approval Gate
4. Matched items → mark "Reconciled", record in BigQuery `task_outcomes`
5. **Ledger** generates summary row in `Error Logs` tab
6. **Nexus-Prime** archives summary to BigQuery `observability_weekly`

## Expected Duration
< 5 minutes for typical month-end volume.
""",
    "workflows/order_fulfillment.md": """\
# Order Fulfillment Workflow (Deal-to-Delivery)

## Trigger
New row in `Sales by Product` tab with Status = "Closed Won"

## Steps
1. **Pursuit** marks deal as "Fulfillment Queued" and publishes to `agent.pursuit.events`
2. **Foreman** receives event, creates shipment record in `Shipping and Receiving`
3. **Foreman** checks inventory; triggers reorder if below threshold
4. **Foreman** updates shipment Status to "In Transit" when carrier confirmed
5. **Foreman** updates to "Delivered" on confirmation; publishes to `agent.foreman.events`
6. **Ledger** receives event, creates invoice row in `Accounting`
7. **Pursuit** updates CRM row to "Delivered"

## SLA
Order to shipment: 2 business days. Escalate if exceeded.
""",
    "workflows/weekly_reporting.md": """\
# Weekly Summary Generation Workflow

## Trigger
Every Monday at 08:00 (Cloud Scheduler job, when configured)

## Steps
1. **Nexus-Prime** queries each agent's Sheet tab for the prior week's rows
2. **Nexus-Prime** aggregates: tasks started, succeeded, escalated, total cost
3. **Nexus-Prime** identifies top constraint and top error fingerprint
4. **Nexus-Prime** writes summary row to `Error Logs` tab
5. After 7 days, `Error Logs` row is archived to BigQuery `observability_weekly`

## Distribution
Summary is available in the Sheet. No external distribution until Communications Policy is updated.
""",
}


def get_or_create_folder(drive, parent_id: str, name: str) -> str:
    """Return folder ID, creating it if it doesn't exist."""
    q = (
        f"name='{name}' and '{parent_id}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    results = drive.files().list(q=q, fields="files(id)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    folder = drive.files().create(body={
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }, fields="id").execute()
    return folder["id"]


def file_exists(drive, parent_id: str, name: str) -> bool:
    q = f"name='{name}' and '{parent_id}' in parents and trashed=false"
    results = drive.files().list(q=q, fields="files(id)").execute()
    return bool(results.get("files"))


def main() -> None:
    with open(SETTINGS_PATH) as f:
        settings = yaml.safe_load(f)

    knowledge_folder_id = settings["projects"]["default"]["drive_folder_id"]
    print(f"Knowledge folder: {knowledge_folder_id}\n")

    creds, _ = google.auth.default(scopes=SCOPES)
    creds.refresh(Request())
    drive = build("drive", "v3", credentials=creds)

    # Ensure subfolder structure exists
    subfolder_ids: dict[str, str] = {}
    for subfolder in ("policies", "procedures", "workflows", "archive"):
        fid = get_or_create_folder(drive, knowledge_folder_id, subfolder)
        subfolder_ids[subfolder] = fid

    # Upload each seed file
    created = skipped = 0
    for rel_path, content in SEED_FILES.items():
        subfolder, filename = rel_path.split("/", 1)
        parent_id = subfolder_ids[subfolder]

        if file_exists(drive, parent_id, filename):
            print(f"  skip  {rel_path} (already exists)")
            skipped += 1
            continue

        drive.files().create(
            body={
                "name": filename,
                "parents": [parent_id],
                "mimeType": "text/plain",
            },
            media_body=_media(content),
            fields="id",
        ).execute()
        print(f"  created {rel_path}")
        created += 1

    print(f"\nDone — {created} created, {skipped} skipped.")


def _media(content: str):
    from googleapiclient.http import MediaInMemoryUpload
    return MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain")


if __name__ == "__main__":
    main()
