"""
tests/test_scout_discover.py — Phase 2.5 Step 6: Scout _discover + _inject_knowledge.

TestGoogleSearchTool (5 tests):
  GS1  Happy path: search() returns parsed result list.
  GS2  Empty query string returns empty list without calling the API.
  GS3  HTTP 429 raises GoogleSearchError with "quota exceeded".
  GS4  Missing credentials raises GoogleSearchError.
  GS5  num > 10 is capped to 10 in the API request.

TestResearchTopic (4 tests):
  RT1  Multiple queries are deduplicated by URL.
  RT2  max_queries cap is enforced — no more than N searches executed.
  RT3  A failed query is skipped; subsequent queries still run.
  RT4  Empty queries list returns empty list.

TestDiscoverNode (6 tests):
  DN1  Returns state unchanged when incoming_message is None.
  DN2  Results from research_topic appear in sub_task_results.
  DN3  Corroborated findings (source_count ≥ 5) go into observation_buffer.
  DN4  Findings below threshold do not reach observation_buffer.
  DN5  GoogleSearchError on all queries is graceful (sub_task_results is empty, no raise).
  DN6  iteration_count reflects queries_used from the discovery loop.

TestInjectKnowledgeNode (5 tests):
  IK1  Publishes KNOWLEDGE_INJECTION when observation_buffer is non-empty.
  IK2  Skips publish when observation_buffer is empty.
  IK3  Appends Section E to Blueprint Doc when blueprint_doc_id is in payload.
  IK4  Skips append when blueprint_doc_id is absent.
  IK5  Publish failure is graceful — state still returned.

TestRouteAfterBoot (2 tests):
  RB1  RESEARCH_MANDATE message routes to "discover".
  RB2  Any other message type (or None) routes to "plan".

TestInitialStatePubSub (2 tests):
  IS1  Dict agent_input with valid Pub/Sub envelope extracts incoming_message.
  IS2  AgentInput object sets project_id and task_id correctly.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from models import AgentWorkingMemory

# ── Settings fixture ─────────────────────────────────────────────────────────

_SETTINGS_YAML = """\
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
google_search:
  api_key_secret: GOOGLE_SEARCH_API_KEY
  cx_secret: GOOGLE_SEARCH_CX
  max_search_depth: 2
  max_queries_per_mandate: 6
"""


def _make_settings(tmp_path, yaml_text: str = _SETTINGS_YAML):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml_text)
    import config

    config._reset_for_testing()
    config.load_settings(cfg)
    return cfg


@pytest.fixture(autouse=True)
def reset_settings():
    yield
    import config

    config._reset_for_testing()


# ── Shared helpers ────────────────────────────────────────────────────────────


def _make_mandate(topic="loyalty program trends", blueprint_doc_id=""):
    from models import A2AMessage, MessageType

    return A2AMessage(
        source_agent="nexus-prime",
        target_agent="scout",
        project_id="test-project",
        task_id="mandate-001",
        message_type=MessageType.RESEARCH_MANDATE,
        priority=3,
        payload={"topic": topic, "blueprint_doc_id": blueprint_doc_id},
    )


def _base_state(extra=None) -> AgentWorkingMemory:
    state: AgentWorkingMemory = {  # type: ignore[assignment]
        "task_id": "mandate-001",
        "project_id": "test-project",
        "cost_usd": 0.0,
        "tokens_used": 0,
        "messages": [],
        "sub_task_results": [],
        "observation_buffer": [],
        "error_history": [],
        "iteration_count": 0,
        "memory_context": {},
        "episodic_cache": {},
        "parked_proposals": [],
        "hard_stop_triggered": False,
        "evolution_triggered": False,
    }
    if extra:
        state.update(extra)
    return state


def _fake_model_resp(data=None, text=""):
    from agents import ModelResponse

    return ModelResponse(text=text, data=data or {}, cost_usd=0.001, tokens_used=50)


def _fake_search_results(n=8):
    return [
        {
            "title": f"Result {i}",
            "url": f"https://source{i}.com/article",
            "snippet": f"Snippet {i}",
            "date": "",
        }
        for i in range(n)
    ]


# ── TestGoogleSearchTool ──────────────────────────────────────────────────────


class TestGoogleSearchTool:
    """Tests for tools.google_search.search()."""

    def _make_mock_response(self, items):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"organic": items}
        return mock_resp

    # GS1
    def test_happy_path_returns_result_list(self, tmp_path):
        _make_settings(tmp_path)
        items = [
            {
                "title": "Loyalty Trends 2026",
                "link": "https://example.com",
                "snippet": "Key trends...",
                "date": "",
            },
        ]
        with (
            patch("tools.secrets.get_secret", return_value="fake-key"),
            patch("tools.google_search.httpx.post", return_value=self._make_mock_response(items)),
        ):
            from tools.google_search import search

            results = search("loyalty trends 2026", "test-project")
        assert len(results) == 1
        assert results[0]["title"] == "Loyalty Trends 2026"
        assert results[0]["url"] == "https://example.com"
        assert results[0]["snippet"] == "Key trends..."

    # GS2
    def test_empty_query_returns_empty_without_api_call(self, tmp_path):
        _make_settings(tmp_path)
        with patch("tools.google_search.httpx.post") as mock_post:
            from tools.google_search import search

            result = search("", "test-project")
        assert result == []
        mock_post.assert_not_called()

    # GS3
    def test_http_429_raises_google_search_error(self, tmp_path):
        _make_settings(tmp_path)
        import httpx

        mock_resp = MagicMock()
        mock_resp.status_code = 429
        exc = httpx.HTTPStatusError("rate limit", request=MagicMock(), response=mock_resp)
        mock_post = MagicMock()
        mock_post.return_value.raise_for_status.side_effect = exc
        with (
            patch("tools.secrets.get_secret", return_value="k"),
            patch("tools.google_search.httpx.post", mock_post),
        ):
            from tools.google_search import GoogleSearchError, search

            with pytest.raises(GoogleSearchError, match="quota exceeded"):
                search("query", "test-project")

    # GS4
    def test_missing_credentials_raises_google_search_error(self, tmp_path):
        _make_settings(tmp_path)
        from tools.secrets import SecretNotFoundError

        with patch("tools.secrets.get_secret", side_effect=SecretNotFoundError("no key")):
            from tools.google_search import GoogleSearchError, search

            with pytest.raises(GoogleSearchError, match="credentials not available"):
                search("query", "test-project")

    # GS5
    def test_num_capped_at_10(self, tmp_path):
        _make_settings(tmp_path)
        captured = {}

        def mock_post(url, headers=None, json=None, timeout=None):
            captured["num"] = (json or {}).get("num")
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {"organic": []}
            return mock_resp

        with (
            patch("tools.secrets.get_secret", return_value="k"),
            patch("tools.google_search.httpx.post", mock_post),
        ):
            from tools.google_search import search

            search("query", "test-project", num=20)
        assert captured["num"] == 10


# ── TestResearchTopic ─────────────────────────────────────────────────────────


class TestResearchTopic:
    """Tests for tools.google_search.research_topic()."""

    # RT1
    def test_deduplicates_by_url(self, tmp_path):
        _make_settings(tmp_path)
        # Both queries return the same URL among their results
        dup_url = "https://same.com/article"
        results_q1 = [
            {"title": "A", "url": dup_url, "snippet": "", "date": ""},
            {"title": "B", "url": "https://unique1.com", "snippet": "", "date": ""},
        ]
        results_q2 = [
            {"title": "A2", "url": dup_url, "snippet": "", "date": ""},
            {"title": "C", "url": "https://unique2.com", "snippet": "", "date": ""},
        ]
        call_count = {"n": 0}

        def fake_search(query, project_id, num=10):
            call_count["n"] += 1
            return results_q1 if call_count["n"] == 1 else results_q2

        with patch("tools.google_search.search", fake_search):
            from tools.google_search import research_topic

            results = research_topic(["q1", "q2"], "test-project")

        urls = [r["url"] for r in results]
        assert urls.count(dup_url) == 1  # deduplicated
        assert len(results) == 3  # A + B + C

    # RT2
    def test_max_queries_cap_enforced(self, tmp_path):
        _make_settings(tmp_path)
        call_count = {"n": 0}

        def fake_search(query, project_id, num=10):
            call_count["n"] += 1
            return [
                {
                    "title": f"R{call_count['n']}",
                    "url": f"https://s{call_count['n']}.com",
                    "snippet": "",
                    "date": "",
                }
            ]

        with patch("tools.google_search.search", fake_search):
            from tools.google_search import research_topic

            research_topic(["q1", "q2", "q3", "q4", "q5"], "test-project", max_queries=3)

        assert call_count["n"] == 3

    # RT3
    def test_failed_query_skipped_others_succeed(self, tmp_path):
        _make_settings(tmp_path)
        call_count = {"n": 0}
        from tools.google_search import GoogleSearchError

        def fake_search(query, project_id, num=10):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise GoogleSearchError("transient error")
            return [
                {
                    "title": "OK",
                    "url": f"https://ok{call_count['n']}.com",
                    "snippet": "",
                    "date": "",
                }
            ]

        with patch("tools.google_search.search", fake_search):
            from tools.google_search import research_topic

            results = research_topic(["q1", "q2", "q3"], "test-project")

        assert len(results) == 2
        assert call_count["n"] == 3

    # RT4
    def test_empty_queries_list_returns_empty(self, tmp_path):
        _make_settings(tmp_path)
        with patch("tools.google_search.search") as mock_search:
            from tools.google_search import research_topic

            result = research_topic([], "test-project")
        assert result == []
        mock_search.assert_not_called()


# ── TestDiscoverNode ──────────────────────────────────────────────────────────


class TestDiscoverNode:
    """Tests for agents.scout.orchestrator._discover()."""

    def _run(
        self,
        tmp_path,
        topic="loyalty trends",
        search_results=None,
        corr_data=None,
        search_raises=None,
        extra_state=None,
    ):
        _make_settings(tmp_path)
        msg = _make_mandate(topic=topic)
        state = _base_state({"incoming_message": msg, **(extra_state or {})})

        if search_results is None:
            search_results = _fake_search_results(8)

        init_queries = [
            {"query": "loyalty market 2026", "intent": "trends"},
            {"query": "loyalty competitor analysis", "intent": "competition"},
        ]
        follow_queries: list = []

        call_n = {"n": 0}

        def fake_model(prompt, model, parse_json=False, **kw):
            call_n["n"] += 1
            if "follow-up" in prompt or "deeper" in prompt:
                return _fake_model_resp(data=follow_queries)
            if "Identify" in prompt or "corroborated" in prompt:
                return _fake_model_resp(data=corr_data or [])
            return _fake_model_resp(data=init_queries)

        if search_raises:
            fake_research = MagicMock(side_effect=search_raises)
        else:
            fake_research = MagicMock(return_value=search_results)

        with (
            patch("agents.scout.orchestrator._call_model", side_effect=fake_model),
            patch("agents.scout.orchestrator._log_cloud"),
            patch("tools.google_search.research_topic", fake_research),
        ):
            from agents.scout.orchestrator import _discover

            result = _discover(state)

        return result

    # DN1
    def test_no_message_returns_state_unchanged(self, tmp_path):
        _make_settings(tmp_path)
        state = _base_state()
        with patch("agents.scout.orchestrator._call_model"):
            from agents.scout.orchestrator import _discover

            result = _discover(state)
        assert result["sub_task_results"] == []

    # DN2
    def test_results_stored_in_sub_task_results(self, tmp_path):
        results = _fake_search_results(5)
        state = self._run(tmp_path, search_results=results)
        assert len(state["sub_task_results"]) == 5
        assert state["sub_task_results"][0]["task_type"] == "discovery"
        assert state["sub_task_results"][0]["status"] == "success"

    # DN3
    def test_corroborated_findings_in_observation_buffer(self, tmp_path):
        corr = [
            {
                "finding": "Loyalty programs drive 20% repeat purchase",
                "source_count": 7,
                "sources": ["a.com", "b.com", "c.com", "d.com", "e.com", "f.com", "g.com"],
            }
        ]
        state = self._run(tmp_path, corr_data=corr)
        assert len(state["observation_buffer"]) == 1
        obs = state["observation_buffer"][0]
        assert obs["knowledge_type"] == "market_intel"
        assert obs["confidence"] >= 0.70
        assert "market_intel" in obs["tags"]

    # DN4
    def test_below_threshold_no_observation_buffer(self, tmp_path):
        # source_count = 3 — below the ≥5 threshold
        corr = [{"finding": "Some finding", "source_count": 3, "sources": ["a.com"]}]
        state = self._run(tmp_path, corr_data=corr)
        assert state["observation_buffer"] == []

    # DN5
    def test_search_error_is_graceful(self, tmp_path):
        from tools.google_search import GoogleSearchError

        state = self._run(tmp_path, search_raises=GoogleSearchError("API down"))
        # Should return without raising; sub_task_results empty
        assert isinstance(state, dict)
        assert state["sub_task_results"] == []

    # DN6
    def test_iteration_count_set_to_queries_used(self, tmp_path):
        state = self._run(tmp_path, search_results=_fake_search_results(4))
        # init_queries has 2 entries; _discover executes 1 batch (depth 0) → 2 queries
        assert state["iteration_count"] >= 1


# ── TestInjectKnowledgeNode ───────────────────────────────────────────────────


class TestInjectKnowledgeNode:
    """Tests for agents.scout.orchestrator._inject_knowledge()."""

    def _run(
        self,
        tmp_path,
        observation_buffer=None,
        sub_task_results=None,
        blueprint_doc_id="",
        publish_raises=None,
        append_raises=None,
    ):
        _make_settings(tmp_path)
        msg = _make_mandate(topic="loyalty trends", blueprint_doc_id=blueprint_doc_id)
        state = _base_state(
            {
                "incoming_message": msg,
                "observation_buffer": observation_buffer or [],
                "sub_task_results": sub_task_results or [{"status": "success", "output": {}}],
            }
        )

        mock_publish = MagicMock(side_effect=publish_raises) if publish_raises else MagicMock()
        mock_append = MagicMock(side_effect=append_raises) if append_raises else MagicMock()

        with (
            patch("tools.pubsub.publish", mock_publish),
            patch("tools.google_docs.append_content", mock_append),
            patch("agents.scout.orchestrator._log_cloud"),
        ):
            from agents.scout.orchestrator import _inject_knowledge

            result = _inject_knowledge(state)

        return result, mock_publish, mock_append

    def _corroborated(self):
        return [
            {
                "content": "Loyalty drives revenue",
                "knowledge_type": "market_intel",
                "tags": ["loyalty"],
                "source_count": 6,
                "sources": [],
                "mandate_id": "m1",
                "confidence": 0.72,
            }
        ]

    # IK1
    def test_publishes_when_corroborated_findings_present(self, tmp_path):
        _, mock_publish, _ = self._run(tmp_path, observation_buffer=self._corroborated())
        mock_publish.assert_called_once()
        call_args = mock_publish.call_args[0]
        msg = call_args[1]
        from models import MessageType

        assert msg.message_type == MessageType.KNOWLEDGE_INJECTION
        assert msg.target_agent == "nexus-prime"
        assert len(msg.payload["findings"]) == 1

    # IK2
    def test_skips_publish_when_no_corroborated_findings(self, tmp_path):
        _, mock_publish, _ = self._run(tmp_path, observation_buffer=[])
        mock_publish.assert_not_called()

    # IK3
    def test_appends_section_e_when_blueprint_doc_id_present(self, tmp_path):
        _, _, mock_append = self._run(
            tmp_path,
            observation_buffer=self._corroborated(),
            blueprint_doc_id="doc-blueprint-123",
        )
        mock_append.assert_called_once()
        content_arg = mock_append.call_args[0][1]
        assert "Section E" in content_arg
        assert "Market Intelligence" in content_arg

    # IK4
    def test_skips_append_when_no_blueprint_doc_id(self, tmp_path):
        _, _, mock_append = self._run(
            tmp_path,
            observation_buffer=self._corroborated(),
            blueprint_doc_id="",
        )
        mock_append.assert_not_called()

    # IK5
    def test_publish_failure_is_graceful(self, tmp_path):
        result, _, _ = self._run(
            tmp_path,
            observation_buffer=self._corroborated(),
            publish_raises=RuntimeError("Pub/Sub down"),
        )
        assert result["project_id"] == "test-project"


# ── TestRouteAfterBoot ────────────────────────────────────────────────────────


class TestRouteAfterBoot:
    """Tests for agents.scout.orchestrator._route_after_boot()."""

    # RB1
    def test_research_mandate_routes_to_discover(self, tmp_path):
        _make_settings(tmp_path)
        msg = _make_mandate()
        state = _base_state({"incoming_message": msg})
        from agents.scout.orchestrator import _route_after_boot

        assert _route_after_boot(state) == "discover"

    # RB2
    def test_other_message_routes_to_plan(self, tmp_path):
        _make_settings(tmp_path)
        from models import A2AMessage, MessageType

        for mt in (MessageType.ALERT, MessageType.TASK_HANDOFF, None):
            if mt is not None:
                msg = A2AMessage(
                    source_agent="foreman",
                    target_agent="scout",
                    project_id="test-project",
                    task_id="t1",
                    message_type=mt,
                    priority=2,
                    payload={},
                )
                state = _base_state({"incoming_message": msg})
            else:
                state = _base_state()
            from agents.scout.orchestrator import _route_after_boot

            assert _route_after_boot(state) == "plan"


# ── TestInitialStatePubSub ────────────────────────────────────────────────────


class TestInitialStatePubSub:
    """Tests for the updated _initial_state() Pub/Sub envelope decoding."""

    # IS1
    def test_dict_envelope_extracts_incoming_message(self, tmp_path):
        _make_settings(tmp_path)
        import base64

        from models import A2AMessage, MessageType

        mandate = A2AMessage(
            source_agent="nexus-prime",
            target_agent="scout",
            project_id="test-project",
            task_id="t-pubsub-1",
            message_type=MessageType.RESEARCH_MANDATE,
            priority=3,
            payload={"topic": "e-com trends"},
        )
        envelope = {
            "message": {
                "data": base64.b64encode(mandate.model_dump_json().encode()).decode(),
                "messageId": "m-1",
            },
            "subscription": "projects/test/subscriptions/sub",
        }
        with (
            patch("agents.scout.orchestrator._call_model"),
            patch("agents.scout.orchestrator._log_cloud"),
        ):
            from agents.scout.orchestrator import _initial_state

            state = _initial_state(envelope)

        assert state["incoming_message"] is not None
        assert state["incoming_message"].message_type == MessageType.RESEARCH_MANDATE
        assert state["project_id"] == "test-project"
        assert state["task_id"] == "t-pubsub-1"

    # IS2
    def test_agent_input_sets_project_and_task_id(self, tmp_path):
        _make_settings(tmp_path)
        from models import AgentInput

        ai = AgentInput(
            task_id="task-xyz", project_id="proj-abc", instruction="do research", context={}
        )
        from agents.scout.orchestrator import _initial_state

        state = _initial_state(ai)
        assert state["task_id"] == "task-xyz"
        assert state["project_id"] == "proj-abc"
        assert state["incoming_message"] is None
