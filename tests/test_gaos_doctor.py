"""
tests/test_gaos_doctor.py — Unit tests for scripts/gaos_doctor.py.

All GCP calls are mocked at the SDK boundary.  No live API calls are made.

Tests:
  Check 9 — Circuit Breaker States
    test_check_circuit_breakers_all_closed
    test_check_circuit_breakers_one_open
    test_check_circuit_breakers_table_not_found
    test_check_circuit_breakers_bq_error

  Check 10 — Cloud Scheduler Job Inventory
    test_check_scheduler_jobs_all_present_enabled
    test_check_scheduler_jobs_one_missing
    test_check_scheduler_jobs_one_paused
    test_check_scheduler_jobs_api_error

  Check 11 — Ollama Tunnel Reachability
    test_check_ollama_tunnel_200_ok
    test_check_ollama_tunnel_non_200_fails
    test_check_ollama_tunnel_connection_error_fails
    test_check_ollama_tunnel_secret_missing_fails
    test_check_ollama_tunnel_bypass_header_injected_for_loca_lt

  send_doctor_report() + _maybe_send_report()
    test_send_doctor_report_subject_contains_counts
    test_send_doctor_report_body_lists_fail_items
    test_maybe_send_report_no_env_var_skips_send
    test_maybe_send_report_email_failure_is_non_fatal
"""

from __future__ import annotations

from types import ModuleType
from unittest.mock import MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reload_doctor() -> ModuleType:
    """Re-import gaos_doctor with a clean ``results`` list each time."""
    # gaos_doctor lives in scripts/, which may not be on sys.path in tests
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "gaos_doctor",
        Path(__file__).parent.parent / "scripts" / "gaos_doctor.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    # Provide a minimal config stub so get_settings() doesn't hit disk
    with patch("config.get_settings"):
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    mod.results.clear()
    return mod


def _run_and_collect(fn) -> list[tuple[str, str, str]]:
    """Call fn(), return the results list from its module."""
    fn()
    # fn belongs to a doctor module instance; results is module-global
    import scripts.gaos_doctor as gd  # noqa: PLC0415 — intentional late import

    return list(gd.results)


# ---------------------------------------------------------------------------
# Fixtures — reset the module-level results list between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_results():
    """Clear gaos_doctor.results before and after every test."""
    import scripts.gaos_doctor as gd  # noqa: PLC0415

    gd.results.clear()
    yield
    gd.results.clear()


# ---------------------------------------------------------------------------
# Check 9: Circuit Breaker States
# ---------------------------------------------------------------------------


class TestCheckCircuitBreakers:
    """Tests for check_circuit_breakers() in gaos_doctor.py."""

    def _make_row(self, agent_id: str, resource_key: str, new_state: str) -> MagicMock:
        row = MagicMock()
        row.__getitem__ = lambda self, k: {
            "agent_id": agent_id,
            "resource_key": resource_key,
            "new_state": new_state,
        }[k]
        return row

    def test_check_circuit_breakers_all_closed(self):
        """All resources CLOSED → single OK result."""
        import scripts.gaos_doctor as gd

        rows = [
            self._make_row("nexus-prime", "bigquery", "CLOSED"),
            self._make_row("ledger", "sheets", "HALF_OPEN"),
        ]
        mock_client = MagicMock()
        mock_client.query.return_value.result.return_value = rows

        with patch("scripts.gaos_doctor._bq.Client", return_value=mock_client):
            gd.check_circuit_breakers()

        labels = [r[0] for r in gd.results]
        statuses = [r[1] for r in gd.results]
        assert any("Circuit breakers" in lbl for lbl in labels)
        assert all(s == "ok" for s in statuses)

    def test_check_circuit_breakers_one_open(self):
        """One OPEN resource → one WARN result (not FAIL)."""
        import scripts.gaos_doctor as gd

        rows = [
            self._make_row("nexus-prime", "bigquery", "CLOSED"),
            self._make_row("ledger", "vertex-ai", "OPEN"),
        ]
        mock_client = MagicMock()
        mock_client.query.return_value.result.return_value = rows

        with patch("scripts.gaos_doctor._bq.Client", return_value=mock_client):
            gd.check_circuit_breakers()

        warn_results = [r for r in gd.results if r[1] == "warn"]
        assert len(warn_results) == 1
        assert "ledger/vertex-ai" in warn_results[0][0]
        # Must be WARN not FAIL — open breaker is operational (cooldown in progress)
        fail_results = [r for r in gd.results if r[1] == "fail"]
        assert len(fail_results) == 0

    def test_check_circuit_breakers_table_not_found(self):
        """Table doesn't exist yet → graceful OK (not FAIL)."""
        import scripts.gaos_doctor as gd

        mock_client = MagicMock()
        mock_client.query.side_effect = Exception(
            "Not found: Table morphic-gaos-prod.aos_logs.circuit_breaker_events"
        )

        with patch("scripts.gaos_doctor._bq.Client", return_value=mock_client):
            gd.check_circuit_breakers()

        assert len(gd.results) == 1
        assert gd.results[0][1] == "ok"
        assert "not yet created" in gd.results[0][2]

    def test_check_circuit_breakers_bq_error(self):
        """Unexpected BQ error (not notFound) → FAIL."""
        import scripts.gaos_doctor as gd

        mock_client = MagicMock()
        mock_client.query.side_effect = Exception("Connection refused")

        with patch("scripts.gaos_doctor._bq.Client", return_value=mock_client):
            gd.check_circuit_breakers()

        assert len(gd.results) == 1
        assert gd.results[0][1] == "fail"


# ---------------------------------------------------------------------------
# Check 10: Cloud Scheduler Job Inventory
# ---------------------------------------------------------------------------


class TestCheckSchedulerJobs:
    """Tests for check_scheduler_jobs() in gaos_doctor.py."""

    _EXPECTED_JOBS = [
        "gaos-archive",
        "gaos-daily-sync",
        "gaos-sheets-sync",
        "gaos-gmail-renew-watch",
        "gaos-daily-digest",
        "gaos-doctor-daily",
    ]

    def _make_job(self, job_id: str, state: str = "ENABLED") -> dict:
        return {
            "name": f"projects/morphic-gaos-prod/locations/us-central1/jobs/{job_id}",
            "state": state,
        }

    def test_check_scheduler_jobs_all_present_enabled(self):
        """All 5 jobs present and ENABLED → 5 OK results, no failures."""
        import scripts.gaos_doctor as gd

        jobs = [self._make_job(j) for j in self._EXPECTED_JOBS]
        mock_scheduler = MagicMock()
        (mock_scheduler.projects().locations().jobs().list().execute.return_value) = {"jobs": jobs}

        with (
            patch("scripts.gaos_doctor.google.auth.default", return_value=(MagicMock(), None)),
            patch("googleapiclient.discovery.build", return_value=mock_scheduler),
        ):
            gd.check_scheduler_jobs()

        statuses = [r[1] for r in gd.results]
        assert len(gd.results) == 6
        assert all(s == "ok" for s in statuses)

    def test_check_scheduler_jobs_one_missing(self):
        """One job absent from GCP → that job FAIL, others OK."""
        import scripts.gaos_doctor as gd

        present = [j for j in self._EXPECTED_JOBS if j != "gaos-gmail-renew-watch"]
        jobs = [self._make_job(j) for j in present]
        mock_scheduler = MagicMock()
        (mock_scheduler.projects().locations().jobs().list().execute.return_value) = {"jobs": jobs}

        with (
            patch("scripts.gaos_doctor.google.auth.default", return_value=(MagicMock(), None)),
            patch("googleapiclient.discovery.build", return_value=mock_scheduler),
        ):
            gd.check_scheduler_jobs()

        fail_results = [r for r in gd.results if r[1] == "fail"]
        assert len(fail_results) == 1
        assert "gaos-gmail-renew-watch" in fail_results[0][0]
        ok_results = [r for r in gd.results if r[1] == "ok"]
        assert len(ok_results) == 5

    def test_check_scheduler_jobs_one_paused(self):
        """One job PAUSED → WARN (not FAIL) — job exists but won't fire."""
        import scripts.gaos_doctor as gd

        jobs = [
            self._make_job(j, state="PAUSED" if j == "gaos-daily-digest" else "ENABLED")
            for j in self._EXPECTED_JOBS
        ]
        mock_scheduler = MagicMock()
        (mock_scheduler.projects().locations().jobs().list().execute.return_value) = {"jobs": jobs}

        with (
            patch("scripts.gaos_doctor.google.auth.default", return_value=(MagicMock(), None)),
            patch("googleapiclient.discovery.build", return_value=mock_scheduler),
        ):
            gd.check_scheduler_jobs()

        warn_results = [r for r in gd.results if r[1] == "warn"]
        assert len(warn_results) == 1
        assert "gaos-daily-digest" in warn_results[0][0]
        fail_results = [r for r in gd.results if r[1] == "fail"]
        assert len(fail_results) == 0

    def test_check_scheduler_jobs_api_error(self):
        """Cloud Scheduler API call fails → single FAIL result."""
        import scripts.gaos_doctor as gd

        with (
            patch("scripts.gaos_doctor.google.auth.default", return_value=(MagicMock(), None)),
            patch(
                "googleapiclient.discovery.build",
                side_effect=Exception("403 Permission denied"),
            ),
        ):
            gd.check_scheduler_jobs()

        assert len(gd.results) == 1
        assert gd.results[0][1] == "fail"
        assert "403" in gd.results[0][2]


# ---------------------------------------------------------------------------
# Check 11: Ollama Tunnel Reachability
# ---------------------------------------------------------------------------


class TestCheckOllamaTunnel:
    """Tests for check_ollama_tunnel() in gaos_doctor.py."""

    def _mock_secret(self, host: str) -> MagicMock:
        """Return a mock SecretManagerServiceClient that yields ``host``."""
        client = MagicMock()
        payload = MagicMock()
        payload.data = host.encode("utf-8")
        client.access_secret_version.return_value.payload = payload
        return client

    def test_check_ollama_tunnel_200_ok(self):
        """Tunnel returns HTTP 200 → single OK result."""
        import scripts.gaos_doctor as gd

        mock_resp = MagicMock(status_code=200)
        with (
            patch(
                "scripts.gaos_doctor.secretmanager.SecretManagerServiceClient",
                return_value=self._mock_secret("https://gaos-ollama.loca.lt"),
            ),
            patch("scripts.gaos_doctor.httpx.get", return_value=mock_resp),
        ):
            gd.check_ollama_tunnel()

        assert len(gd.results) == 1
        assert gd.results[0][1] == "ok"

    def test_check_ollama_tunnel_non_200_fails(self):
        """Tunnel returns HTTP 503 → single FAIL result."""
        import scripts.gaos_doctor as gd

        mock_resp = MagicMock(status_code=503)
        with (
            patch(
                "scripts.gaos_doctor.secretmanager.SecretManagerServiceClient",
                return_value=self._mock_secret("https://gaos-ollama.loca.lt"),
            ),
            patch("scripts.gaos_doctor.httpx.get", return_value=mock_resp),
        ):
            gd.check_ollama_tunnel()

        assert len(gd.results) == 1
        assert gd.results[0][1] == "fail"
        assert "503" in gd.results[0][2]

    def test_check_ollama_tunnel_connection_error_fails(self):
        """Network error (timeout/refused) → single FAIL result with error detail."""
        import scripts.gaos_doctor as gd

        with (
            patch(
                "scripts.gaos_doctor.secretmanager.SecretManagerServiceClient",
                return_value=self._mock_secret("https://gaos-ollama.loca.lt"),
            ),
            patch(
                "scripts.gaos_doctor.httpx.get",
                side_effect=httpx.ConnectTimeout("ConnectTimeout"),
            ),
        ):
            gd.check_ollama_tunnel()

        assert len(gd.results) == 1
        assert gd.results[0][1] == "fail"
        assert "ConnectTimeout" in gd.results[0][2]

    def test_check_ollama_tunnel_secret_missing_fails(self):
        """Secret Manager raises → single FAIL result, no HTTP call made."""
        import scripts.gaos_doctor as gd

        bad_client = MagicMock()
        bad_client.access_secret_version.side_effect = Exception("403 Permission denied")

        with (
            patch(
                "scripts.gaos_doctor.secretmanager.SecretManagerServiceClient",
                return_value=bad_client,
            ),
            patch("scripts.gaos_doctor.httpx.get") as mock_http,
        ):
            gd.check_ollama_tunnel()

        assert len(gd.results) == 1
        assert gd.results[0][1] == "fail"
        assert "secret read failed" in gd.results[0][2]
        mock_http.assert_not_called()

    def test_check_ollama_tunnel_bypass_header_injected_for_loca_lt(self):
        """Bypass-Tunnel-Reminder header is sent when host contains .loca.lt."""
        import scripts.gaos_doctor as gd

        mock_resp = MagicMock(status_code=200)
        captured_headers: dict = {}

        def _fake_get(url, headers=None, timeout=None):
            captured_headers.update(headers or {})
            return mock_resp

        with (
            patch(
                "scripts.gaos_doctor.secretmanager.SecretManagerServiceClient",
                return_value=self._mock_secret("https://gaos-ollama.loca.lt"),
            ),
            patch("scripts.gaos_doctor.httpx.get", side_effect=_fake_get),
        ):
            gd.check_ollama_tunnel()

        assert captured_headers.get("Bypass-Tunnel-Reminder") == "true"


# ---------------------------------------------------------------------------
# send_doctor_report() + _maybe_send_report()
# ---------------------------------------------------------------------------


class TestSendDoctorReport:
    """Tests for send_doctor_report() and _maybe_send_report() in gaos_doctor.py."""

    def test_send_doctor_report_subject_contains_counts(self):
        """Subject line includes the OK / WARN / FAIL counts."""
        import scripts.gaos_doctor as gd

        gd.results[:] = [
            ("Sheet connectivity", "ok", ""),
            ("Pub/Sub topics", "warn", "1 topic missing"),
            ("Secret Manager", "fail", "GEMINI_API_KEY inaccessible"),
        ]

        captured: dict = {}

        def _fake_send_email(project_id, to, subject, body, from_addr=None, **kw):
            captured["subject"] = subject
            captured["body"] = body

        with (
            patch("scripts.gaos_doctor.get_settings") as mock_settings,
            patch("tools.gmail.send_email", _fake_send_email),
        ):
            mock_settings.return_value.gmail.alert_address = "alerts@example.com"
            mock_settings.return_value.gmail.sender_address = "from@example.com"
            gd.send_doctor_report(n_ok=1, n_warn=1, n_fail=1)

        assert "1 OK" in captured["subject"]
        assert "1 WARN" in captured["subject"]
        assert "1 FAIL" in captured["subject"]

    def test_send_doctor_report_body_lists_fail_items(self):
        """Body includes the label and detail of every FAIL result."""
        import scripts.gaos_doctor as gd

        gd.results[:] = [
            ("Sheet connectivity", "ok", ""),
            ("Secret Manager", "fail", "GEMINI_API_KEY inaccessible"),
        ]

        captured: dict = {}

        def _fake_send_email(project_id, to, subject, body, from_addr=None, **kw):
            captured["body"] = body

        with (
            patch("scripts.gaos_doctor.get_settings") as mock_settings,
            patch("tools.gmail.send_email", _fake_send_email),
        ):
            mock_settings.return_value.gmail.alert_address = "alerts@example.com"
            mock_settings.return_value.gmail.sender_address = "from@example.com"
            gd.send_doctor_report(n_ok=1, n_warn=0, n_fail=1)

        assert "Secret Manager" in captured["body"]
        assert "GEMINI_API_KEY inaccessible" in captured["body"]
        assert "[FAIL]" in captured["body"]

    def test_maybe_send_report_no_env_var_skips_send(self, monkeypatch):
        """When DOCTOR_SEND_REPORT is unset, send_doctor_report is never called."""
        import scripts.gaos_doctor as gd

        monkeypatch.delenv("DOCTOR_SEND_REPORT", raising=False)

        with patch.object(gd, "send_doctor_report") as mock_send:
            gd._maybe_send_report(n_ok=5, n_warn=0, n_fail=0)

        mock_send.assert_not_called()

    def test_maybe_send_report_email_failure_is_non_fatal(self, monkeypatch):
        """A send_email exception inside _maybe_send_report must not propagate."""
        import scripts.gaos_doctor as gd

        monkeypatch.setenv("DOCTOR_SEND_REPORT", "1")

        with patch.object(gd, "send_doctor_report", side_effect=Exception("SMTP timeout")):
            # Must not raise — email failure is logged as WARN, not fatal
            gd._maybe_send_report(n_ok=5, n_warn=0, n_fail=0)
