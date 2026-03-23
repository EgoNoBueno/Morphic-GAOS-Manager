"""tests/test_web_search.py — Unit tests for tools/web_search.py"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from tools.web_search import web_search

# ── Helpers ───────────────────────────────────────────────────────────────────


def _ddg_resp(
    abstract: str = "",
    abstract_source: str = "",
    answer: str = "",
    related: list | None = None,
) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "AbstractText": abstract,
        "AbstractSource": abstract_source,
        "Answer": answer,
        "RelatedTopics": related or [],
    }
    return resp


# ── Happy path ────────────────────────────────────────────────────────────────


class TestWebSearchHappyPath:
    def test_abstract_included_with_source(self):
        resp = _ddg_resp(abstract="Steel is an iron alloy.", abstract_source="Wikipedia")
        with patch("tools.web_search.httpx.get", return_value=resp):
            result = web_search("steel alloy")

        assert "Steel is an iron alloy." in result
        assert "Wikipedia" in result

    def test_direct_answer_included(self):
        resp = _ddg_resp(answer="42")
        with patch("tools.web_search.httpx.get", return_value=resp):
            result = web_search("ultimate answer")

        assert "Direct answer: 42" in result

    def test_related_topics_included(self):
        related = [{"Text": "Carbon steel facts"}, {"Text": "Alloy steel facts"}]
        resp = _ddg_resp(related=related)
        with patch("tools.web_search.httpx.get", return_value=resp):
            result = web_search("steel types")

        assert "Carbon steel facts" in result
        assert "Alloy steel facts" in result

    def test_max_results_caps_related_topics(self):
        related = [{"Text": f"Topic {i}"} for i in range(10)]
        resp = _ddg_resp(related=related)
        with patch("tools.web_search.httpx.get", return_value=resp):
            result = web_search("broad query", max_results=3)

        for i in range(3):
            assert f"Topic {i}" in result
        for i in range(3, 10):
            assert f"Topic {i}" not in result

    def test_abstract_source_omitted_when_empty(self):
        resp = _ddg_resp(abstract="Some abstract", abstract_source="")
        with patch("tools.web_search.httpx.get", return_value=resp):
            result = web_search("query")

        assert "[]" not in result  # no empty bracket from empty source
        assert "Some abstract" in result

    def test_related_topics_without_text_field_are_skipped(self):
        related = [{"Url": "https://x.com"}, {"Text": "Valid topic"}]
        resp = _ddg_resp(related=related)
        with patch("tools.web_search.httpx.get", return_value=resp):
            result = web_search("query")

        assert "Valid topic" in result

    def test_all_fields_combined_in_output(self):
        related = [{"Text": "Related item"}]
        resp = _ddg_resp(
            abstract="Abstract text.",
            abstract_source="Src",
            answer="Direct",
            related=related,
        )
        with patch("tools.web_search.httpx.get", return_value=resp):
            result = web_search("query")

        assert "Abstract text." in result
        assert "Direct answer: Direct" in result
        assert "Related item" in result


# ── Empty / invalid input ─────────────────────────────────────────────────────


class TestWebSearchEmptyInput:
    def test_empty_query_returns_empty_string(self):
        assert web_search("") == ""

    def test_whitespace_only_query_returns_empty_string(self):
        assert web_search("   ") == ""

    def test_no_content_returns_empty_string(self):
        resp = _ddg_resp()  # no abstract, no answer, no related
        with patch("tools.web_search.httpx.get", return_value=resp):
            assert web_search("nothing here") == ""

    def test_related_topic_with_empty_text_is_skipped(self):
        related = [{"Text": ""}, {"Text": "   "}]
        resp = _ddg_resp(related=related)
        with patch("tools.web_search.httpx.get", return_value=resp):
            assert web_search("query") == ""


# ── Network / API failure ─────────────────────────────────────────────────────


class TestWebSearchNetworkFailure:
    def test_timeout_returns_empty_string(self):
        with patch(
            "tools.web_search.httpx.get",
            side_effect=httpx.TimeoutException("timeout"),
        ):
            assert web_search("query") == ""

    def test_connect_error_returns_empty_string(self):
        with patch(
            "tools.web_search.httpx.get",
            side_effect=httpx.ConnectError("no route to host"),
        ):
            assert web_search("query") == ""

    def test_http_status_error_returns_empty_string(self):
        mock_resp = MagicMock(spec=httpx.Response)
        exc = httpx.HTTPStatusError("429", request=MagicMock(), response=mock_resp)
        with patch("tools.web_search.httpx.get", side_effect=exc):
            assert web_search("query") == ""

    def test_json_decode_error_returns_empty_string(self):
        resp = MagicMock(spec=httpx.Response)
        resp.raise_for_status.return_value = None
        resp.json.side_effect = ValueError("bad json")
        with patch("tools.web_search.httpx.get", return_value=resp):
            assert web_search("query") == ""
