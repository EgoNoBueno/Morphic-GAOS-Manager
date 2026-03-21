"""scripts/_sync_e2e_test.py — One-shot §4d Chat-path approval E2E via /sync.

Reads NP_TOKEN_VAL and NP_PROP / NP_URL from environment (set by caller).
"""

from __future__ import annotations

import os
import sys

import httpx

tok = os.environ.get("NP_TOKEN_VAL", "").strip()
url = os.environ.get("NP_URL", "https://nexus-prime-975461050387.us-central1.run.app")
proposal_id = os.environ.get("NP_PROP", "bcd7b40d-4187-4fbd-bf46-23806b5bcb8b")

if not tok:
    print("ERROR: NP_TOKEN_VAL not set")
    sys.exit(1)

print(f"Calling {url}/sync with proposal_id={proposal_id}")
resp = httpx.post(
    url + "/sync",
    json={
        "proposal_id": proposal_id,
        "project_id": "morphic-gaos-prod",
        "status": "Approved",
        "approved_by": "e2e-test@local",
    },
    headers={"Authorization": f"Bearer {tok}"},
    timeout=45,
)
print(f"HTTP {resp.status_code}")
print(resp.text[:600])
