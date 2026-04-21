"""
tests/test_start_ollama_tunnel.py — Unit tests for scripts/start_ollama_tunnel.py.

All external calls (Gmail, Secret Manager, get_settings) are mocked.

Tests:
  _send_tunnel_alert()
    test_send_tunnel_alert_calls_send_email
    test_send_tunnel_alert_subject_contains_subdomain_and_count
    test_send_tunnel_alert_body_contains_remediation_steps
    test_send_tunnel_alert_no_op_if_send_email_raises
    test_send_tunnel_alert_no_op_if_get_settings_raises
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _load_tunnel_script() -> ModuleType:
    """Load start_ollama_tunnel.py without executing its __main__ block."""
    spec = importlib.util.spec_from_file_location(
        "start_ollama_tunnel",
        Path(__file__).parent.parent / "scripts" / "start_ollama_tunnel.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Load once for the module — the script has no module-level GCP calls.
_tunnel = _load_tunnel_script()


# ---------------------------------------------------------------------------
# _send_tunnel_alert tests
# ---------------------------------------------------------------------------


class TestSendTunnelAlert:
    """Tests for _send_tunnel_alert() — email dispatch on repeated failures."""

    def _make_settings(self) -> MagicMock:
        s = MagicMock()
        s.gmail.alert_address = "admin@example.com"
        s.gmail.sender_address = "gaos@example.com"
        return s

    def test_send_tunnel_alert_calls_send_email(self):
        """Happy path: alert triggers send_email with correct project_id and to address."""
        mock_settings = self._make_settings()
        mock_send = MagicMock(return_value="msg-id-123")

        with (
            patch("config.get_settings", return_value=mock_settings),
            patch("tools.gmail.send_email", mock_send),
        ):
            _tunnel._send_tunnel_alert("morphic-gaos-prod", 5, "gaos-ollama")

        mock_send.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        assert kwargs["project_id"] == "morphic-gaos-prod"
        assert kwargs["to"] == "admin@example.com"
        assert kwargs["from_addr"] == "gaos@example.com"

    def test_send_tunnel_alert_subject_contains_subdomain_and_count(self):
        """Subject must name the subdomain and failure count so the alert is actionable."""
        mock_settings = self._make_settings()
        mock_send = MagicMock(return_value="x")

        with (
            patch("config.get_settings", return_value=mock_settings),
            patch("tools.gmail.send_email", mock_send),
        ):
            _tunnel._send_tunnel_alert("morphic-gaos-prod", 7, "gaos-ollama")

        subject = mock_send.call_args.kwargs["subject"]
        assert "gaos-ollama" in subject
        assert "7" in subject

    def test_send_tunnel_alert_body_contains_remediation_steps(self):
        """Body must mention Task Scheduler so the reader knows how to fix it."""
        mock_settings = self._make_settings()
        mock_send = MagicMock(return_value="x")

        with (
            patch("config.get_settings", return_value=mock_settings),
            patch("tools.gmail.send_email", mock_send),
        ):
            _tunnel._send_tunnel_alert("morphic-gaos-prod", 5, "gaos-ollama")

        body = mock_send.call_args.kwargs["body"]
        assert "Task Scheduler" in body or "start_ollama_tunnel" in body

    def test_send_tunnel_alert_no_op_if_send_email_raises(self):
        """A broken email tool must not crash the watchdog loop."""
        mock_settings = self._make_settings()

        with (
            patch("config.get_settings", return_value=mock_settings),
            patch("tools.gmail.send_email", side_effect=RuntimeError("smtp down")),
        ):
            # Must not raise
            _tunnel._send_tunnel_alert("morphic-gaos-prod", 5, "gaos-ollama")

    def test_send_tunnel_alert_no_op_if_get_settings_raises(self):
        """If settings can't load (e.g., no config file), the watchdog keeps running."""
        with patch("config.get_settings", side_effect=FileNotFoundError("no config")):
            # Must not raise
            _tunnel._send_tunnel_alert("morphic-gaos-prod", 5, "gaos-ollama")


# ---------------------------------------------------------------------------
# _run_tunnel_once return-value tests
# ---------------------------------------------------------------------------


class TestRunTunnelOnceReturnValue:
    """Verify _run_tunnel_once returns False on health-kill and True on natural exit.

    These tests exercise the ``health_killed`` threading.Event introduced to
    distinguish flapping (health-thread kill → False) from a genuine clean
    process death (True) so the watchdog's consecutive_failures counter
    accumulates correctly in crash-loop scenarios.
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_proc(self, *, lines: list[str], returncode: int = 1) -> MagicMock:
        """Build a fake Popen object whose stdout yields *lines* then stops."""
        proc = MagicMock()
        proc.pid = 99999
        proc.returncode = returncode
        proc.stdout = iter(lines)
        proc.wait = MagicMock(return_value=returncode)
        return proc

    # ------------------------------------------------------------------
    # natural exit → True
    # ------------------------------------------------------------------

    def test_natural_exit_returns_true(self):
        """When the tunnel process exits on its own (not health-killed), return True."""
        import subprocess
        import threading

        url = "https://gaos-ollama.loca.lt"
        proc = self._make_proc(lines=[f"your url is: {url}\n", "some other line\n"])

        with (
            patch.object(_tunnel, "subprocess", create=True) as mock_sub,
            patch.object(_tunnel, "_verify_tunnel", return_value=True),
            patch.object(_tunnel, "_current_secret_url", return_value=""),
            patch.object(_tunnel, "_update_secret"),
            patch.object(_tunnel, "_kill_tree"),
        ):
            mock_sub.Popen.return_value = proc
            mock_sub.PIPE = subprocess.PIPE
            mock_sub.STDOUT = subprocess.STDOUT

            result = _tunnel._run_tunnel_once(
                ["npx", "localtunnel", "--port", "11434", "--subdomain", "gaos-ollama"],
                no_secret=True,
                project="morphic-gaos-prod",
                health_interval=9999.0,  # prevent health thread from firing
            )

        assert result is True, "Natural process exit should return True"

    # ------------------------------------------------------------------
    # health-kill → False
    # ------------------------------------------------------------------

    def test_health_kill_returns_false(self):
        """When the health thread kills the process, _run_tunnel_once must return False."""
        import subprocess
        import threading
        import time

        url = "https://gaos-ollama.loca.lt"

        # A fake proc whose stdout blocks until we release it — simulates a
        # long-lived tunnel that gets killed by the health thread.
        kill_called = threading.Event()
        stdout_released = threading.Event()

        class _BlockingStdout:
            """Yields the URL line, then blocks until the process is 'killed'."""

            def __iter__(self):
                yield f"your url is: {url}\n"
                # Block until _kill_tree is called (simulates process death)
                stdout_released.wait(timeout=10)

        proc = MagicMock()
        proc.pid = 99999
        proc.returncode = -9
        proc.stdout = _BlockingStdout()
        proc.wait = MagicMock(return_value=-9)

        def _fake_kill_tree(pid: int) -> None:
            kill_called.set()
            stdout_released.set()  # unblock the stdout iterator

        with (
            patch.object(_tunnel, "subprocess", create=True) as mock_sub,
            patch.object(_tunnel, "_verify_tunnel", return_value=True),
            patch.object(_tunnel, "_current_secret_url", return_value=""),
            patch.object(_tunnel, "_update_secret"),
            patch.object(_tunnel, "_kill_tree", side_effect=_fake_kill_tree),
            patch.object(_tunnel, "httpx") as mock_httpx,
        ):
            mock_sub.Popen.return_value = proc
            mock_sub.PIPE = subprocess.PIPE
            mock_sub.STDOUT = subprocess.STDOUT

            # Make every health-check fail immediately
            mock_httpx.get.side_effect = ConnectionError("tunnel down")

            result = _tunnel._run_tunnel_once(
                ["npx", "localtunnel", "--port", "11434", "--subdomain", "gaos-ollama"],
                no_secret=True,
                project="morphic-gaos-prod",
                health_interval=0.05,  # fire health checks quickly in the test
            )

        assert result is False, "Health-thread kill should return False"
