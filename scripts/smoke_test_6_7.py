"""
scripts/smoke_test_6_7.py — Smoke tests 6 and 7: HMAC webhook security.

Runs all 8 webhook test cases from GAOS-Manager-Spec.md §14 against the
live Apps Script doPost endpoint:

  1. POST with valid HMAC signature + valid payload         → statusCode 200
  2. POST with tampered signature (1 char changed)          → statusCode 401
  3. POST with missing signature parameter                  → statusCode 401
  4. POST with valid signature but missing project_id       → statusCode 400
  5. POST with valid signature but invalid project_id       → statusCode 400
  6. POST with valid signature but priority out of range    → statusCode 400
  7. POST with empty body                                   → statusCode 400/500
  8. Replay: identical valid request sent twice             → statusCode 200 (by design)

Pre-conditions:
  - WEBHOOK_URL secret exists in Secret Manager and points to the live
    Apps Script Web App (set up by scripts/setup_apps_script.py).
  - WEBHOOK_HMAC_SECRET secret exists with the shared signing key.
  - config/settings.yaml has correct gcp.project_id.
  - Project Registry tab has at least one row with project_id matching
    gcp.project_id and status = 'active'. This is required for test 1 to
    reach the Sheet write — tests 4/5/6/7/8 do not require it.

Side effects:
  - Test 1 (and test 8 replay) write a row to Agent_Approvals with
    source_agent='smoke-test'. These rows are harmless (Status=Pending)
    and can be deleted manually: find rows with Agent ID = 'smoke-test'
    and ID starting with 'SMOKE67-'.

Run from repo root (venv active):
  python scripts/smoke_test_6_7.py
  python scripts/smoke_test_6_7.py --project-id morphic-gaos-prod
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml

# ── Path setup so tools.* modules resolve from repo root ─────────────────────

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

SETTINGS_PATH = _REPO_ROOT / "config" / "settings.yaml"

# ── Helpers ───────────────────────────────────────────────────────────────────


def _load_project_id() -> str:
    with open(SETTINGS_PATH) as f:
        cfg = yaml.safe_load(f)
    project_id = (cfg.get("gcp") or {}).get("project_id")
    if not project_id:
        raise ValueError(
            f"gcp.project_id is missing or empty in {SETTINGS_PATH}. "
            "Set it before running smoke tests to avoid operating on the wrong project."
        )
    return project_id


def _sign(secret: str, body_bytes: bytes) -> str:
    """Return base64-encoded HMAC-SHA256 of body_bytes."""
    return base64.b64encode(hmac.new(secret.encode(), body_bytes, hashlib.sha256).digest()).decode()


def _post(url: str, body_bytes: bytes, signature: str | None) -> dict:
    """
    POST body_bytes to url. If signature is None the ?signature param is omitted.
    Apps Script always returns HTTP 200; statusCode is in the JSON body.
    """
    full_url = f"{url}?signature={quote(signature)}" if signature is not None else url

    try:
        resp = httpx.post(
            full_url,
            content=body_bytes,
            headers={"Content-Type": "application/json"},
            timeout=30,
            follow_redirects=True,
        )
        try:
            return resp.json()
        except Exception:
            return {"statusCode": resp.status_code, "raw": resp.text[:200]}
    except httpx.TimeoutException:
        return {"statusCode": -1, "error": "Request timed out"}
    except Exception as exc:
        return {"statusCode": -1, "error": str(exc)}


def _make_valid_payload(project_id: str, smoke_id: str) -> dict:
    """Build a valid A2AMessage-shaped payload that passes all three doPost gates."""
    return {
        "message_id": smoke_id,
        "correlation_id": str(uuid.uuid4()),
        "project_id": project_id,
        "source_agent": "smoke-test",
        "target_agent": "nexus-prime",
        "message_type": "SMOKE_TEST",
        "priority": 3,
        "payload": {
            "issue": "Automated smoke test — safe to delete",
            "trigger_reason": "SMOKE_TEST",
        },
        "timestamp": datetime.now(UTC).isoformat(),
    }


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _check(test_num: int, description: str, result: dict, expected_code: int) -> bool:
    """Print test result and return True if passed."""
    got = result.get("statusCode", -1)
    passed = got == expected_code
    status = "✅ PASSED" if passed else "❌ FAILED"
    print(f"  [{status}] Test {test_num}: {description}")
    if not passed:
        print(f"            Expected statusCode={expected_code}, got {got}")
        print(f"            Response: {result}")
    return passed


# ── Main test runner ──────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Run webhook smoke tests 6+7 (8 cases).")
    parser.add_argument(
        "--project-id", default=None, help="Override GCP project ID from settings.yaml"
    )
    args = parser.parse_args()

    project_id = args.project_id or _load_project_id()
    print(f"\nWebhook Smoke Tests 6+7  |  project_id={project_id}")
    print("=" * 60)

    # ── Fetch secrets ─────────────────────────────────────────────────────────
    print("\nFetching secrets from Secret Manager…")
    try:
        from tools.secrets import get_secret

        webhook_url = get_secret("WEBHOOK_URL", project_id)
        hmac_secret = get_secret("WEBHOOK_HMAC_SECRET", project_id)
    except Exception as exc:
        print(f"❌ Failed to fetch secrets: {exc}")
        print("   Ensure WEBHOOK_URL and WEBHOOK_HMAC_SECRET exist in Secret Manager.")
        sys.exit(1)

    print(f"   WEBHOOK_URL: {'*' * 8} (present, {len(webhook_url)} chars)")
    print(f"   WEBHOOK_HMAC_SECRET: {'*' * 8} (present, {len(hmac_secret)} chars)")

    # ── Build the canonical valid payload once ────────────────────────────────
    smoke_id = f"SMOKE67-{uuid.uuid4().hex[:8].upper()}"
    valid_payload = _make_valid_payload(project_id, smoke_id)
    valid_body = _canonical(valid_payload)
    valid_sig = _sign(hmac_secret, valid_body)

    print(f"\nSmoke ID: {smoke_id}")
    print(f"Note: Tests 1 and 8 will write rows to Agent_Approvals (ID={smoke_id}).")
    print("      Clean up by deleting rows where 'Agent ID' = 'smoke-test'.\n")

    results: list[bool] = []

    # ── Test 1: valid signature + valid payload → 200 ─────────────────────────
    print("Running tests…")
    r = _post(webhook_url, valid_body, valid_sig)
    results.append(_check(1, "Valid HMAC + valid payload → 200", r, 200))

    # ── Test 2: tampered signature (flip one char) → 401 ─────────────────────
    tampered_sig = valid_sig[:-1] + ("A" if valid_sig[-1] != "A" else "B")
    r = _post(webhook_url, valid_body, tampered_sig)
    results.append(_check(2, "Tampered signature → 401", r, 401))

    # ── Test 3: missing signature parameter → 401 ─────────────────────────────
    r = _post(webhook_url, valid_body, None)
    results.append(_check(3, "Missing signature param → 401", r, 401))

    # ── Test 4: valid sig but missing project_id → 400 ───────────────────────
    payload_no_pid = {k: v for k, v in valid_payload.items() if k != "project_id"}
    body_no_pid = _canonical(payload_no_pid)
    sig_no_pid = _sign(hmac_secret, body_no_pid)
    r = _post(webhook_url, body_no_pid, sig_no_pid)
    results.append(_check(4, "Valid sig, missing project_id → 400", r, 400))

    # ── Test 5: valid sig but project_id not in registry → 400 ───────────────
    payload_bad_pid = {**valid_payload, "project_id": "smoke-invalid-project-99999"}
    body_bad_pid = _canonical(payload_bad_pid)
    sig_bad_pid = _sign(hmac_secret, body_bad_pid)
    r = _post(webhook_url, body_bad_pid, sig_bad_pid)
    results.append(_check(5, "Valid sig, project_id not in registry → 400", r, 400))

    # ── Test 6: valid sig but priority out of range (0) → 400 ────────────────
    payload_bad_pri = {**valid_payload, "priority": 0}
    body_bad_pri = _canonical(payload_bad_pri)
    sig_bad_pri = _sign(hmac_secret, body_bad_pri)
    r = _post(webhook_url, body_bad_pri, sig_bad_pri)
    results.append(_check(6, "Valid sig, priority=0 (out of range) → 400", r, 400))

    # ── Test 7: empty body → 400 or 500 ──────────────────────────────────────
    empty_body = b""
    sig_empty = _sign(hmac_secret, empty_body)
    r = _post(webhook_url, empty_body, sig_empty)
    got_7 = r.get("statusCode", -1)
    # Accept 400 or 500 per spec — either means the body was rejected gracefully
    passed_7 = got_7 in (400, 500)
    results.append(passed_7)
    status_7 = "✅ PASSED" if passed_7 else "❌ FAILED"
    print(f"  [{status_7}] Test 7: Empty body → 400 or 500 (got {got_7})")
    if not passed_7:
        print(f"            Response: {r}")

    # ── Test 8: replay (send identical valid request again) → 200 ─────────────
    # HMAC alone does not prevent replay — second request must be accepted per spec.
    r = _post(webhook_url, valid_body, valid_sig)
    results.append(_check(8, "Replay: identical valid request → 200 (by design)", r, 200))

    # ── Summary ───────────────────────────────────────────────────────────────
    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"Result: {passed}/{total} tests passed")

    if passed == total:
        print("✅ ALL WEBHOOK TESTS PASSED — smoke tests 6+7 complete.")
        print("\n⚠  Clean up: delete rows with Agent ID='smoke-test' from Agent_Approvals.")
    else:
        print("❌ SOME TESTS FAILED — see details above.")
        print("\nCommon causes:")
        print("  Test 1/5 fail: Project Registry has no active row for this project_id.")
        print(
            "  Test 2/3 fail: WEBHOOK_HMAC_SECRET in Secret Manager mismatches Apps Script properties."
        )
        print("  All fail: WEBHOOK_URL is outdated — re-run setup_apps_script.py --deploy.")
        sys.exit(1)


if __name__ == "__main__":
    main()
