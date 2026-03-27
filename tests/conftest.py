"""
tests/conftest.py — Shared pytest fixtures for the GAOS test suite.

Autouse fixtures that prevent real GCP calls during unit tests.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _mock_init_sheets_client():
    """
    Prevent init_sheets_client from attempting a real gspread/auth call in
    unit tests. All sheet operation functions (get_all_records, append_row,
    delete_rows, etc.) are mocked individually per-test; this fixture ensures
    the initialization guard also never fires.
    """
    with patch("tools.google_sheets.init_sheets_client", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _mock_agents_log_cloud():
    """
    Prevent _log_cloud from making real Cloud Logging API calls during unit
    tests.  The function itself has ``except Exception: pass``, but the Google
    Cloud retry layer issues ``time.sleep()`` calls *before* the final
    exception is raised, causing tests to hang for minutes without this patch.

    Two patch targets are required:
    - ``agents._log_cloud`` — patches the canonical definition.
    - ``agents.nexus_prime.orchestrator._log_cloud`` — patches the module-local
      name created by ``from agents import _log_cloud`` in orchestrator.py.
      Without this second patch the orchestrator's local binding still points
      to the live function and Cloud Logging calls escape the mock.
    """
    with patch("agents._log_cloud"), patch("agents.nexus_prime.orchestrator._log_cloud"):
        yield
