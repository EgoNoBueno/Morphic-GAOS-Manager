"""
tools/pubsub.py — Cloud Pub/Sub publisher for Morphic-G AOS.

Handles outbound A2AMessage publishing and inbound push-delivery decoding.
All agent-to-agent messages travel through this module on their way to Pub/Sub.

Topic naming convention:
  A2AMessage uses "/" as a display separator (e.g. "agent/beacon/events").
  GCP Pub/Sub topic names cannot contain "/", so slashes are replaced with "."
  when building the resource path (e.g. "agent.beacon.events").

Spec: GAOS-Tools-Spec.md §4
"""
from __future__ import annotations

import base64
import json

from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud import pubsub_v1

from config import get_settings
from models import A2AMessage

# ── Error types ────────────────────────────────────────────────────────────


class TopicNotFoundError(Exception):
    """Pub/Sub topic does not exist."""


class PubSubPublishError(Exception):
    """Unrecoverable publish error."""


class PubSubAdminError(Exception):
    """Cannot create or manage Pub/Sub topics."""


class MessageDecodeError(Exception):
    """Cannot decode the raw Pub/Sub push envelope."""


class MessageValidationError(Exception):
    """Decoded message does not satisfy A2AMessage schema."""


# ── Internal helpers ───────────────────────────────────────────────────────


def _topic_path(topic_name: str, project_id: str) -> str:
    """
    Build the fully-qualified Pub/Sub topic resource path.
    Replaces "/" with "." in topic_name per GCP naming constraints.

    "agent/beacon/events" → "projects/proj/topics/agent.beacon.events"
    """
    gcp_name = topic_name.replace("/", ".")
    return f"projects/{project_id}/topics/{gcp_name}"


# ── Public API ─────────────────────────────────────────────────────────────


def publish(topic_name: str, message: A2AMessage, project_id: str) -> str:
    """
    Serialize and publish one A2AMessage to the named topic.

    Args:
        topic_name: Short name without the full resource path
                    (e.g. "agent/beacon/events").
        message:    A validated A2AMessage instance.
        project_id: GCP project that owns the topic.

    Returns:
        message_id: The Pub/Sub-assigned message ID (string).

    Raises:
        TopicNotFoundError:  Topic does not exist.
        PubSubPublishError:  Unrecoverable publish error.
    """
    settings = get_settings()
    # Always use centralised GCP project from settings (project_id param
    # is the AOS namespace, not the GCP project).
    gcp_project = settings.GCP_PROJECT_ID
    topic = _topic_path(topic_name, gcp_project)

    publisher = pubsub_v1.PublisherClient()
    payload = message.model_dump_json().encode("utf-8")

    try:
        future = publisher.publish(topic, data=payload)
        return future.result()
    except NotFound as exc:
        raise TopicNotFoundError(
            f"Topic '{topic}' does not exist. Call ensure_topic_exists() during "
            "the agent boot sequence (step 5)."
        ) from exc
    except Exception as exc:
        raise PubSubPublishError(
            f"Failed to publish message to '{topic}': {exc}"
        ) from exc


def ensure_topic_exists(topic_name: str, project_id: str) -> None:
    """
    Idempotent topic creation. Creates the topic if it does not exist.
    Safe to call on every boot — used in the agent boot sequence (step 5).

    Args:
        topic_name: Short name (e.g. "agent/beacon/events").
        project_id: GCP project that should own the topic.

    Raises:
        PubSubAdminError: Cannot create topic (permissions or quota error).
    """
    settings = get_settings()
    # Always use centralised GCP project from settings.
    gcp_project = settings.GCP_PROJECT_ID
    topic = _topic_path(topic_name, gcp_project)

    publisher = pubsub_v1.PublisherClient()
    try:
        publisher.create_topic(request={"name": topic})
    except AlreadyExists:
        pass  # idempotent — topic already exists
    except Exception as exc:
        raise PubSubAdminError(
            f"Cannot create topic '{topic}': {exc}. "
            "Check that the service account has roles/pubsub.admin."
        ) from exc


def decode_push_message(envelope: dict) -> A2AMessage:
    """
    Decode a Pub/Sub push delivery envelope into a validated A2AMessage.

    The envelope format from Cloud Run push subscriptions:
        {
          "message": {
            "data": "<base64-encoded JSON>",
            "messageId": "...",
            ...
          },
          "subscription": "..."
        }

    Args:
        envelope: The raw dict parsed from the HTTP request body.

    Returns:
        A validated A2AMessage instance.

    Raises:
        MessageDecodeError:     Base64 or JSON decode failure.
        MessageValidationError: Decoded message fails A2AMessage schema.
    """
    try:
        message_dict = envelope["message"]
        data_b64 = message_dict["data"]
        data_bytes = base64.b64decode(data_b64)
        data_str = data_bytes.decode("utf-8")
        raw = json.loads(data_str)
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        raise MessageDecodeError(
            f"Failed to decode Pub/Sub push envelope: {exc}"
        ) from exc

    try:
        return A2AMessage.model_validate(raw)
    except Exception as exc:
        raise MessageValidationError(
            f"Decoded message does not satisfy A2AMessage schema: {exc}"
        ) from exc
