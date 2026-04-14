"""tests/test_api_metrics.py — Unit tests for the API telemetry layer.

Covers: record_api_call context manager, tracked decorator, recursion guard,
        tokens_used/model ctx propagation, and circuit breaker BQ event writes.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────────────

SETTINGS_YAML = """\
gcp:
  project_id: test-project
  region: us-central1
sheet:
  workbook_id: spreadsheet-123
models:
  LOCAL_MODEL: ollama/llama3.1
  FAST_MODEL: gemini-2.0-flash
  DEEP_MODEL: gemini-2.0-pro
  LOCAL_MODEL_FALLBACK: gemini-2.0-flash
  LOCAL_MODEL_TIMEOUT_SECONDS: 2
projects:
  default:
    sheet_id: spreadsheet-123
    drive_folder_id: folder-abc
"""


@pytest.fixture(autouse=True)
def _suppress_telemetry_writes():
    """Override the conftest global suppression.

    test_api_metrics.py tests assert on actual _write_metric invocations and
    row content, so _write_metric must NOT be no-op'd here.  Each test that
    needs to capture BQ writes patches tools.bigquery.insert_row directly.
    """
    yield


@pytest.fixture(autouse=True)
def load_test_settings(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(SETTINGS_YAML)
    import config

    config._reset_for_testing()
    config.load_settings(cfg)

    from tools import set_caller
    from tools.circuit_breaker import reset_all

    set_caller("test-agent")
    reset_all()
    yield
    from tools.circuit_breaker import reset_all as ra

    ra()
    config._reset_for_testing()


@pytest.fixture(autouse=True)
def _clear_metric_buffer():
    """Clear the telemetry buffer before and after each test.

    Prevents rows accumulated in one test from leaking into the next when
    _write_metric is running live (i.e. _suppress_telemetry_writes is not
    active).
    """
    import tools

    with tools._buffer_lock:
        tools._metric_buffer.clear()
    yield
    with tools._buffer_lock:
        tools._metric_buffer.clear()


# ── record_api_call context manager ──────────────────────────────────────────


class TestRecordApiCallContextManager:
    def test_happy_path_writes_success_row(self):
        """Clean exit from record_api_call writes a success row to BQ."""
        from tools import flush_metric_buffer, record_api_call

        with patch("tools.bigquery.insert_rows") as mock_batch:
            with record_api_call("gmail", "send_email", "agent-x", "proj-1"):
                pass
            flush_metric_buffer()

        mock_batch.assert_called_once()
        row = mock_batch.call_args[0][1][0]  # first row of the batch
        assert row["api_name"] == "gmail"
        assert row["operation"] == "send_email"
        assert row["caller"] == "agent-x"
        assert row["project_id"] == "proj-1"
        assert row["success"] is True
        assert isinstance(row["latency_ms"], int)
        assert row["latency_ms"] >= 0

    def test_exception_writes_failure_row_and_reraises(self):
        """Exception inside context manager → success=False row, exception re-raised."""
        from tools import flush_metric_buffer, record_api_call

        with patch("tools.bigquery.insert_rows") as mock_batch:
            with pytest.raises(ValueError, match="boom"):
                with record_api_call("bigquery", "insert_row", "agent-x", "proj-1"):
                    raise ValueError("boom")
            flush_metric_buffer()

        mock_batch.assert_called_once()
        row = mock_batch.call_args[0][1][0]
        assert row["success"] is False
        assert row["error_code"] == "ValueError"

    def test_latency_is_captured(self):
        """latency_ms reflects actual call duration."""
        from tools import flush_metric_buffer, record_api_call

        with patch("tools.bigquery.insert_rows") as mock_batch:
            with record_api_call("pubsub", "publish", "agent-x", "proj-1"):
                time.sleep(0.015)
            flush_metric_buffer()

        row = mock_batch.call_args[0][1][0]
        assert row["latency_ms"] >= 10

    def test_tokens_used_and_model_flow_through_ctx(self):
        """Caller can set tokens_used and model in the yielded ctx dict."""
        from tools import flush_metric_buffer, record_api_call

        with patch("tools.bigquery.insert_rows") as mock_batch:
            with record_api_call("gemini", "_call_model", "nexus-prime", "proj-1") as ctx:
                ctx["tokens_used"] = 1234
                ctx["model"] = "gemini-2.5-flash"
            flush_metric_buffer()

        row = mock_batch.call_args[0][1][0]
        assert row["tokens_used"] == 1234
        assert row["model"] == "gemini-2.5-flash"

    def test_bq_write_failure_is_swallowed(self):
        """A BQ error during metric flush must not propagate to the caller."""
        from tools import flush_metric_buffer, record_api_call

        with patch("tools.bigquery.insert_rows", side_effect=RuntimeError("bq down")):
            with record_api_call("secrets", "get_secret", "agent-x", "proj-1"):
                pass
            flush_metric_buffer()  # Must not raise — flush is best-effort.


# ── Recursion guard ───────────────────────────────────────────────────────────


class TestRecursionGuard:
    def test_record_api_call_inside_write_metric_does_not_recurse(self):
        """insert_rows is @tracked; the recursion guard prevents a second buffer append on flush."""
        import tools as tools_module
        from tools import flush_metric_buffer, record_api_call

        batch_call_count = 0

        def fake_insert_rows(table, rows, project_id="", row_ids=None):
            nonlocal batch_call_count
            batch_call_count += 1

        with patch("tools.bigquery.insert_rows", side_effect=fake_insert_rows):
            with record_api_call("bigquery", "insert_row", "agent-x", "proj-1"):
                pass
            flush_metric_buffer()

        # flush_metric_buffer() calls insert_rows once.
        # The @tracked wrapper on insert_rows sees in_metrics_write=True and skips
        # enqueuing a second row, preventing infinite recursion.
        assert batch_call_count == 1
        with tools_module._buffer_lock:
            assert len(tools_module._metric_buffer) == 0


# ── @tracked decorator ────────────────────────────────────────────────────────


class TestTrackedDecorator:
    def test_tracked_wraps_function_and_writes_row(self):
        """@tracked instruments the decorated function with correct api_name + project_id."""
        from tools import flush_metric_buffer, tracked

        @tracked("gmail")
        def send_email(to: str, project_id: str) -> str:
            return "ok"

        with patch("tools.bigquery.insert_rows") as mock_batch:
            result = send_email("test@example.com", project_id="my-project")
            flush_metric_buffer()

        assert result == "ok"
        mock_batch.assert_called_once()
        row = mock_batch.call_args[0][1][0]
        assert row["api_name"] == "gmail"
        assert row["operation"] == "send_email"
        assert row["project_id"] == "my-project"

    def test_tracked_is_noop_inside_metrics_write(self):
        """@tracked skips instrumentation when the recursion guard is set."""
        import tools as tools_module
        from tools import tracked

        @tracked("bigquery")
        def inner_func(project_id: str) -> str:
            return "inner"

        write_count = 0

        def fake_insert(table, row):
            nonlocal write_count
            write_count += 1

        tools_module._tls.in_metrics_write = True
        try:
            with patch("tools.bigquery.insert_row", side_effect=fake_insert):
                result = inner_func(project_id="any")
        finally:
            tools_module._tls.in_metrics_write = False

        assert result == "inner"
        assert write_count == 0  # Guard suppressed the write.

    def test_tracked_preserves_return_value_and_signature(self):
        """`@tracked` is transparent — return value, exceptions, and call semantics preserved."""
        from tools import tracked

        @tracked("vertex_search")
        def search(query: str, project_id: str, top_k: int = 5) -> list:
            return [query] * top_k

        # No BQ patch needed — buffering means no immediate BQ call; _clear_metric_buffer
        # fixture drains any accumulated rows after the test.
        result = search("loyalty", project_id="p", top_k=3)

        assert result == ["loyalty", "loyalty", "loyalty"]


# ── Circuit breaker BQ event writes ──────────────────────────────────────────


class TestCircuitBreakerBqEvents:
    def test_open_transition_writes_cb_event(self):
        """Tripping a circuit to OPEN writes one circuit_breaker_events row."""
        from tools.circuit_breaker import record_failure, reset_all

        reset_all()
        with patch("tools.bigquery.insert_row") as mock_insert:
            for _ in range(3):
                record_failure("test-agent", "gemini", failure_threshold=3)

        cb_calls = [
            c for c in mock_insert.call_args_list if c[0][0] == "aos_logs.circuit_breaker_events"
        ]
        assert len(cb_calls) == 1
        row = cb_calls[0][0][1]
        assert row["new_state"] == "OPEN"
        assert row["old_state"] in ("CLOSED", "HALF_OPEN")
        assert row["agent_id"] == "test-agent"
        assert row["resource_key"] == "gemini"

    def test_no_event_on_incremental_failures_below_threshold(self):
        """Failures below the threshold do NOT write a CB event."""
        from tools.circuit_breaker import record_failure, reset_all

        reset_all()
        with patch("tools.bigquery.insert_row") as mock_insert:
            record_failure("test-agent", "drive", failure_threshold=5)
            record_failure("test-agent", "drive", failure_threshold=5)

        cb_calls = [
            c for c in mock_insert.call_args_list if c[0][0] == "aos_logs.circuit_breaker_events"
        ]
        assert len(cb_calls) == 0

    def test_recovery_writes_cb_event(self):
        """OPEN → HALF_OPEN → success writes a CLOSED event."""
        from tools.circuit_breaker import check, record_failure, record_success, reset_all

        reset_all()
        # Trip the circuit with instant cooldown
        for _ in range(3):
            record_failure("test-agent", "sheets", failure_threshold=3, cooldown_seconds=0)

        time.sleep(0.01)  # let cooldown elapse
        check("test-agent", "sheets", cooldown_seconds=0)  # → HALF_OPEN, probe allowed

        with patch("tools.bigquery.insert_row") as mock_insert:
            record_success("test-agent", "sheets")

        cb_calls = [
            c for c in mock_insert.call_args_list if c[0][0] == "aos_logs.circuit_breaker_events"
        ]
        assert len(cb_calls) == 1
        row = cb_calls[0][0][1]
        assert row["new_state"] == "CLOSED"
        assert row["old_state"] == "HALF_OPEN"
