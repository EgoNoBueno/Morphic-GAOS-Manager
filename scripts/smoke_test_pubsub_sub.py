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

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime

from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud import pubsub_v1

TOPIC_NAME = "agent.approvals.events"


def ensure_topic(publisher: pubsub_v1.PublisherClient, topic_path: str) -> None:
    try:
        publisher.create_topic(request={"name": topic_path})
        print(f"  [setup] Created topic {TOPIC_NAME}")
    except AlreadyExists:
        print(f"  [setup] Topic {TOPIC_NAME} already exists — OK")


def create_sub(subscriber: pubsub_v1.SubscriberClient, sub_path: str, topic_path: str) -> None:
    subscriber.create_subscription(
        request={"name": sub_path, "topic": topic_path, "ack_deadline_seconds": 30}
    )
    print(f"  [setup] Created pull subscription {sub_path.split('/')[-1]}")


def delete_sub(subscriber: pubsub_v1.SubscriberClient, sub_path: str) -> None:
    try:
        subscriber.delete_subscription(request={"subscription": sub_path})
        print(f"  [cleanup] Deleted subscription {sub_path.split('/')[-1]}")
    except NotFound:
        pass


def publish_test_message(publisher: pubsub_v1.PublisherClient, topic_path: str) -> str:
    proposal_id = str(uuid.uuid4())
    payload = {
        "message_type": "APPROVAL_RESULT",
        "proposal_id": proposal_id,
        "new_status": "Approved",
        "approved_by": "smoke-test@local",
        "timestamp": datetime.now(UTC).isoformat(),
    }
    data = json.dumps(payload).encode("utf-8")
    future = publisher.publish(topic_path, data=data)
    future.result()
    print(f"  [publish] Sent synthetic APPROVAL_RESULT — proposal_id={proposal_id}")
    return proposal_id


def pull_message(
    subscriber: pubsub_v1.SubscriberClient, sub_path: str, expected_proposal_id: str
) -> bool:
    import time

    deadline = time.monotonic() + 15  # 15-second window
    while time.monotonic() < deadline:
        response = subscriber.pull(
            request={"subscription": sub_path, "max_messages": 1},
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

        subscriber.acknowledge(request={"subscription": sub_path, "ack_ids": [msg.ack_id]})

        if proposal_id != expected_proposal_id:
            print(f"  [FAIL] proposal_id mismatch: expected {expected_proposal_id}")
            return False
        if new_status != "Approved":
            print("  [FAIL] new_status mismatch: expected 'Approved'")
            return False

        return True

    print("  [FAIL] Timed out waiting for message pull (15 s)")
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Pub/Sub local subscriber smoke test")
    parser.add_argument(
        "--project", default="morphic-gaos-prod", help="GCP project ID (default: morphic-gaos-prod)"
    )
    args = parser.parse_args()
    project_id = args.project

    sub_name = f"local-smoketest-sub-{uuid.uuid4().hex[:8]}"
    topic_path = f"projects/{project_id}/topics/{TOPIC_NAME}"
    sub_path = f"projects/{project_id}/subscriptions/{sub_name}"

    publisher = pubsub_v1.PublisherClient()
    subscriber = pubsub_v1.SubscriberClient()

    print("=== Pub/Sub local subscriber smoke test ===")
    print(f"Project : {project_id}")
    print(f"Topic   : {TOPIC_NAME}")
    print()

    try:
        ensure_topic(publisher, topic_path)
        create_sub(subscriber, sub_path, topic_path)
        proposal_id = publish_test_message(publisher, topic_path)
        passed = pull_message(subscriber, sub_path, proposal_id)
    finally:
        delete_sub(subscriber, sub_path)

    if passed:
        print("RESULT: PASS — local subscriber received APPROVAL_RESULT ✓")
        sys.exit(0)
    else:
        print("RESULT: FAIL")
        sys.exit(1)


if __name__ == "__main__":
    main()
