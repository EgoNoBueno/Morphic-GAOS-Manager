"""One-shot script: add nexus-prime SA as editor on Agent_Approvals Status column protection."""

import google.auth
import yaml
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SA_EMAIL = "nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

with open("config/settings.yaml") as f:
    cfg = yaml.safe_load(f)
SS_ID = cfg["sheet"]["workbook_id"]

creds, _ = google.auth.default(scopes=SCOPES)
creds.refresh(Request())
svc = build("sheets", "v4", credentials=creds)

# List all protected ranges
ss = (
    svc.spreadsheets()
    .get(
        spreadsheetId=SS_ID,
        fields="sheets(properties(sheetId,title),protectedRanges)",
    )
    .execute()
)

target_ids = []
for sheet in ss.get("sheets", []):
    title = sheet["properties"]["title"]
    for pr in sheet.get("protectedRanges", []):
        desc = pr.get("description", "")
        editors = pr.get("editors", {})
        print(f"  {title}: [{pr['protectedRangeId']}] '{desc}' users={editors.get('users', [])}")
        if desc == "Status \u2014 owner only":
            target_ids.append((pr["protectedRangeId"], editors.get("users", [])))

if not target_ids:
    print("\nNo protection with description 'Status — owner only' found.")
    raise SystemExit(1)

print(f"\nPatching {len(target_ids)} 'Status — owner only' protection(s) ...")
requests = []
for target_id, current_editors in target_ids:
    if SA_EMAIL in current_editors:
        print(f"  [{target_id}] already has SA — skipping")
        continue
    new_editors = current_editors + [SA_EMAIL]
    requests.append(
        {
            "updateProtectedRange": {
                "protectedRange": {
                    "protectedRangeId": target_id,
                    "editors": {"users": new_editors},
                },
                "fields": "editors",
            }
        }
    )

if not requests:
    print("  All protections already include SA — nothing to do.")
    raise SystemExit(0)

body = {"requests": requests}
resp = svc.spreadsheets().batchUpdate(spreadsheetId=SS_ID, body=body).execute()
print(f"  Done. Patched {len(requests)} protection(s).")
