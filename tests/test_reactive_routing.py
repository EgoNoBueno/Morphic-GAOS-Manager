"""tests/test_reactive_routing.py

Unit tests for the reactive cross-domain routing nodes added in Phase 3:
  - market_watchdog  (STOCK_INSUFFICIENT → Scout ALERT)
  - roi_optimizer    (DEAL_CLOSED → Beacon ALERT when margin < threshold)

Coverage:
  - Happy path: correct topic, message type, payload keys
  - No-op path: msg is None → no publish
  - Threshold edge cases: exactly at threshold vs. below
  - Zero-revenue guard (no ZeroDivisionError)
  - Publish failure: logs ERROR, does not re-raise
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import patch

if TYPE_CHECKING:
    from agents.nexus_prime.orchestrator import NexusPrimeWorkingMemory

import pytest

from models import A2AMessage, MessageType

# ── helpers ───────────────────────────────────────────────────────────────────


def _state(
    incoming: A2AMessage | None = None,
    project_id: str = "test-project",
    task_id: str = "task-1",
) -> dict:
    return {
        "project_id": project_id,
        "task_id": task_id,
        "incoming_message": incoming,
    }


def _stock_msg(sku: str = "SKU-001", qty: int = 0) -> A2AMessage:
    return A2AMessage(
        source_agent="foreman",
        target_agent="nexus-prime",
        project_id="test-project",
        task_id="task-1",
        message_type=MessageType.STOCK_INSUFFICIENT,
        priority=4,
        payload={"sku": sku, "quantity_on_hand": qty},
    )


def _deal_msg(
    revenue: float = 1000.0,
    cogs: float = 900.0,
    lead_source: str = "google-ads",
    deal_id: str = "deal-99",
) -> A2AMessage:
    return A2AMessage(
        source_agent="pursuit",
        target_agent="nexus-prime",
        project_id="test-project",
        task_id="task-1",
        message_type=MessageType.DEAL_CLOSED,
        priority=3,
        payload={
            "revenue": revenue,
            "cogs": cogs,
            "lead_source": lead_source,
            "deal_id": deal_id,
        },
    )


# ── market_watchdog ───────────────────────────────────────────────────────────


class TestMarketWatchdog:
    def _run(self, state: dict) -> NexusPrimeWorkingMemory:
        from agents.nexus_prime.orchestrator import market_watchdog

        return market_watchdog(cast("NexusPrimeWorkingMemory", state))

    @patch("agents.nexus_prime.orchestrator._log_cloud")
    @patch("tools.pubsub.publish")
    def test_dispatches_alert_to_scout(self, mock_publish, _mock_log):
        state = _state(incoming=_stock_msg("SKU-001"))
        self._run(state)

        mock_publish.assert_called_once()
        topic, msg = mock_publish.call_args.args
        assert topic == "agent.scout.events"
        assert isinstance(msg, A2AMessage)
        assert msg.message_type == MessageType.ALERT
        assert msg.target_agent == "scout"
        assert msg.payload["alert_type"] == "stock_insufficient"
        assert msg.payload["sku"] == "SKU-001"

    @patch("agents.nexus_prime.orchestrator._log_cloud")
    @patch("tools.pubsub.publish")
    def test_forwards_original_payload_fields(self, mock_publish, _mock_log):
        state = _state(incoming=_stock_msg("SKU-XYZ", qty=3))
        self._run(state)

        _, msg = mock_publish.call_args.args
        assert msg.payload["quantity_on_hand"] == 3

    @patch("agents.nexus_prime.orchestrator._log_cloud")
    @patch("tools.pubsub.publish")
    def test_preserves_state_fields(self, mock_publish, _mock_log):
        state = _state(incoming=_stock_msg(), project_id="acme", task_id="t-99")
        result = self._run(state)
        assert result["project_id"] == "acme"
        assert result["task_id"] == "t-99"

    @patch("agents.nexus_prime.orchestrator._log_cloud")
    @patch("tools.pubsub.publish")
    def test_no_message_skips_publish(self, mock_publish, _mock_log):
        state = _state(incoming=None)
        result = self._run(state)
        mock_publish.assert_not_called()
        assert result is state

    @patch("agents.nexus_prime.orchestrator._log_cloud")
    @patch("tools.pubsub.publish", side_effect=RuntimeError("pubsub down"))
    def test_publish_failure_does_not_raise(self, mock_publish, mock_log):
        state = _state(incoming=_stock_msg())
        result = self._run(state)  # must not propagate RuntimeError
        mock_publish.assert_called_once()
        # An ERROR-severity log must be emitted
        error_calls = [
            c
            for c in mock_log.call_args_list
            if "ERROR" in c.args or (len(c.args) >= 6 and c.args[5] == "ERROR")
        ]
        assert error_calls, "expected an ERROR log on publish failure"
        assert result is state


# ── roi_optimizer ─────────────────────────────────────────────────────────────


class TestRoiOptimizer:
    def _run(self, state: dict) -> NexusPrimeWorkingMemory:
        from agents.nexus_prime.orchestrator import roi_optimizer

        return roi_optimizer(cast("NexusPrimeWorkingMemory", state))

    @patch("agents.nexus_prime.orchestrator._log_cloud")
    @patch("tools.pubsub.publish")
    def test_low_margin_dispatches_beacon_alert(self, mock_publish, _mock_log):
        # margin = (1000 - 900) / 1000 = 10% → below 20%
        state = _state(incoming=_deal_msg(revenue=1000.0, cogs=900.0))
        self._run(state)

        mock_publish.assert_called_once()
        topic, msg = mock_publish.call_args.args
        assert topic == "agent.beacon.events"
        assert msg.message_type == MessageType.ALERT
        assert msg.target_agent == "beacon"
        assert msg.payload["alert_type"] == "low_margin"
        assert msg.payload["margin_pct"] == pytest.approx(10.0, abs=0.01)
        assert msg.payload["threshold_pct"] == pytest.approx(20.0, abs=0.01)

    @patch("agents.nexus_prime.orchestrator._log_cloud")
    @patch("tools.pubsub.publish")
    def test_lead_source_forwarded_in_alert(self, mock_publish, _mock_log):
        state = _state(incoming=_deal_msg(revenue=1000.0, cogs=900.0, lead_source="facebook-ads"))
        self._run(state)
        _, msg = mock_publish.call_args.args
        assert msg.payload["lead_source"] == "facebook-ads"

    @patch("agents.nexus_prime.orchestrator._log_cloud")
    @patch("tools.pubsub.publish")
    def test_high_margin_does_not_dispatch(self, mock_publish, _mock_log):
        # margin = (1000 - 500) / 1000 = 50% → above 20%
        state = _state(incoming=_deal_msg(revenue=1000.0, cogs=500.0))
        self._run(state)
        mock_publish.assert_not_called()

    @patch("agents.nexus_prime.orchestrator._log_cloud")
    @patch("tools.pubsub.publish")
    def test_exactly_at_threshold_does_not_dispatch(self, mock_publish, _mock_log):
        # margin = (1000 - 800) / 1000 = 20.0% — at threshold, no action
        state = _state(incoming=_deal_msg(revenue=1000.0, cogs=800.0))
        self._run(state)
        mock_publish.assert_not_called()

    @patch("agents.nexus_prime.orchestrator._log_cloud")
    @patch("tools.pubsub.publish")
    def test_zero_revenue_does_not_raise(self, mock_publish, _mock_log):
        # Guard: revenue=0 → margin=0.0, which is below threshold → Beacon alert sent
        state = _state(incoming=_deal_msg(revenue=0.0, cogs=0.0))
        result = self._run(state)  # must not raise ZeroDivisionError
        assert result is state

    @patch("agents.nexus_prime.orchestrator._log_cloud")
    @patch("tools.pubsub.publish")
    def test_no_message_skips_publish(self, mock_publish, _mock_log):
        state = _state(incoming=None)
        result = self._run(state)
        mock_publish.assert_not_called()
        assert result is state

    @patch("agents.nexus_prime.orchestrator._log_cloud")
    @patch("tools.pubsub.publish", side_effect=RuntimeError("pubsub down"))
    def test_publish_failure_does_not_raise(self, mock_publish, mock_log):
        # margin = 10%, below threshold → will attempt publish
        state = _state(incoming=_deal_msg(revenue=1000.0, cogs=900.0))
        result = self._run(state)  # must not propagate RuntimeError
        mock_publish.assert_called_once()
        error_calls = [
            c
            for c in mock_log.call_args_list
            if "ERROR" in c.args or (len(c.args) >= 6 and c.args[5] == "ERROR")
        ]
        assert error_calls, "expected an ERROR log on publish failure"
        assert result is state
