"""
scripts/smoke_test_pubsub_sub.py — Phase 1 Pub/Sub subscriber smoke test.

Verifies that a local Python process can pull a message from the
agent.approvals.events topic and decode a simulated APPROVAL_RESULT payload.

Steps:
  1. Ensures topic agent.approvals.events exists.
  2. Creates a temporary pull subscription (deleted on exit).
  3. Publishes a synthetic APPROVAL_RESULT message.
  4. Pulls and acks the message.
  5. Prints proposal_id and new status to stdout.

Exit code 0 = pass, 1 = fail.
"""
from __future__ import annotations

import base64
import json
import sys
import uuid
from datetime import datetime, timezone

from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud import pubsub_v1

PROJECT_ID = "morphic-gaos-prod"
TOPIC_NAME = "agent.approvals.events"
SUB_NAME = f"local-smoketest-sub-{uuid.uuid4().hex[:8]}"

TOPIC_PATH = f"projects/{PROJECT_ID}/topics/{TOPIC_NAME}"
SUB_PATH = f"projects/{PROJECT_ID}/subscriptions/{SUB_NAME}"


def ensure_topic(publisher: pubsub_v1.PublisherClient) -> None:
    try:
        publisher.create_topic(request={"name": TOPIC_PATH})
        print(f"  [setup] Created topic {TOPIC_NAME}")
    except AlreadyExists:
        print(f"  [setup] Topic {TOPIC_NAME} already exists — OK")


def create_sub(subscriber: pubsub_v1.SubscriberClient) -> None:
    subscriber.create_subscription(
        request={"name": SUB_PATH, "topic": TOPIC_PATH, "ack_deadline_seconds": 30}
    )
    print(f"  [setup] Created pull subscription {SUB_NAME}")


def delete_sub(subscriber: pubsub_v1.SubscriberClient) -> None:
    try:
        subscriber.delete_subscription(request={"subscription": SUB_PATH})
        print(f"  [cleanup] Deleted subscription {SUB_NAME}")
    except NotFound:
        pass


def publish_test_message(publisher: pubsub_v1.PublisherClient) -> str:
    proposal_id = str(uuid.uuid4())
    payload = {
        "message_type": "APPROVAL_RESULT",
        "proposal_id": proposal_id,
        "new_status": "Approved",
        "approved_by": "smoke-test@local",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    data = json.dumps(payload).encode("utf-8")
    future = publisher.publish(TOPIC_PATH, data=data)
    future.result()
    print(f"  [publish] Sent synthetic APPROVAL_RESULT — proposal_id={proposal_id}")
    return proposal_id


def pull_message(subscriber: pubsub_v1.SubscriberClient, expected_proposal_id: str) -> bool:
    import time
    deadline = time.monotonic() + 15  # 15-second window
    while time.monotonic() < deadline:
        response = subscriber.pull(
            request={"subscription": SUB_PATH, "max_messages": 1},
            timeout=5,
        )
        if not response.received_messages:
            continue

        msg = response.received_messages[0]
        raw = msg.message.data
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"  [FAIL] Could not decode message: {exc}")
            return False

        proposal_id = decoded.get("proposal_id", "<missing>")
        new_status = decoded.get("new_status", "<missing>")

        print()
        print(f"  proposal_id : {proposal_id}")
        print(f"  new_status  : {new_status}")
        print()

        subscriber.acknowledge(
            request={"subscription": SUB_PATH, "ack_ids": [msg.ack_id]}
        )

        if proposal_id != expected_proposal_id:
            print(f"  [FAIL] proposal_id mismatch: expected {expected_proposal_id}")
            return False
        if new_status != "Approved":
            print(f"  [FAIL] new_status mismatch: expected 'Approved'")
            return False

        return True

    print("  [FAIL] Timed out waiting for message pull (15 s)")
    return False


def main() -> None:
    publisher = pubsub_v1.PublisherClient()
    subscriber = pubsub_v1.SubscriberClient()

    print("=== Pub/Sub local subscriber smoke test ===")
    print(f"Project : {PROJECT_ID}")
    print(f"Topic   : {TOPIC_NAME}")
    print()

    try:
        ensure_topic(publisher)
        create_sub(subscriber)
        proposal_id = publish_test_message(publisher)
        passed = pull_message(subscriber, proposal_id)
    finally:
        delete_sub(subscriber)

    if passed:
        print("RESULT: PASS — local subscriber received APPROVAL_RESULT ✓")
        sys.exit(0)
    else:
        print("RESULT: FAIL")
        sys.exit(1)


if __name__ == "__main__":
    main()
