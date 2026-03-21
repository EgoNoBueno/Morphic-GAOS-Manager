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
