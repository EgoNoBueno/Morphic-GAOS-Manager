"""
tests/test_deal_closed.py — Unit tests for agents/pursuit/tasks/deal_closed.py

Covers: happy path (sheet write + both publishes), sheet write failure,
publish failures (non-fatal), low-margin flag, project_id forwarding.
All GCP calls are mocked at the SDK boundary.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from models import AgentInput, MessageType

_PROJECT_ID = "test-project"
_TASK_ID = "task-dc-001"


def _make_input(context: dict[str, Any] | None = None) -> AgentInput:
    return AgentInput(
        task_id=_TASK_ID,
        project_id=_PROJECT_ID,
        instruction="Process deal close.",
        context=context or {},
    )


_DEAL_CTX = {
    "deal_id": "DEAL-001",
    "client_name": "Acme Corp",
    "product": "Widget Pro",
    "amount_usd": 10000.0,
    "cost_usd": 7000.0,
    "contact_email": "billing@acme.com",
}


class TestDealClosedHappyPath:
    def test_success_status_returned(self):
        from agents.pursuit.tasks.deal_closed import run

        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.append_row"),
            patch("tools.pubsub.publish"),
        ):
            out = run(_make_input(_DEAL_CTX))

        assert out.status == "success"

    def test_gross_margin_calculated_correctly(self):
        from agents.pursuit.tasks.deal_closed import run

        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.append_row"),
            patch("tools.pubsub.publish"),
        ):
            out = run(_make_input(_DEAL_CTX))

        # (10000 - 7000) / 10000 = 0.30
        assert abs(out.result["gross_margin"] - 0.30) < 0.001

    def test_low_margin_flag_false_above_threshold(self):
        from agents.pursuit.tasks.deal_closed import run

        ctx = {**_DEAL_CTX, "amount_usd": 10000.0, "cost_usd": 7000.0}  # 30% margin
        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.append_row"),
            patch("tools.pubsub.publish"),
        ):
            out = run(_make_input(ctx))

        assert out.result["low_margin"] is False

    def test_low_margin_flag_true_below_threshold(self):
        from agents.pursuit.tasks.deal_closed import run

        ctx = {**_DEAL_CTX, "amount_usd": 10000.0, "cost_usd": 9500.0}  # 5% margin
        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.append_row"),
            patch("tools.pubsub.publish"),
        ):
            out = run(_make_input(ctx))

        assert out.result["low_margin"] is True

    def test_deal_id_echoed_in_result(self):
        from agents.pursuit.tasks.deal_closed import run

        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.append_row"),
            patch("tools.pubsub.publish"),
        ):
            out = run(_make_input(_DEAL_CTX))

        assert out.result["deal_id"] == "DEAL-001"

    def test_both_publishes_confirmed_in_result(self):
        from agents.pursuit.tasks.deal_closed import run

        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.append_row"),
            patch("tools.pubsub.publish"),
        ):
            out = run(_make_input(_DEAL_CTX))

        assert out.result["handoff_to_ledger"] is True
        assert out.result["deal_closed_to_nexus"] is True

    def test_publish_called_twice_for_ledger_and_nexus(self):
        from agents.pursuit.tasks.deal_closed import run

        mock_publish = MagicMock()
        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.append_row"),
            patch("tools.pubsub.publish", mock_publish),
        ):
            run(_make_input(_DEAL_CTX))

        assert mock_publish.call_count == 2
        msg_types = [c.args[1].message_type for c in mock_publish.call_args_list]
        assert MessageType.TASK_HANDOFF in msg_types
        assert MessageType.DEAL_CLOSED in msg_types

    def test_task_handoff_targets_ledger(self):
        from agents.pursuit.tasks.deal_closed import run

        published: list = []
        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.append_row"),
            patch("tools.pubsub.publish", side_effect=lambda topic, msg: published.append(msg)),
        ):
            run(_make_input(_DEAL_CTX))

        handoff = next(m for m in published if m.message_type == MessageType.TASK_HANDOFF)
        assert handoff.target_agent == "ledger"

    def test_deal_closed_targets_nexus_prime(self):
        from agents.pursuit.tasks.deal_closed import run

        published: list = []
        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.append_row"),
            patch("tools.pubsub.publish", side_effect=lambda topic, msg: published.append(msg)),
        ):
            run(_make_input(_DEAL_CTX))

        deal_closed = next(m for m in published if m.message_type == MessageType.DEAL_CLOSED)
        assert deal_closed.target_agent == "nexus-prime"


class TestDealClosedFailures:
    def test_sheet_write_failure_returns_escalated(self):
        from agents.pursuit.tasks.deal_closed import run

        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.append_row", side_effect=Exception("Sheet unavailable")),
        ):
            out = run(_make_input(_DEAL_CTX))

        assert out.status == "escalated"
        assert "Sheet write failed" in out.result["error"]

    def test_publish_failure_does_not_escalate(self):
        from agents.pursuit.tasks.deal_closed import run

        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.append_row"),
            patch("tools.pubsub.publish", side_effect=Exception("Pub/Sub unavailable")),
        ):
            out = run(_make_input(_DEAL_CTX))

        assert out.status == "success"
        assert out.result["handoff_to_ledger"] is False
        assert out.result["deal_closed_to_nexus"] is False

    def test_project_id_forwarded_to_sheet(self):
        from agents.pursuit.tasks.deal_closed import run

        captured: list[str] = []

        def _capture(tab: str, row: dict, pid: str) -> None:
            captured.append(pid)

        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.append_row", side_effect=_capture),
            patch("tools.pubsub.publish"),
        ):
            run(_make_input(_DEAL_CTX))

        assert captured == [_PROJECT_ID]

    def test_project_id_forwarded_to_published_messages(self):
        from agents.pursuit.tasks.deal_closed import run

        published: list = []
        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.append_row"),
            patch("tools.pubsub.publish", side_effect=lambda topic, msg: published.append(msg)),
        ):
            run(_make_input(_DEAL_CTX))

        for msg in published:
            assert msg.project_id == _PROJECT_ID

    def test_zero_amount_produces_zero_margin(self):
        from agents.pursuit.tasks.deal_closed import run

        ctx = {**_DEAL_CTX, "amount_usd": 0.0}
        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.append_row"),
            patch("tools.pubsub.publish"),
        ):
            out = run(_make_input(ctx))

        assert out.result["gross_margin"] == 0.0

    def test_cost_usd_always_zero(self):
        from agents.pursuit.tasks.deal_closed import run

        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.append_row"),
            patch("tools.pubsub.publish"),
        ):
            out = run(_make_input(_DEAL_CTX))

        assert out.cost_usd == 0.0
