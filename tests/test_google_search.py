"""tests/test_google_search.py — Unit tests for tools/google_search.py"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from tools.google_search import (
    GoogleSearchError,
    research_topic,
    search,
)

_PROJECT = "test-project"
_API_KEY = "test-api-key-abc"


# ── Secret fixture ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_secrets():
    """Provide fake Serper API key via Secret Manager for all tests."""
    with patch("tools.secrets.get_secret", return_value=_API_KEY):
        yield


# ── Helpers ───────────────────────────────────────────────────────────────────


def _ok_resp(items: list) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"organic": items}
    return resp


def _item(title: str = "T", url: str = "https://x.com", snippet: str = "", date: str = "") -> dict:
    return {"title": title, "link": url, "snippet": snippet, "date": date}


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = status
    return httpx.HTTPStatusError(str(status), request=MagicMock(), response=mock_resp)


# ── search — happy path ───────────────────────────────────────────────────────


class TestSearch:
    def test_returns_result_list(self):
        with patch("tools.google_search.httpx.post", return_value=_ok_resp([_item()])):
            results = search("test query", _PROJECT)

        assert len(results) == 1
        assert results[0]["url"] == "https://x.com"
        assert results[0]["title"] == "T"

    def test_snippet_and_date_present_in_result(self):
        item = _item(
            title="Article", url="https://example.com", snippet="Some snippet", date="2026-01-01"
        )
        with patch("tools.google_search.httpx.post", return_value=_ok_resp([item])):
            results = search("query", _PROJECT)

        assert results[0]["snippet"] == "Some snippet"
        assert results[0]["date"] == "2026-01-01"

    def test_no_organic_key_returns_empty_list(self):
        resp = MagicMock(spec=httpx.Response)
        resp.raise_for_status.return_value = None
        resp.json.return_value = {}
        with patch("tools.google_search.httpx.post", return_value=resp):
            assert search("query", _PROJECT) == []

    def test_num_capped_at_10(self):
        captured: dict = {}

        def _fake_post(url, *, headers, json, timeout):
            captured["num"] = json["num"]
            return _ok_resp([])

        with patch("tools.google_search.httpx.post", side_effect=_fake_post):
            search("query", _PROJECT, num=99)

        assert captured["num"] == 10

    def test_num_minimum_is_1(self):
        captured: dict = {}

        def _fake_post(url, *, headers, json, timeout):
            captured["num"] = json["num"]
            return _ok_resp([])

        with patch("tools.google_search.httpx.post", side_effect=_fake_post):
            search("query", _PROJECT, num=0)

        assert captured["num"] == 1

    # ── empty / invalid input ─────────────────────────────────────────────────

    def test_empty_query_returns_empty_list(self):
        assert search("", _PROJECT) == []

    def test_whitespace_only_query_returns_empty_list(self):
        assert search("   ", _PROJECT) == []

    # ── Secret Manager failure ────────────────────────────────────────────────

    def test_secret_not_found_raises_google_search_error(self):
        from tools.secrets import SecretNotFoundError

        with patch(
            "tools.secrets.get_secret",
            side_effect=SecretNotFoundError("GOOGLE_SEARCH_API_KEY"),
        ):
            with pytest.raises(GoogleSearchError, match="credentials not available"):
                search("query", _PROJECT)

    def test_secret_access_denied_raises_google_search_error(self):
        from tools.secrets import SecretAccessDenied

        with patch(
            "tools.secrets.get_secret",
            side_effect=SecretAccessDenied("GOOGLE_SEARCH_CX"),
        ):
            with pytest.raises(GoogleSearchError, match="credentials not available"):
                search("query", _PROJECT)

    # ── Network / API failure ─────────────────────────────────────────────────

    def test_http_429_raises_quota_exceeded(self):
        with patch("tools.google_search.httpx.post", side_effect=_http_status_error(429)):
            with pytest.raises(GoogleSearchError, match="quota exceeded"):
                search("query", _PROJECT)

    def test_http_403_raises_api_key_error(self):
        with patch("tools.google_search.httpx.post", side_effect=_http_status_error(403)):
            with pytest.raises(GoogleSearchError, match="403"):
                search("query", _PROJECT)

    def test_http_500_raises_google_search_error(self):
        with patch("tools.google_search.httpx.post", side_effect=_http_status_error(500)):
            with pytest.raises(GoogleSearchError, match="500"):
                search("query", _PROJECT)

    def test_timeout_raises_google_search_error(self):
        with patch(
            "tools.google_search.httpx.post",
            side_effect=httpx.TimeoutException("timeout"),
        ):
            with pytest.raises(GoogleSearchError, match="timed out"):
                search("query", _PROJECT)

    def test_connect_error_raises_google_search_error(self):
        with patch(
            "tools.google_search.httpx.post",
            side_effect=httpx.ConnectError("no route"),
        ):
            with pytest.raises(GoogleSearchError, match="network error"):
                search("query", _PROJECT)

    def test_json_decode_error_raises_google_search_error(self):
        resp = MagicMock(spec=httpx.Response)
        resp.raise_for_status.return_value = None
        resp.json.side_effect = ValueError("bad json")
        with patch("tools.google_search.httpx.post", return_value=resp):
            with pytest.raises(GoogleSearchError, match="JSON decode"):
                search("query", _PROJECT)


# ── research_topic ────────────────────────────────────────────────────────────


class TestResearchTopic:
    def test_empty_queries_returns_empty_list(self):
        assert research_topic([], _PROJECT) == []

    def test_happy_path_returns_merged_results(self):
        item_a = _item("A", "https://example.com/a")
        item_b = _item("B", "https://example.com/b")
        call_count = 0

        def _fake_post(url, *, headers, json, timeout):
            nonlocal call_count
            items = [item_a] if call_count == 0 else [item_b]
            call_count += 1
            return _ok_resp(items)

        with patch("tools.google_search.httpx.post", side_effect=_fake_post):
            results = research_topic(["q1", "q2"], _PROJECT)

        urls = [r["url"] for r in results]
        assert "https://example.com/a" in urls
        assert "https://example.com/b" in urls

    def test_deduplicates_by_url(self):
        item_a = _item("A", "https://example.com/a")
        # Both queries return the same URL
        with patch("tools.google_search.httpx.post", return_value=_ok_resp([item_a])):
            results = research_topic(["q1", "q2"], _PROJECT)

        urls = [r["url"] for r in results]
        assert urls.count("https://example.com/a") == 1

    def test_failed_query_skipped_rest_continue(self):
        item = _item("OK", "https://ok.com/result")
        call_count = 0

        def _fake_post(url, *, headers, json, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("simulated failure")
            return _ok_resp([item])

        with patch("tools.google_search.httpx.post", side_effect=_fake_post):
            results = research_topic(["bad", "good"], _PROJECT)

        assert any(r["url"] == "https://ok.com/result" for r in results)

    def test_max_queries_caps_execution(self):
        call_count = 0

        def _fake_post(url, *, headers, json, timeout):
            nonlocal call_count
            call_count += 1
            return _ok_resp([])

        with patch("tools.google_search.httpx.post", side_effect=_fake_post):
            research_topic(["q1", "q2", "q3", "q4"], _PROJECT, max_queries=2)

        assert call_count == 2

    def test_blank_query_strings_are_skipped(self):
        call_count = 0

        def _fake_post(url, *, headers, json, timeout):
            nonlocal call_count
            call_count += 1
            return _ok_resp([])

        with patch("tools.google_search.httpx.post", side_effect=_fake_post):
            research_topic(["", "   ", "real query"], _PROJECT)

        assert call_count == 1

    def test_all_queries_fail_returns_empty_list(self):
        with patch(
            "tools.google_search.httpx.post",
            side_effect=httpx.ConnectError("all down"),
        ):
            assert research_topic(["q1", "q2"], _PROJECT) == []
