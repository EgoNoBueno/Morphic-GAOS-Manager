"""
tools/circuit_breaker.py — Thread-safe circuit breaker for Morphic-GAOS tool calls.

Prevents agents from hammering failed external dependencies by tracking consecutive
failures per (agent_id, resource_key) and temporarily disabling the call path after
a configurable threshold is breached.

States:
  CLOSED    — Normal operation. Calls are permitted.
  OPEN      — Failure threshold breached. Calls are rejected immediately.
  HALF_OPEN — Cooldown has elapsed. One probe call is permitted to test recovery.

Spec: Phoenix/Circuit Breaker pattern — §8.3.3, OpenClaw Paradigm Book ch. 8.
"""

from __future__ import annotations

import threading
import time
from enum import Enum, auto

# ── Constants ────────────────────────────────────────────────────────────────

_DEFAULT_FAILURE_THRESHOLD: int = 3
_DEFAULT_COOLDOWN_SECONDS: float = 300.0


# ── Public types ─────────────────────────────────────────────────────────────


class CircuitState(Enum):
    """The three possible states of a circuit breaker."""

    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()


class CircuitOpenError(Exception):
    """Raised when a call is attempted while the circuit is OPEN."""


# ── Internal state ────────────────────────────────────────────────────────────


class _Breaker:
    """Internal state for one (agent_id, resource_key) circuit."""

    __slots__ = (
        "state",
        "failure_count",
        "last_failure_ts",
        "failure_threshold",
        "cooldown_seconds",
        "probe_in_flight",
    )

    def __init__(self, failure_threshold: int, cooldown_seconds: float) -> None:
        self.state: CircuitState = CircuitState.CLOSED
        self.failure_count: int = 0
        self.last_failure_ts: float = 0.0
        self.failure_threshold: int = failure_threshold
        self.cooldown_seconds: float = cooldown_seconds
        self.probe_in_flight: bool = False


_breakers: dict[tuple[str, str], _Breaker] = {}
_lock = threading.Lock()


def _get_or_create(
    agent_id: str,
    resource_key: str,
    failure_threshold: int,
    cooldown_seconds: float,
) -> _Breaker:
    key = (agent_id, resource_key)
    with _lock:
        if key not in _breakers:
            _breakers[key] = _Breaker(failure_threshold, cooldown_seconds)
        return _breakers[key]


def _write_cb_event(
    agent_id: str,
    resource_key: str,
    old_state: CircuitState,
    new_state: CircuitState,
) -> None:
    """Best-effort BQ write on a circuit breaker state transition. Never raises."""
    try:
        from datetime import UTC, datetime

        from tools.bigquery import insert_row

        insert_row(
            "aos_logs.circuit_breaker_events",
            {
                "ts": datetime.now(UTC).isoformat(),
                "agent_id": agent_id,
                "resource_key": resource_key,
                "old_state": old_state.name,
                "new_state": new_state.name,
            },
        )
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning("CB event write failed (non-fatal): %s", exc)


# ── Public API ────────────────────────────────────────────────────────────────


def check(
    agent_id: str,
    resource_key: str,
    failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
    cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
) -> None:
    """
    Check whether the circuit is open. Raises CircuitOpenError if blocked.

    Call this before attempting a tool call that may fail. Pair with
    record_success() on success and record_failure() on failure.

    Args:
        agent_id:          The calling agent's identifier.
        resource_key:      A string identifying the resource (e.g., "bigquery", "gemini-api").
        failure_threshold: Consecutive failures before the circuit opens. Default: 3.
        cooldown_seconds:  Seconds to keep the circuit open before allowing a probe. Default: 300.

    Raises:
        CircuitOpenError: The circuit is OPEN and the cooldown has not elapsed.
    """
    breaker = _get_or_create(agent_id, resource_key, failure_threshold, cooldown_seconds)
    with _lock:
        if breaker.state is CircuitState.OPEN:
            elapsed = time.monotonic() - breaker.last_failure_ts
            if elapsed >= breaker.cooldown_seconds:
                breaker.state = CircuitState.HALF_OPEN
                breaker.probe_in_flight = True
            else:
                remaining = round(breaker.cooldown_seconds - elapsed)
                raise CircuitOpenError(
                    f"Circuit OPEN for {agent_id}/{resource_key}. Cooldown: {remaining}s remaining."
                )
        elif breaker.state is CircuitState.HALF_OPEN and breaker.probe_in_flight:
            raise CircuitOpenError(
                f"Circuit HALF_OPEN for {agent_id}/{resource_key}: probe already in flight."
            )


def record_success(agent_id: str, resource_key: str) -> None:
    """
    Record a successful call, resetting the failure count and closing the circuit.

    Args:
        agent_id:     The calling agent's identifier.
        resource_key: The resource that succeeded.
    """
    key = (agent_id, resource_key)
    old_state: CircuitState | None = None
    with _lock:
        breaker = _breakers.get(key)
        if breaker is not None:
            old_state = breaker.state
            breaker.state = CircuitState.CLOSED
            breaker.failure_count = 0
            breaker.probe_in_flight = False
    if old_state is not None and old_state is not CircuitState.CLOSED:
        _write_cb_event(agent_id, resource_key, old_state, CircuitState.CLOSED)


def record_failure(
    agent_id: str,
    resource_key: str,
    failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
    cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
) -> CircuitState:
    """
    Record a failed call. Returns the resulting CircuitState.

    If failures reach the threshold, the circuit transitions to OPEN.

    Args:
        agent_id:          The calling agent's identifier.
        resource_key:      The resource that failed.
        failure_threshold: Consecutive failures before the circuit opens. Default: 3.
        cooldown_seconds:  Cooldown after circuit opens. Default: 300.

    Returns:
        The new CircuitState after recording the failure.
    """
    breaker = _get_or_create(agent_id, resource_key, failure_threshold, cooldown_seconds)
    with _lock:
        old_state = breaker.state
        breaker.failure_count += 1
        breaker.last_failure_ts = time.monotonic()
        breaker.probe_in_flight = False
        if (
            breaker.state is CircuitState.HALF_OPEN
            or breaker.failure_count >= breaker.failure_threshold
        ):
            breaker.state = CircuitState.OPEN
        new_state = breaker.state
    if old_state is not CircuitState.OPEN and new_state is CircuitState.OPEN:
        _write_cb_event(agent_id, resource_key, old_state, new_state)
    return new_state


def get_state(agent_id: str, resource_key: str) -> CircuitState | None:
    """
    Return the current CircuitState for a (agent_id, resource_key) pair.

    Args:
        agent_id:     The calling agent's identifier.
        resource_key: The resource identifier.

    Returns:
        The current CircuitState, or None if no breaker exists for this pair yet.
    """
    key = (agent_id, resource_key)
    with _lock:
        breaker = _breakers.get(key)
        return breaker.state if breaker is not None else None


def reset(agent_id: str, resource_key: str) -> None:
    """
    Forcibly reset a circuit breaker to an unregistered state. Useful for manual recovery.

    Args:
        agent_id:     The calling agent's identifier.
        resource_key: The resource identifier.
    """
    key = (agent_id, resource_key)
    with _lock:
        _breakers.pop(key, None)


def reset_all() -> None:
    """Clear all circuit breaker state. Intended for use in tests only."""
    with _lock:
        _breakers.clear()
