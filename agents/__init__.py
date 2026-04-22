"""
agents/__init__.py — Shared utilities for all Morphic-G AOS agent orchestrators.

Provides:
  - ModelResponse and _call_model()        — thin wrapper over google.genai / Ollama
  - _call_model_ollama()                   — Ollama HTTP routing with timeout + fallback
  - validate_code_safety()                 — AST-based import + pattern gate (Gates 1 + 2)
  - _validate_proposal_coherence()         — quality gate run before Agent_Approvals write
  - _run_evolution_loop()                  — Write-Test-Refine loop (§13.1)
  - _load_identity_file()             — reads Docs/agents/<name>.md at boot
  - _write_heartbeat()                — Tier 2 dashboard heartbeat helper
  - _log_cloud()                      — structured Cloud Logging entry
  - utcnow_iso() / utcnow_date()      — UTC timestamp helpers
  - _elapsed_seconds()                — wall-clock duration from state start
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Agent lifecycle state machine ────────────────────────────────────────────


class AgentState(StrEnum):
    """Formal lifecycle states for GAOS agent orchestrators.

    Models the OODA-loop state machine described in §8.23 of the OpenClaw
    Paradigm Book. Transitions should be logged via log_state_transition().
    """

    INIT = "INIT"
    PLANNING = "PLANNING"
    EXECUTION = "EXECUTION"
    OBSERVATION = "OBSERVATION"
    HEALING = "HEALING"
    SYNTHESIS = "SYNTHESIS"
    ESCALATION = "ESCALATION"
    IDLE = "IDLE"
    COMPLETED = "COMPLETED"


def log_state_transition(
    agent_id: str,
    project_id: str,
    task_id: str,
    from_state: AgentState | str,
    to_state: AgentState | str,
    reason: str = "",
) -> None:
    """Log an agent lifecycle state transition to Cloud Logging.

    Should be called at every OODA loop boundary so the state machine is fully
    auditable in Cloud Logging and the Grafana dashboard.

    Args:
        agent_id:   The agent identifier (e.g., "nexus-prime").
        project_id: The AOS project namespace.
        task_id:    The current task UUID (or empty string for boot transitions).
        from_state: The state being exited.
        to_state:   The state being entered.
        reason:     Optional human-readable explanation for the transition.
    """
    _log_cloud(
        agent_id,
        project_id,
        "state_transition",
        task_id,
        f"State: {from_state} → {to_state}" + (f" | {reason}" if reason else ""),
        severity="INFO",
        extra={"from_state": str(from_state), "to_state": str(to_state), "reason": reason},
    )


# ── Ollama fallback telemetry ────────────────────────────────────────────────

_ollama_fallback_lock = threading.Lock()
_ollama_fallback_count: int = 0

# ── Non-Ollama LLM alert telemetry ───────────────────────────────────────────
# Sends a Google Chat notification the first time a non-Ollama model is called,
# then rate-limits to one alert per model per _GEMINI_ALERT_INTERVAL_SECONDS.
# Intent: while OLLAMA_HOST is broken, every Gemini call is unexpected — the
# owner should know immediately without being flooded.

_GEMINI_ALERT_INTERVAL_SECONDS: int = 300  # 5 minutes between alerts per model
_gemini_alert_lock = threading.Lock()
# Maps model-name → epoch-seconds of last alert sent
_gemini_last_alert: dict[str, float] = {}


def get_ollama_fallback_count() -> int:
    """Return the number of times the system has fallen back from Ollama to Gemini.

    Thread-safe. Resets to zero only when reset_ollama_fallback_count() is called.
    """
    with _ollama_fallback_lock:
        return _ollama_fallback_count


def reset_ollama_fallback_count() -> None:
    """Reset the Ollama-to-Gemini fallback counter to zero."""
    global _ollama_fallback_count
    with _ollama_fallback_lock:
        _ollama_fallback_count = 0


# ── Timestamp helpers ─────────────────────────────────────────────────────────

_DOCS_ROOT = Path(__file__).parent.parent / "Docs" / "agents"
_CONTEXT_TRIO_ROOT = Path(__file__).parent.parent / "Docs"
_CONTEXT_TRIO_FILES = ("about-me.md", "brand-voice.md", "working-preferences.md")


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def utcnow_date() -> str:
    return datetime.now(UTC).date().isoformat()


def _elapsed_seconds(state: dict) -> float:
    started = state.get("_started_at", time.time())
    return round(time.time() - started, 2)


# ── Model call wrapper ────────────────────────────────────────────────────────


@dataclass
class ModelResponse:
    text: str
    cost_usd: float = 0.0
    tokens_used: int = 0
    data: dict = field(default_factory=dict)


def _call_model(
    prompt: str,
    model: str,
    system_prompt: str = "",
    parse_json: bool = False,
    web_access: bool = False,
    image_bytes: bytes | None = None,
) -> ModelResponse:
    """
    Routes to Ollama (local) or google.genai depending on the model alias.

    If model starts with "ollama/":
      - Optionally prepends web search results when web_access=True
      - POSTs to OLLAMA_HOST/api/generate with LOCAL_MODEL_TIMEOUT_SECONDS timeout
      - On httpx.TimeoutException, httpx.ConnectError, or httpx.HTTPStatusError
        (including 502 Bad Gateway from a dead tunnel), raises RuntimeError — the
        Gemini fallback is DISABLED. The error propagates to the caller; no Gemini
        tokens are spent. Callers must catch RuntimeError and handle gracefully;
        they must NOT re-invoke _call_model() with a Gemini alias as a substitute.
      - image_bytes are not supported for Ollama; a warning is logged and bytes ignored.

    If model is a Gemini alias:
      - Calls google.genai with the GEMINI_API_KEY secret
      - When image_bytes is provided, sends a multimodal request (image + text).
        Use DEEP_MODEL for multimodal calls — Pro models have higher vision accuracy.
        Multimodal calls consume significantly more tokens; budget monitoring is
        the caller's responsibility.

    Args:
        prompt:        User-turn content.
        model:         Resolved model alias string (e.g. "ollama/llama3" or "gemini-2.5-flash").
        system_prompt: Optional system instruction prefix.
        parse_json:    If True, attempt to extract and parse JSON from response.
        web_access:    If True and model is an Ollama alias, prepend DuckDuckGo search
                       results for the prompt before sending to the local model.
        image_bytes:   Optional raw image bytes for multimodal Gemini calls.
                       Ignored (with a warning) when the model is an Ollama alias.

    Returns:
        ModelResponse with text, rough cost_usd, and parsed data dict.
    """
    from config import get_settings
    from tools import get_caller, record_api_call

    settings = get_settings()

    if web_access and model.startswith("ollama/"):
        from tools.web_search import web_search

        snippets = web_search(prompt)
        if snippets:
            prompt = f"Web search results for context:\n{snippets}\n\n---\n\n{prompt}"

    api_name = "ollama" if model.startswith("ollama/") else "gemini"
    caller = get_caller()
    pid = settings.GCP_PROJECT_ID

    with record_api_call(api_name, "_call_model", caller, pid) as ctx:
        if model.startswith("ollama/"):
            if image_bytes is not None:
                logger.warning(
                    "image_bytes provided for Ollama model '%s' — multimodal is not supported "
                    "for local models. The image will be ignored.",
                    model,
                )
            resp = _call_model_ollama(prompt, model, system_prompt, parse_json, settings)
        else:
            resp = _call_model_gemini(
                prompt, model, system_prompt, parse_json, settings, image_bytes
            )
        ctx["tokens_used"] = resp.tokens_used
        ctx["model"] = model

    warn_threshold = int(getattr(settings.models, "TOKEN_WARNING_THRESHOLD", 8000))
    if resp.tokens_used > warn_threshold:
        logger.warning(
            "Token runaway detected: model=%s tokens=%d exceeds TOKEN_WARNING_THRESHOLD=%d",
            model,
            resp.tokens_used,
            warn_threshold,
        )

    return resp


def _call_model_ollama(
    prompt: str,
    model: str,
    system_prompt: str,
    parse_json: bool,
    settings: Any,
    no_fallback: bool = False,  # retained for call-site compatibility; always behaves as True
) -> ModelResponse:
    """
    Calls the local Ollama server. Raises RuntimeError on any connection or timeout
    error — Gemini fallback is permanently disabled until a network-reachable
    OLLAMA_HOST is configured in Secret Manager.
    Model alias format: "ollama/<model-name>" e.g. "ollama/llama3"
    """
    import httpx

    ollama_model = model.split("/", 1)[1]  # strip "ollama/" prefix
    timeout_s = float(getattr(settings.models, "LOCAL_MODEL_TIMEOUT_SECONDS", 2))

    try:
        from tools.secrets import get_secret

        host = (
            get_secret("OLLAMA_HOST", settings.GCP_PROJECT_ID).lstrip("\ufeff").strip().rstrip("/")
        )
    except Exception:
        host = "http://localhost:11434"

    full_prompt = f"System: {system_prompt}\n\n{prompt}" if system_prompt else prompt

    # localtunnel (loca.lt) serves an HTML challenge page to clients that don't
    # identify themselves as non-browser traffic.  The header below bypasses it.
    extra_headers: dict[str, str] = {}
    if ".loca.lt" in host:
        extra_headers["Bypass-Tunnel-Reminder"] = "true"

    try:
        response = httpx.post(
            f"{host}/api/generate",
            json={"model": ollama_model, "prompt": full_prompt, "stream": False},
            timeout=timeout_s,
            headers=extra_headers,
        )
        response.raise_for_status()
        data = response.json()
        text = data.get("response", "")
        ollama_tokens = int(data.get("prompt_eval_count") or 0) + int(data.get("eval_count") or 0)
        parsed: dict = {}
        if parse_json and text:
            json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
            raw = json_match.group(1) if json_match else text
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                parsed = {}
        return ModelResponse(text=text, cost_usd=0.0, tokens_used=ollama_tokens, data=parsed)

    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError) as exc:
        # Fallback to Gemini is disabled until Ollama is reachable from Cloud Run.
        # OLLAMA_HOST = http://localhost:11434 is the container loopback — it never
        # resolves in Cloud Run.  Falling back silently burns Gemini Flash free-tier
        # quota (20 req/day) causing 429s in chat_respond.  Fail fast instead so the
        # error surfaces clearly and no Gemini tokens are wasted.
        # Re-enable by setting LOCAL_MODEL_FALLBACK_DISABLED = false in settings.yaml
        # once a network-reachable OLLAMA_HOST is configured.
        global _ollama_fallback_count
        with _ollama_fallback_lock:
            _ollama_fallback_count += 1
            current_count = _ollama_fallback_count
        logger.error(
            "Ollama unreachable (%s) at %s for model '%s'. "
            "Fallback to Gemini is DISABLED. Fix OLLAMA_HOST in Secret Manager. "
            "Total failure count this session: %d.",
            type(exc).__name__,
            host,
            ollama_model,
            current_count,
        )
        raise RuntimeError(
            f"LOCAL_MODEL unavailable ({type(exc).__name__}) and fallback is disabled. "
            f"Configure a network-reachable OLLAMA_HOST. host={host!r}"
        ) from exc


def _notify_non_ollama_call(model: str, settings: Any) -> None:
    """Fire a Google Chat alert when a non-Ollama model is about to be called.

    Rate-limited to one alert per *model* per _GEMINI_ALERT_INTERVAL_SECONDS so
    high-frequency callers (heartbeats, distillation) don't flood the Chat space.
    Failures are silently swallowed — the alert must never block the model call.

    Args:
        model:    The resolved model string being invoked (e.g. ``"gemini-2.5-flash"``)
        settings: Loaded Settings object (used for owner_space and GCP_PROJECT_ID).
    """
    now = time.time()
    with _gemini_alert_lock:
        last = _gemini_last_alert.get(model, 0.0)
        if now - last < _GEMINI_ALERT_INTERVAL_SECONDS:
            return
        _gemini_last_alert[model] = now

    owner_space: str = getattr(getattr(settings, "chat", None), "owner_space", "") or ""
    if not owner_space:
        logger.warning(
            "NON_OLLAMA_LLM_CALL: model=%r — chat.owner_space not configured, "
            "cannot send Chat alert.",
            model,
        )
        return

    try:
        from tools.google_chat import send_message

        send_message(
            owner_space,
            f"⚠️ NON-OLLAMA LLM CALL\n"
            f"Model: {model}\n"
            f"OLLAMA_HOST is unreachable from Cloud Run. "
            f"Fix Secret Manager OLLAMA_HOST to point at a network-reachable endpoint "
            f"and redeploy. Until then, every LOCAL_MODEL task will fail.",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "NON_OLLAMA_LLM_CALL: model=%r — Chat notification failed (non-fatal): %s",
            model,
            exc,
        )


def _call_model_gemini(
    prompt: str,
    model: str,
    system_prompt: str,
    parse_json: bool,
    settings: Any,
    image_bytes: bytes | None = None,
) -> ModelResponse:
    """Calls google.genai via the Google AI Studio free tier API key.

    The client is initialised exclusively with the GEMINI_API_KEY secret — no
    Vertex AI / Discovery Engine endpoint is involved, keeping all traffic inside
    the AI Studio free tier quota.  If the secret is unavailable the call fails
    fast with a RuntimeError rather than silently re-routing to Vertex AI.

    When image_bytes is provided the request is sent as a multimodal
    (vision + text) call.  DEEP_MODEL is recommended for image inputs — Pro
    models have higher accuracy on visual content.  Multimodal calls consume
    significantly more tokens than text-only calls; the caller is responsible
    for budget monitoring and logging.

    Args:
        prompt:        User-turn content (may include prepended web context).
        model:         Resolved model alias string (e.g. "gemini-2.5-flash").
        system_prompt: Optional system instruction prefix.
        parse_json:    If True, attempt to extract and parse JSON from response.
        settings:      Loaded Settings object from config.get_settings().
        image_bytes:   Optional raw image bytes (JPEG/PNG). When present the
                       request is sent as a multimodal image+text payload.

    Returns:
        ModelResponse with text, cost_usd computed from token counts using
        settings.models pricing fields, and tokens_used for usage monitoring.

    Raises:
        RuntimeError: If GEMINI_API_KEY cannot be retrieved from Secret Manager.
        google.api_core.exceptions.ResourceExhausted: Re-raised after logging when
            the AI Studio free quota (429) is hit.
    """
    import google.genai as genai
    from google.api_core import exceptions as gapi_exc
    from google.genai import types as genai_types

    from tools.circuit_breaker import CircuitOpenError
    from tools.circuit_breaker import check as cb_check
    from tools.circuit_breaker import record_failure as cb_failure
    from tools.circuit_breaker import record_success as cb_success
    from tools.secrets import get_secret

    # ── Circuit breaker guards ────────────────────────────────────────────────
    # Two independent circuits:
    #   gemini/<model> — per-model quota (429). Trips on first exhaustion,
    #                    opens for 1 h. Prevents burning remaining daily quota.
    #   gemini-api-key — key validity (400). Trips on first bad-key error,
    #                    opens for 24 h. No calls wasted until key is replaced.
    _CB_AGENT = "agents"
    _cb_model_key = f"gemini/{model}"
    _cb_apikey_key = "gemini-api-key"

    try:
        cb_check(_CB_AGENT, _cb_model_key, failure_threshold=1, cooldown_seconds=3600)
    except CircuitOpenError as exc:
        raise RuntimeError(f"Gemini quota circuit OPEN — {exc}") from exc

    try:
        cb_check(_CB_AGENT, _cb_apikey_key, failure_threshold=1, cooldown_seconds=86400)
    except CircuitOpenError as exc:
        raise RuntimeError(f"Gemini API key circuit OPEN — {exc}") from exc

    try:
        api_key = get_secret("GEMINI_API_KEY", settings.GCP_PROJECT_ID)
    except Exception as exc:
        raise RuntimeError(
            f"GEMINI_API_KEY unavailable — cannot call Gemini. "
            f"Ensure the secret exists in project '{settings.GCP_PROJECT_ID}'. "
            f"Original error: {exc}"
        ) from exc

    client = genai.Client(api_key=api_key)
    full_prompt = f"System: {system_prompt}\n\n{prompt}" if system_prompt else prompt

    # ── Thinking config ───────────────────────────────────────────────────────
    # Gemini 2.5 Flash has dynamic thinking enabled by default — billed at
    # $3.50/1M (≈6× the $0.60/1M non-thinking output rate).  FAST_MODEL disables
    # thinking (budget=0) by default to prevent surprise spend.  Set
    # FAST_MODEL_THINKING_BUDGET: -1 in settings.yaml to re-enable dynamic thinking.
    # DEEP_MODEL keeps dynamic thinking (-1) for complex multi-step reasoning.
    _thinking_budget = (
        int(getattr(settings.models, "DEEP_MODEL_THINKING_BUDGET", -1))
        if model == settings.models.DEEP_MODEL
        else int(getattr(settings.models, "FAST_MODEL_THINKING_BUDGET", 0))
    )
    _gen_config = genai_types.GenerateContentConfig(
        thinking_config=genai_types.ThinkingConfig(thinking_budget=_thinking_budget)
    )

    try:
        if image_bytes is not None:
            # Detect format from magic bytes (imghdr is deprecated in 3.11+)
            if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
                detected_mime = "image/png"
            elif image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
                detected_mime = "image/webp"
            elif image_bytes[:6] in (b"GIF87a", b"GIF89a"):
                detected_mime = "image/gif"
            else:
                detected_mime = "image/jpeg"  # JPEG or unknown — default
            contents = [
                genai_types.Part.from_bytes(data=image_bytes, mime_type=detected_mime),
                genai_types.Part.from_text(text=full_prompt),
            ]
            response = client.models.generate_content(
                model=model, contents=contents, config=_gen_config
            )
        else:
            response = client.models.generate_content(
                model=model, contents=full_prompt, config=_gen_config
            )
    except gapi_exc.ResourceExhausted as exc:
        # Distinguish monthly spending cap (won't clear without manual action at
        # https://ai.studio/spend) from a per-minute/per-day rate limit.
        # Monthly cap: reset and re-trip the circuit with a 48 h cooldown so the
        # same container doesn't retry.  The Pub/Sub handler also returns 204
        # (ack) to prevent infinite re-delivery across container instances.
        from tools.circuit_breaker import reset as cb_reset

        _exc_str = str(exc).lower()
        _is_monthly_cap = "spending cap" in _exc_str or "monthly" in _exc_str
        if _is_monthly_cap:
            cb_reset(_CB_AGENT, _cb_model_key)
            cb_failure(_CB_AGENT, _cb_model_key, failure_threshold=1, cooldown_seconds=48 * 3600)
            logger.error(
                "Gemini MONTHLY SPENDING CAP exceeded for model '%s'. Circuit open for 48 h. "
                "Go to https://ai.studio/spend to raise or remove the cap. Error: %s",
                model,
                exc,
            )
        else:
            cb_failure(_CB_AGENT, _cb_model_key, failure_threshold=1, cooldown_seconds=3600)
            logger.warning(
                "Gemini quota exhausted (429) for model '%s'. Circuit open for 1 h. "
                "No further calls to this model until cooldown expires. Error: %s",
                model,
                exc,
            )
        raise
    except gapi_exc.InvalidArgument as exc:
        cb_failure(_CB_AGENT, _cb_apikey_key, failure_threshold=1, cooldown_seconds=86400)
        logger.error(
            "Gemini API key invalid (400). Circuit open for 24 h. "
            "Replace GEMINI_API_KEY in Secret Manager to recover. Error: %s",
            exc,
        )
        raise RuntimeError(f"Gemini API key invalid — replace GEMINI_API_KEY: {exc}") from exc

    cb_success(_CB_AGENT, _cb_model_key)
    cb_success(_CB_AGENT, _cb_apikey_key)

    text = response.text or ""

    usage = getattr(response, "usage_metadata", None)
    input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
    output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
    thinking_tokens = int(getattr(usage, "thoughts_token_count", 0) or 0)
    tokens_used = int(getattr(usage, "total_token_count", 0) or 0) or (
        input_tokens + output_tokens + thinking_tokens
    )

    # Compute cost from token counts.  Thinking tokens are billed separately at
    # THINKING_PRICE_PER_M ($3.50/1M for Flash — ≈6× the $0.60/1M text output rate).
    # Omitting them caused severe cost under-reporting when thinking was enabled by
    # default on Gemini 2.5 Flash, burning through the monthly spending cap.
    if model == settings.models.DEEP_MODEL:
        input_price = settings.models.DEEP_MODEL_INPUT_PRICE_PER_M
        output_price = settings.models.DEEP_MODEL_OUTPUT_PRICE_PER_M
        thinking_price = float(getattr(settings.models, "DEEP_MODEL_THINKING_PRICE_PER_M", 3.50))
    else:
        input_price = settings.models.FAST_MODEL_INPUT_PRICE_PER_M
        output_price = settings.models.FAST_MODEL_OUTPUT_PRICE_PER_M
        thinking_price = float(getattr(settings.models, "FAST_MODEL_THINKING_PRICE_PER_M", 3.50))
    cost_usd = (
        input_tokens * input_price + output_tokens * output_price + thinking_tokens * thinking_price
    ) / 1_000_000

    parsed: dict = {}
    if parse_json and text:
        json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        raw = json_match.group(1) if json_match else text
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            parsed = {}

    return ModelResponse(text=text, cost_usd=cost_usd, tokens_used=tokens_used, data=parsed)


# ── Code safety gate ──────────────────────────────────────────────────────────

_BLOCKED_BUILTINS = {"exec", "eval", "compile", "__import__", "breakpoint"}
_BLOCKED_PATTERNS = [
    "os.system",
    "os.popen",
    "subprocess.call",
    "subprocess.run",
    "subprocess.Popen",
    "__builtins__",
    "ctypes.",
    "socket.connect",
    "pickle.loads",
    "pickle.load",
]
_ALLOWED_IMPORTS = {
    "datetime",
    "json",
    "math",
    "re",
    "uuid",
    "hashlib",
    "time",
    "typing",
    "dataclasses",
    "collections",
    "functools",
    "itertools",
    "pathlib",
    "enum",
    "abc",
    "copy",
    "textwrap",
    "google.cloud.bigquery",
    "google.cloud.logging",
    "google.cloud.pubsub",
    "google.cloud.secretmanager",
    "google.cloud.storage",
    "google.adk",
    "google.genai",
    "google.auth",
    "gspread",
    "pydantic",
    "yaml",
    "config",
    "models",
    "tools",
    "agents",
    "langgraph",
}


def validate_code_safety(code: str) -> dict[str, Any]:
    """
    Gate 1 + 2 combined static-analysis check for agent-generated code.
    Returns {"passed": bool, "reason": str}.

    Called by nexus_prime.propose_gate before submitting to Approval Gate.
    Blocked code never reaches the Gate — hard stop is triggered instead.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return {"passed": False, "reason": f"SyntaxError: {exc}"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_BUILTINS:
                return {"passed": False, "reason": f"Blocked built-in: {node.func.id}"}
            if isinstance(node.func, ast.Attribute) and node.func.attr in _BLOCKED_BUILTINS:
                return {"passed": False, "reason": f"Blocked built-in (attr): {node.func.attr}"}

        if isinstance(node, ast.Import):
            for alias in node.names:
                if not any(
                    alias.name == a or alias.name.startswith(a + ".") for a in _ALLOWED_IMPORTS
                ):
                    return {"passed": False, "reason": f"Unapproved import: {alias.name}"}

        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module:
                for alias in node.names:
                    full_name = f"{module}.{alias.name}" if alias.name != "*" else module
                    if not any(
                        full_name == a or full_name.startswith(a + ".") for a in _ALLOWED_IMPORTS
                    ):
                        return {"passed": False, "reason": f"Unapproved import: {full_name}"}

    for pattern in _BLOCKED_PATTERNS:
        if pattern in code:
            return {"passed": False, "reason": f"Blocked pattern: {pattern}"}

    return {"passed": True, "reason": ""}


# ── Proposal coherence gate ───────────────────────────────────────────────────

_MIN_ISSUE_LENGTH = 20  # chars — guards against blank or one-word issue descriptions

_COHERENCE_PROMPT_TEMPLATE = """\
You are a code proposal quality reviewer for an AI agent system.
A GAOS agent has submitted the following proposal for human approval.

Issue description: {issue}
Trigger reason: {trigger_reason}
Stopping constraint: {stopping_constraint}
Proposed code excerpt (first 500 chars): {proposed_code_excerpt}

Determine whether this proposal is coherent and warrants human review.
Answer ONLY with a JSON object:
  {{"coherent": true or false, "reason": "<one sentence, max 120 chars>"}}

A proposal is incoherent if:
- The issue description is vague, generic, or a placeholder
- The description does not explain why human approval is needed
- The fields appear to be template text rather than a real problem
- The proposed code (if present) does not relate to the stated issue
"""


def _validate_proposal_coherence(
    proposal: Any,
    project_id: str,
) -> dict[str, Any]:
    """
    Quality gate for Approval Gate proposals — runs before Agent_Approvals write.

    Performs two checks:
    1. Structural: issue is non-trivial (≥ ``_MIN_ISSUE_LENGTH`` chars) and
       ``stopping_constraint`` is populated whenever ``proposed_code`` is present.
    2. Semantic (LOCAL_MODEL): the proposal fields describe a real, coherent
       problem that warrants human review.

    This is a *warning* gate — the caller decides whether to block or log.
    The function never raises; it returns a result dict even on LLM failure.

    Args:
        proposal: ApprovalProposal about to be written to Agent_Approvals.
        project_id: Active project namespace (used for model cost logging).

    Returns:
        dict with keys:
            ``passed`` (bool) — False means the proposal needs owner attention.
            ``reason`` (str) — Explanation if not passed.
            ``warnings`` (list[str]) — Non-blocking notes even when passed.
    """
    from config import get_settings

    warnings_list: list[str] = []

    # ── Structural checks ─────────────────────────────────────────────────────
    issue_text: str = (getattr(proposal, "issue", "") or "").strip()
    if len(issue_text) < _MIN_ISSUE_LENGTH:
        return {
            "passed": False,
            "reason": (
                f"Issue description is too short or empty "
                f"(got {len(issue_text)} chars, min {_MIN_ISSUE_LENGTH})."
            ),
            "warnings": warnings_list,
        }

    has_code: bool = bool((getattr(proposal, "proposed_code", "") or "").strip())
    stopping: str = (getattr(proposal, "stopping_constraint", "") or "").strip()
    if has_code and not stopping:
        return {
            "passed": False,
            "reason": "stopping_constraint must be populated when proposed_code is present.",
            "warnings": warnings_list,
        }

    # ── Semantic coherence (LOCAL_MODEL) ──────────────────────────────────────
    settings = get_settings()
    try:
        prompt = _COHERENCE_PROMPT_TEMPLATE.format(
            issue=issue_text[:800],
            trigger_reason=(getattr(proposal, "trigger_reason", "") or "")[:200],
            stopping_constraint=stopping[:400],
            proposed_code_excerpt=(getattr(proposal, "proposed_code", "") or "")[:500],
        )
        resp = _call_model_ollama(prompt, settings.models.LOCAL_MODEL, "", True, settings)
        coherent: bool = bool((resp.data or {}).get("coherent", True))
        llm_reason: str = str((resp.data or {}).get("reason", ""))
        if not coherent:
            return {
                "passed": False,
                "reason": f"LOCAL_MODEL coherence check failed: {llm_reason}",
                "warnings": warnings_list,
            }
    except Exception as exc:
        warnings_list.append(f"Coherence check skipped (LOCAL_MODEL unavailable): {exc}")

    return {"passed": True, "reason": "", "warnings": warnings_list}


# ── Output coherence quality check ───────────────────────────────────────────

_OUTPUT_COHERENCE_PROMPT = """\
You are a quality assurance agent evaluating whether an AI agent's output \
actually addresses the stated goal.

Goal: {goal}

Output to evaluate:
{output}

Does the output meaningfully address the goal? Consider:
  - Relevance: Does the output pertain to the goal?
  - Completeness: Does the output address the main ask, even if not exhaustively?
  - Coherence: Is the output internally consistent and logically structured?

Return ONLY a JSON object:
{{"passed": true or false, "confidence": 0.0-1.0, "reason": "<one sentence, max 120 chars>"}}
"""


def validate_output_coherence(
    goal: str,
    output: str,
    agent_id: str,
    project_id: str,
) -> dict[str, Any]:
    """Run a LOCAL_MODEL semantic check to verify *output* addresses *goal*.

    Uses an offline model (Ollama) to avoid data egress for operational content.
    Gracefully degrades — if the model is unavailable, passes with a warning so
    the caller's main path is never blocked by a downed Ollama instance.

    Args:
        goal:       The original task objective or prompt.
        output:     The generated text to evaluate.
        agent_id:   Calling agent identifier (used for warning logs).
        project_id: The AOS project namespace.

    Returns:
        A dict with keys:
          - ``passed`` (bool): True if the output is coherent with the goal.
          - ``confidence`` (float): Model confidence score 0.0–1.0.
          - ``reason`` (str): One-sentence explanation.
    """
    from config import get_settings

    if not goal.strip() or not output.strip():
        return {"passed": False, "confidence": 0.0, "reason": "Goal or output is empty."}

    settings = get_settings()
    try:
        prompt = _OUTPUT_COHERENCE_PROMPT.format(
            goal=goal[:500],
            output=output[:1500],
        )
        resp = _call_model_ollama(prompt, settings.models.LOCAL_MODEL, "", True, settings)
        data = resp.data or {}
        passed = bool(data.get("passed", True))
        confidence = float(data.get("confidence", 0.5))
        reason = str(data.get("reason", ""))
        return {"passed": passed, "confidence": confidence, "reason": reason}
    except Exception as exc:
        _log_cloud(
            agent_id,
            project_id,
            "system",
            "",
            f"validate_output_coherence skipped (LOCAL_MODEL unavailable): {exc}",
            severity="WARNING",
        )
        return {"passed": True, "confidence": 0.5, "reason": f"Validation skipped: {exc}"}


# ── Self-evolution loop ───────────────────────────────────────────────────────

_EVOLUTION_MAX_ITERATIONS = 5
_EVOLUTION_MAX_TTL_S = 1800  # 30 minutes
_EVOLUTION_MAX_COST_USD = 5.0


def _run_evolution_loop(
    issue: str,
    agent_id: str,
    context: str = "",
) -> dict[str, Any]:
    """
    Write-Test-Refine loop per GAOS-Manager-Spec.md §13.1.

    Returns dict with keys:
        code, iterations, cost_usd, stopping_constraint, error_fingerprint
    where stopping_constraint is one of:
        "iteration_cap" | "ttl" | "cost_cap" | "no_progress" | "success"
    """
    from config import get_settings

    settings = get_settings()

    start_ts = time.time()
    total_cost = 0.0
    last_fp = ""
    code = ""

    for i in range(1, _EVOLUTION_MAX_ITERATIONS + 1):
        elapsed = time.time() - start_ts

        if elapsed > _EVOLUTION_MAX_TTL_S:
            return _evo_result(code, i - 1, total_cost, "ttl", last_fp)
        if total_cost > _EVOLUTION_MAX_COST_USD:
            return _evo_result(code, i - 1, total_cost, "cost_cap", last_fp)

        model = settings.models.FAST_MODEL if i <= 3 else settings.models.DEEP_MODEL
        prompt = (
            f"Agent: {agent_id}\n"
            f"Issue: {issue}\n"
            f"Context: {context}\n"
            f"Iteration: {i}/{_EVOLUTION_MAX_ITERATIONS}\n"
            f"Previous draft:\n```python\n{code}\n```\n\n"
            "Generate an improved Python implementation that resolves the issue. "
            "Return ONLY a JSON object with key 'code' containing the full Python source."
        )

        resp = _call_model(prompt, model=model, parse_json=True)
        total_cost += resp.cost_usd

        new_code = resp.data.get("code") or resp.text
        fp = hashlib.sha256(new_code.encode()).hexdigest()[:16]

        if fp == last_fp and i > 1:
            return _evo_result(code, i, total_cost, "no_progress", last_fp)

        code = new_code
        last_fp = fp

        safety = validate_code_safety(code)
        if safety["passed"]:
            return _evo_result(code, i, total_cost, "success", last_fp)

    return _evo_result(code, _EVOLUTION_MAX_ITERATIONS, total_cost, "iteration_cap", last_fp)


def _evo_result(code: str, iterations: int, cost: float, constraint: str, fp: str) -> dict:
    return {
        "code": code,
        "iterations": iterations,
        "cost_usd": round(cost, 6),
        "stopping_constraint": constraint,
        "error_fingerprint": fp,
    }


# ── Identity file loader ──────────────────────────────────────────────────────


def _load_context_trio() -> str:
    """
    Read the three Context Trio files from Docs/ and return them as a single
    concatenated string for inclusion in every agent's system prompt.

    Files loaded (in order):
        Docs/about-me.md          — owner business context (The Compass)
        Docs/brand-voice.md       — communication standards (The Persona)
        Docs/working-preferences.md — operational rules (The Constitution)

    Returns:
        Concatenated Markdown string, or an empty string if all three files
        are missing (e.g., during unit tests without the full repo mounted).
    """
    sections: list[str] = []
    for filename in _CONTEXT_TRIO_FILES:
        path = _CONTEXT_TRIO_ROOT / filename
        if path.exists():
            sections.append(path.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(sections)


def _load_identity_file(agent_name: str) -> str:
    """
    Read Docs/agents/<agent_name>.md and return its content, with the Context
    Trio (about-me.md, brand-voice.md, working-preferences.md) appended.

    The trio provides owner business context, brand voice, and operational
    rules to every agent without requiring per-orchestrator changes.

    Falls back gracefully if files are missing (e.g., in unit tests).
    """
    path = _DOCS_ROOT / f"{agent_name}.md"
    if not path.exists():
        identity = (
            f"# {agent_name.title()} Agent\n"
            f"(Identity file not found — ensure Docs/agents/{agent_name}.md exists in the container image.)\n"
        )
    else:
        identity = path.read_text(encoding="utf-8")

    trio = _load_context_trio()
    if trio:
        return f"{identity}\n\n---\n\n{trio}"
    return identity


# ── Cloud Logging helper ──────────────────────────────────────────────────────


def _log_cloud(
    agent_id: str,
    project_id: str,
    log_type: str,
    task_id: str,
    message: str,
    severity: str = "INFO",
    extra: dict | None = None,
) -> None:
    """
    Write one structured entry to Cloud Logging with the labels defined
    in GAOS-Manager-Spec.md §13.2.

    Silently no-ops on import failure (e.g., in unit tests without GCP creds).
    """
    try:
        from google.cloud import logging as gcloud_logging

        from config import get_settings

        settings = get_settings()

        client = gcloud_logging.Client(project=settings.GCP_PROJECT_ID)
        logger = client.logger("gaos-agents")
        payload: dict[str, Any] = {
            "message": message,
            "agent_id": agent_id,
            "project_id": project_id,
            "log_type": log_type,
            "task_id": task_id,
            **(extra or {}),
        }
        logger.log_struct(payload, severity=severity)
    except Exception:
        pass  # Never let logging failures crash the agent


# ── Dashboard heartbeat helper ────────────────────────────────────────────────

# Per-agent BQ write tracking for event-driven status_snapshots.
# _last_bq_heartbeat: monotonic timestamp of the last BQ write per agent.
# _last_bq_status:    status string that was written in that last write.
# Together these implement the rule: write to BQ only when status changes OR
# the keepalive interval has elapsed (liveness proof for gaos_doctor).
_last_bq_heartbeat: dict[str, float] = {}
_last_bq_status: dict[str, str] = {}


def _write_heartbeat(
    agent_id: str,
    project_id: str,
    status: str,
    objective: str,
    open_proposals: int,
    last_error: str,
    tab: str,
) -> None:
    """
    Write a status row to the agent's designated Sheet tab and — when the
    status has changed or the keepalive interval has elapsed — to BigQuery
    ``aos_logs.status_snapshots``.

    The Sheets write fires on every invocation for real-time dashboard
    currency. The BQ write is event-driven to minimise API call volume:

    * **Status change** — written immediately regardless of elapsed time.
    * **Keepalive** — written when ``settings.agents.heartbeat_bq_keepalive_seconds``
      (default 600 s) has elapsed since the last BQ write, even if status
      is unchanged. This satisfies ``gaos_doctor``'s 60-minute liveness
      threshold with 6× headroom.
    * **Duplicate suppression** — if status is unchanged AND the keepalive
      interval has not elapsed, no BQ write is issued.

    Silently swallows all errors so a transient API failure does not crash
    the agent loop.
    """
    row = {
        "timestamp": utcnow_iso(),
        "agent_id": agent_id,
        "project_id": project_id,
        "status": status,
        "current_objective": objective[:255],
        "open_proposals": open_proposals,
        "last_error": (last_error or "")[:512],
    }
    try:
        from tools.google_sheets import append_row

        append_row(tab, row, project_id)
    except Exception:
        pass

    # BQ write: fire on status change OR keepalive interval elapsed.
    from config import get_settings

    keepalive = get_settings().agents.heartbeat_bq_keepalive_seconds
    now = time.monotonic()
    status_changed = status != _last_bq_status.get(agent_id)
    keepalive_due = now - _last_bq_heartbeat.get(agent_id, 0.0) >= keepalive
    if status_changed or keepalive_due:
        try:
            from tools.bigquery import insert_row as _bq_insert

            _bq_insert("aos_logs.status_snapshots", row, project_id)
            # Advance tracking state only after a confirmed successful write.
            # If _bq_insert raises, the next invocation will retry.
            _last_bq_heartbeat[agent_id] = now
            _last_bq_status[agent_id] = status
        except Exception:
            pass
