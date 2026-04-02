"""tests/test_circuit_breaker.py — Unit tests for tools/circuit_breaker.py"""

import time
from unittest.mock import patch

import pytest

from tools.circuit_breaker import (
    CircuitOpenError,
    CircuitState,
    check,
    get_state,
    record_failure,
    record_success,
    reset,
    reset_all,
)

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

_AGENT = "nexus-prime"
_RESOURCE = "bigquery"


@pytest.fixture(autouse=True)
def load_test_settings(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(SETTINGS_YAML)
    import config

    config._reset_for_testing()
    config.load_settings(cfg)
    reset_all()
    yield
    reset_all()
    config._reset_for_testing()


@pytest.fixture(autouse=True)
def suppress_bq_writes():
    """Prevent _write_cb_event and @tracked from making real BQ calls.

    Circuit breaker tests use very short cooldown windows (0.01 s). Without
    this patch, real network calls inside _write_cb_event can exceed the
    cooldown before check() is called, causing timing-sensitive tests to fail.
    BQ write behaviour is covered in tests/test_api_metrics.py.
    """
    with patch("tools.circuit_breaker._write_cb_event"), patch("tools._write_metric"):
        yield


# ── Happy path ────────────────────────────────────────────────────────────────


class TestCircuitBreakerHappyPath:
    def test_check_passes_when_circuit_has_no_history(self):
        check(_AGENT, _RESOURCE)  # Must not raise.

    def test_state_is_none_before_first_interaction(self):
        assert get_state(_AGENT, _RESOURCE) is None

    def test_single_failure_does_not_open_circuit_at_default_threshold(self):
        record_failure(_AGENT, _RESOURCE)
        assert get_state(_AGENT, _RESOURCE) is CircuitState.CLOSED
        check(_AGENT, _RESOURCE)  # Must not raise.

    def test_two_failures_below_default_threshold_do_not_open_circuit(self):
        record_failure(_AGENT, _RESOURCE)
        record_failure(_AGENT, _RESOURCE)
        assert get_state(_AGENT, _RESOURCE) is CircuitState.CLOSED

    def test_record_success_resets_failure_count_and_closes_circuit(self):
        record_failure(_AGENT, _RESOURCE, failure_threshold=1)
        assert get_state(_AGENT, _RESOURCE) is CircuitState.OPEN
        record_success(_AGENT, _RESOURCE)
        assert get_state(_AGENT, _RESOURCE) is CircuitState.CLOSED

    def test_record_success_on_unknown_key_is_noop(self):
        record_success("ghost-agent", "ghost-resource")  # Must not raise.


# ── Circuit trip ──────────────────────────────────────────────────────────────


class TestCircuitTrip:
    def test_circuit_opens_at_exactly_threshold_failures(self):
        for _ in range(3):
            record_failure(_AGENT, _RESOURCE, failure_threshold=3)
        assert get_state(_AGENT, _RESOURCE) is CircuitState.OPEN

    def test_check_raises_circuit_open_error_when_open(self):
        for _ in range(3):
            record_failure(_AGENT, _RESOURCE, failure_threshold=3)
        with pytest.raises(CircuitOpenError, match="Circuit OPEN"):
            check(_AGENT, _RESOURCE)

    def test_circuit_open_error_message_contains_resource_key(self):
        record_failure(_AGENT, _RESOURCE, failure_threshold=1)
        with pytest.raises(CircuitOpenError, match=_RESOURCE):
            check(_AGENT, _RESOURCE)

    def test_record_failure_returns_open_when_threshold_reached(self):
        result = record_failure(_AGENT, _RESOURCE, failure_threshold=1)
        assert result is CircuitState.OPEN

    def test_record_failure_returns_closed_below_threshold(self):
        result = record_failure(_AGENT, _RESOURCE, failure_threshold=10)
        assert result is CircuitState.CLOSED

    def test_failures_beyond_threshold_keep_circuit_open(self):
        for _ in range(10):
            record_failure(_AGENT, _RESOURCE, failure_threshold=3)
        assert get_state(_AGENT, _RESOURCE) is CircuitState.OPEN


# ── Cooldown / half-open ──────────────────────────────────────────────────────


class TestCooldownTransition:
    def test_circuit_transitions_to_half_open_after_cooldown_elapses(self):
        for _ in range(3):
            record_failure(_AGENT, _RESOURCE, failure_threshold=3, cooldown_seconds=0.01)
        assert get_state(_AGENT, _RESOURCE) is CircuitState.OPEN
        time.sleep(0.02)
        # check() must NOT raise after cooldown; it should move to HALF_OPEN.
        check(_AGENT, _RESOURCE, cooldown_seconds=0.01)
        assert get_state(_AGENT, _RESOURCE) is CircuitState.HALF_OPEN

    def test_failure_in_half_open_reopens_circuit(self):
        for _ in range(3):
            record_failure(_AGENT, _RESOURCE, failure_threshold=3, cooldown_seconds=0.01)
        time.sleep(0.02)
        check(_AGENT, _RESOURCE, cooldown_seconds=0.01)  # → HALF_OPEN
        record_failure(_AGENT, _RESOURCE, cooldown_seconds=0.01)
        assert get_state(_AGENT, _RESOURCE) is CircuitState.OPEN

    def test_success_in_half_open_closes_circuit(self):
        for _ in range(3):
            record_failure(_AGENT, _RESOURCE, failure_threshold=3, cooldown_seconds=0.01)
        time.sleep(0.02)
        check(_AGENT, _RESOURCE, cooldown_seconds=0.01)  # → HALF_OPEN
        record_success(_AGENT, _RESOURCE)
        assert get_state(_AGENT, _RESOURCE) is CircuitState.CLOSED

    def test_check_still_raises_before_cooldown_elapses(self):
        for _ in range(3):
            record_failure(_AGENT, _RESOURCE, failure_threshold=3, cooldown_seconds=60)
        with pytest.raises(CircuitOpenError):
            check(_AGENT, _RESOURCE, cooldown_seconds=60)

    def test_second_concurrent_check_rejected_while_probe_in_flight(self):
        """Only ONE caller gets through HALF_OPEN; the rest must get CircuitOpenError."""
        for _ in range(3):
            record_failure(_AGENT, _RESOURCE, failure_threshold=3, cooldown_seconds=0.01)
        time.sleep(0.02)
        # First check: OPEN → HALF_OPEN, probe claimed.
        check(_AGENT, _RESOURCE, cooldown_seconds=0.01)
        # Second check while probe is in flight must be rejected.
        with pytest.raises(CircuitOpenError, match="probe already in flight"):
            check(_AGENT, _RESOURCE, cooldown_seconds=0.01)

    def test_probe_slot_released_on_record_failure(self):
        """After record_failure clears probe_in_flight, cooldown starts fresh (OPEN).
        A new probe must be admissible once the new cooldown elapses."""
        for _ in range(3):
            record_failure(_AGENT, _RESOURCE, failure_threshold=3, cooldown_seconds=0.01)
        time.sleep(0.02)
        check(_AGENT, _RESOURCE, cooldown_seconds=0.01)  # → HALF_OPEN, probe claimed
        record_failure(_AGENT, _RESOURCE, cooldown_seconds=0.01)  # probe released, → OPEN
        # Circuit is OPEN again — check must raise while the new cooldown is active.
        with pytest.raises(CircuitOpenError):
            check(_AGENT, _RESOURCE, cooldown_seconds=60)
        # Sleep past the new cooldown; the probe slot must be free so a fresh
        # probe is admitted (HALF_OPEN).  If probe_in_flight were never cleared
        # this call would raise "probe already in flight".
        time.sleep(0.02)
        check(_AGENT, _RESOURCE, cooldown_seconds=0.01)  # must not raise
        assert get_state(_AGENT, _RESOURCE) is CircuitState.HALF_OPEN
        # A second concurrent call confirms exactly one probe slot is now occupied.
        with pytest.raises(CircuitOpenError, match="probe already in flight"):
            check(_AGENT, _RESOURCE, cooldown_seconds=0.01)


# ── Reset ─────────────────────────────────────────────────────────────────────


class TestReset:
    def test_reset_removes_breaker_entry(self):
        record_failure(_AGENT, _RESOURCE, failure_threshold=1)
        reset(_AGENT, _RESOURCE)
        assert get_state(_AGENT, _RESOURCE) is None

    def test_reset_nonexistent_key_is_noop(self):
        reset("unknown-agent", "unknown-resource")  # Must not raise.

    def test_reset_all_clears_multiple_breakers(self):
        record_failure(_AGENT, "res-a", failure_threshold=1)
        record_failure(_AGENT, "res-b", failure_threshold=1)
        reset_all()
        assert get_state(_AGENT, "res-a") is None
        assert get_state(_AGENT, "res-b") is None

    def test_check_works_normally_after_reset(self):
        for _ in range(3):
            record_failure(_AGENT, _RESOURCE, failure_threshold=3)
        reset(_AGENT, _RESOURCE)
        check(_AGENT, _RESOURCE)  # Must not raise.


# ── Isolation ─────────────────────────────────────────────────────────────────


class TestIsolation:
    def test_different_agents_have_independent_circuits(self):
        for _ in range(3):
            record_failure("agent-a", _RESOURCE, failure_threshold=3)
        # agent-b is unaffected — check() must not raise.
        check("agent-b", _RESOURCE)
        assert get_state("agent-b", _RESOURCE) is not CircuitState.OPEN

    def test_different_resources_have_independent_circuits(self):
        for _ in range(3):
            record_failure(_AGENT, "res-x", failure_threshold=3)
        check(_AGENT, "res-y")  # Must not raise.
        assert get_state(_AGENT, "res-y") is not CircuitState.OPEN
