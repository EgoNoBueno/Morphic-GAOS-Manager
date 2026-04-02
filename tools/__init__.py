"""
tools/__init__.py — Shared tool modules for Morphic-G AOS.

All agents use these modules as the sole interface to Google services.
No agent may call the underlying Google SDKs directly.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import logging
import threading
import time
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from typing import Any

_logger = logging.getLogger(__name__)

# ── Recursion guard ──────────────────────────────────────────────────────────
# record_api_call writes to BQ via insert_row. insert_row is itself
# instrumented. The thread-local flag prevents infinite recursion.
_tls = threading.local()


def _is_metrics_write() -> bool:
    return getattr(_tls, "in_metrics_write", False)


def set_caller(agent_id: str) -> None:
    """Set the current agent_id for API metrics tracking.

    Called once during orchestrator boot. The value persists for the
    lifetime of the thread (Cloud Run worker=1, so one thread per request).
    """
    _tls.current_caller = agent_id


def get_caller() -> str:
    """Return the agent_id set by :func:`set_caller`, or ``"unknown"``."""
    return getattr(_tls, "current_caller", "unknown")


# ── Public API ────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def record_api_call(
    api_name: str,
    operation: str,
    caller: str,
    project_id: str,
    *,
    tokens_used: int | None = None,
    model: str | None = None,
    attempts: int = 1,
) -> Generator[dict[str, Any], None, None]:
    """Context manager that records API call telemetry to BigQuery.

    Usage::

        with record_api_call("gmail", "send_email", agent_id, project_id) as ctx:
            result = gmail_service.users().messages().send(...)
        # on exit: success=True, latency captured, row written to BQ

    To report extra fields after the call completes, mutate the yielded dict::

        with record_api_call("gemini", "generate", agent_id, pid) as ctx:
            resp = _call_model_gemini(...)
            ctx["tokens_used"] = resp.tokens_used
            ctx["model"] = "gemini-2.5-flash"

    Args:
        api_name:    Short identifier for the API (e.g. "gmail", "bigquery").
        operation:   Specific operation (e.g. "send_email", "insert_row").
        caller:      The agent_id making the call.
        project_id:  GCP project scope.
        tokens_used: LLM token count (optional, set after call via ctx dict).
        model:       LLM model name (optional, set after call via ctx dict).
        attempts:    Number of attempts including retries (default 1).

    Yields:
        A mutable dict that the caller can update with tokens_used, model,
        attempts, or error_code before the context manager writes the row.
    """
    ctx: dict[str, Any] = {
        "tokens_used": tokens_used,
        "model": model,
        "attempts": attempts,
        "error_code": None,
    }
    t0 = time.perf_counter()
    success = True
    try:
        yield ctx
    except Exception as exc:
        success = False
        ctx["error_code"] = _extract_error_code(exc)
        raise
    finally:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        if not _is_metrics_write():
            _write_metric(
                api_name=api_name,
                operation=operation,
                caller=caller,
                project_id=project_id,
                success=success,
                latency_ms=latency_ms,
                error_code=ctx.get("error_code"),
                attempts=ctx.get("attempts", 1),
                tokens_used=ctx.get("tokens_used"),
                model=ctx.get("model"),
            )


def _extract_error_code(exc: Exception) -> str:
    """Best-effort error code extraction from common exception types."""
    # google-api-core / googleapiclient HTTP errors
    for attr in ("code", "status_code", "resp"):
        val = getattr(exc, attr, None)
        if val is not None:
            if hasattr(val, "status"):
                return str(val.status)
            if isinstance(val, int):
                return str(val)
    # httpx
    if hasattr(exc, "response") and hasattr(exc.response, "status_code"):
        return str(exc.response.status_code)
    # Timeout variants
    type_name = type(exc).__name__.lower()
    if "timeout" in type_name:
        return "timeout"
    return type(exc).__name__


def _write_metric(
    api_name: str,
    operation: str,
    caller: str,
    project_id: str,
    success: bool,
    latency_ms: int,
    error_code: str | None,
    attempts: int,
    tokens_used: int | None,
    model: str | None,
) -> None:
    """Best-effort write of one telemetry row to BigQuery."""
    _tls.in_metrics_write = True
    try:
        from tools.bigquery import insert_row

        insert_row(
            "aos_logs.api_call_log",
            {
                "ts": datetime.now(UTC).isoformat(),
                "api_name": api_name,
                "operation": operation,
                "caller": caller,
                "project_id": project_id,
                "success": success,
                "latency_ms": latency_ms,
                "error_code": error_code,
                "attempts": attempts,
                "tokens_used": tokens_used,
                "model": model,
            },
        )
    except Exception as exc:
        _logger.warning("API metrics write failed (non-fatal): %s", exc)
    finally:
        _tls.in_metrics_write = False


# ── Decorator ─────────────────────────────────────────────────────────────────


def tracked(api_name: str) -> Callable[..., Any]:
    """Decorator that instruments a public tool function with API telemetry.

    Extracts ``project_id`` from the decorated function's arguments (if present)
    and reads the caller from the thread-local set by :func:`set_caller`.

    Usage::

        @tracked("gmail")
        def send_email(project_id: str, to: str, subject: str, ...) -> str:
            ...

    The decorator is transparent to callers — signature, docstring, and return
    value are preserved.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if _is_metrics_write():
                return fn(*args, **kwargs)
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            pid = str(bound.arguments.get("project_id", ""))
            caller = get_caller()
            with record_api_call(api_name, fn.__name__, caller, pid):
                return fn(*args, **kwargs)

        return wrapper

    return decorator
