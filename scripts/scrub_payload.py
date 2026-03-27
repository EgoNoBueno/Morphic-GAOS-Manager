"""
scripts/scrub_payload.py — Scrub a captured Pub/Sub push envelope for regression testing.

Usage:
    python scripts/scrub_payload.py <raw_payload.json> <agent> <scenario>

    <raw_payload.json>  Path to the raw captured envelope (or "-" to read stdin).
    <agent>             Agent name slug, e.g. "nexus_prime".
    <scenario>          Short scenario description, e.g. "scout-escalation-malformed-priority".

The script:
1. Reads the raw Pub/Sub push envelope (JSON).
2. Base64-decodes and parses the A2AMessage inside.
3. Replaces project_id with "test-project".
4. Replaces all email-like strings in payload values with "user@example.com".
5. Re-encodes to base64 and rebuilds the envelope.
6. Adds a _meta block with scenario, captured date, and an expected.route placeholder.
7. Writes the scrubbed file to tests/payloads/<agent>/<YYYY-MM-DD>-<scenario>.json.

The resulting file is safe to commit: no real project IDs, emails, or credentials.
After running, edit the "route" value in _meta.expected to match the LangGraph node
that route() should return for this message (e.g. "think", "record", "init_project").
"""

from __future__ import annotations

import base64
import json
import re
import sys
from datetime import date
from pathlib import Path

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_SCRUBBED_EMAIL = "user@example.com"
_SCRUBBED_PROJECT = "test-project"

# Valid route() return values — used to populate the placeholder comment.
_VALID_ROUTES = (
    "think",
    "record",
    "promote",
    "park_or_broadcast",
    "conflict_resolve",
    "init_project",
    "vision_blueprint",
    "iterate_plan",
    "handle_skill_request",
    "market_watchdog",
    "roi_optimizer",
)


def _scrub_str(value: str) -> str:
    """Replace email addresses in a string with the scrubbed placeholder."""
    return _EMAIL_RE.sub(_SCRUBBED_EMAIL, value)


def _scrub_value(value: object) -> object:
    """Recursively scrub strings inside dicts and lists."""
    if isinstance(value, str):
        return _scrub_str(value)
    if isinstance(value, dict):
        return {k: _scrub_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_value(item) for item in value]
    return value


def scrub(raw_envelope: dict, agent: str, scenario: str) -> dict:
    """
    Scrub a raw Pub/Sub push envelope and add the _meta regression block.

    Args:
        raw_envelope: The parsed JSON dict of the captured push envelope.
        agent: Agent name slug used for the output directory, e.g. "nexus_prime".
        scenario: Short kebab-case description of the test scenario.

    Returns:
        A new dict ready to write as the regression payload file.

    Raises:
        KeyError: If the envelope is missing the expected Pub/Sub structure.
        ValueError: If the base64 data cannot be decoded or parsed as JSON.
    """
    # Decode payload.
    try:
        data_b64 = raw_envelope["message"]["data"]
        inner = json.loads(base64.b64decode(data_b64).decode("utf-8"))
    except (KeyError, ValueError) as exc:
        raise ValueError(f"Cannot decode envelope: {exc}") from exc

    # Scrub project_id.
    inner["project_id"] = _SCRUBBED_PROJECT

    # Scrub email-like strings in the payload dict.
    if "payload" in inner and isinstance(inner["payload"], dict):
        inner["payload"] = _scrub_value(inner["payload"])

    # Re-encode the scrubbed inner message.
    encoded = base64.b64encode(json.dumps(inner).encode()).decode()

    scrubbed_envelope = {
        "_meta": {
            "scenario": scenario,
            "agent": agent,
            "captured": date.today().isoformat(),
            "expected": {
                "route": "__FILL_ME_IN__",
                "_valid_routes": list(_VALID_ROUTES),
            },
        },
        "message": {
            "data": encoded,
            "messageId": raw_envelope.get("message", {}).get("messageId", "test-msg-001"),
        },
        "subscription": f"projects/{_SCRUBBED_PROJECT}/subscriptions/test-sub",
    }
    return scrubbed_envelope


def main() -> None:
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)

    raw_path, agent, scenario = sys.argv[1], sys.argv[2], sys.argv[3]

    if raw_path == "-":
        raw_envelope = json.load(sys.stdin)
    else:
        raw_envelope = json.loads(Path(raw_path).read_text(encoding="utf-8"))

    result = scrub(raw_envelope, agent, scenario)

    out_dir = Path(__file__).parent.parent / "tests" / "payloads" / agent
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{date.today().isoformat()}-{scenario}.json"
    out_file.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"Written: {out_file}")
    print(f"Next step: edit 'route' in _meta.expected. Valid values: {', '.join(_VALID_ROUTES)}")


if __name__ == "__main__":
    main()
