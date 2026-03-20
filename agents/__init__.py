"""
agents/__init__.py — Shared utilities for all Morphic-G AOS agent orchestrators.

Provides:
  - ModelResponse and _call_model()   — thin wrapper over google.genai / Ollama
  - _call_model_ollama()              — Ollama HTTP routing with timeout + fallback
  - validate_code_safety()            — AST-based import + pattern gate
  - _run_evolution_loop()             — Write-Test-Refine loop (§13.1)
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Ollama fallback telemetry ────────────────────────────────────────────────

_ollama_fallback_lock = threading.Lock()
_ollama_fallback_count: int = 0


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
    return datetime.now(timezone.utc).isoformat()


def utcnow_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


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
      - On timeout or connection error, falls back to LOCAL_MODEL_FALLBACK via Gemini
      - image_bytes are not supported for Ollama; a warning is logged and bytes ignored.

    If model is a Gemini alias:
      - Calls google.genai with the GEMINI_API_KEY secret
      - When image_bytes is provided, sends a multimodal request (image + text).
        Use DEEP_MODEL for multimodal calls — Pro models have higher vision accuracy.
        Multimodal calls consume significantly more tokens; budget monitoring is
        the caller's responsibility.

    Args:
        prompt:        User-turn content.
        model:         Resolved model alias string (e.g. "ollama/llama3.1" or "gemini-2.0-flash").
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
    settings = get_settings()

    if web_access and model.startswith("ollama/"):
        from tools.web_search import web_search
        snippets = web_search(prompt)
        if snippets:
            prompt = f"Web search results for context:\n{snippets}\n\n---\n\n{prompt}"

    if model.startswith("ollama/"):
        if image_bytes is not None:
            logger.warning(
                "image_bytes provided for Ollama model '%s' — multimodal is not supported "
                "for local models. The image will be ignored.",
                model,
            )
        return _call_model_ollama(prompt, model, system_prompt, parse_json, settings)

    return _call_model_gemini(prompt, model, system_prompt, parse_json, settings, image_bytes)


def _call_model_ollama(
    prompt: str,
    model: str,
    system_prompt: str,
    parse_json: bool,
    settings: Any,
) -> ModelResponse:
    """
    Calls the local Ollama server. Falls back to LOCAL_MODEL_FALLBACK on any error.
    Model alias format: "ollama/<model-name>" e.g. "ollama/llama3.1"
    """
    import httpx

    ollama_model = model.split("/", 1)[1]  # strip "ollama/" prefix
    timeout_s = float(getattr(settings.models, "LOCAL_MODEL_TIMEOUT_SECONDS", 2))
    fallback_model = settings.models.LOCAL_MODEL_FALLBACK

    try:
        from tools.secrets import get_secret
        host = get_secret("OLLAMA_HOST", settings.GCP_PROJECT_ID).strip().rstrip("/")
    except Exception:
        host = "http://localhost:11434"

    full_prompt = f"System: {system_prompt}\n\n{prompt}" if system_prompt else prompt

    try:
        response = httpx.post(
            f"{host}/api/generate",
            json={"model": ollama_model, "prompt": full_prompt, "stream": False},
            timeout=timeout_s,
        )
        response.raise_for_status()
        data = response.json()
        text = data.get("response", "")
        parsed: dict = {}
        if parse_json and text:
            json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
            raw = json_match.group(1) if json_match else text
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                parsed = {}
        return ModelResponse(text=text, cost_usd=0.0, tokens_used=0, data=parsed)

    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError) as exc:
        global _ollama_fallback_count
        with _ollama_fallback_lock:
            _ollama_fallback_count += 1
            current_count = _ollama_fallback_count
        logger.warning(
            "Ollama unreachable (%s) at %s for model '%s' — falling back to %s. "
            "Total fallback count this session: %d.",
            type(exc).__name__,
            host,
            ollama_model,
            fallback_model,
            current_count,
        )
        return _call_model_gemini(prompt, fallback_model, system_prompt, parse_json, settings)


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
        ModelResponse with text, cost_usd=0.0 (AI Studio free tier), and
        tokens_used for usage monitoring.

    Raises:
        RuntimeError: If GEMINI_API_KEY cannot be retrieved from Secret Manager.
        google.api_core.exceptions.ResourceExhausted: Re-raised after logging when
            the AI Studio free quota (429) is hit.
    """
    import google.genai as genai
    from google.genai import types as genai_types
    from google.api_core import exceptions as gapi_exc

    from tools.secrets import get_secret

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
            response = client.models.generate_content(model=model, contents=contents)
        else:
            response = client.models.generate_content(model=model, contents=full_prompt)
    except gapi_exc.ResourceExhausted as exc:
        logger.warning(
            "Gemini AI Studio free-tier quota exhausted (429) for model '%s'. "
            "Request cannot be completed until the quota resets. Error: %s",
            model,
            exc,
        )
        raise

    text = response.text or ""

    usage = getattr(response, "usage_metadata", None)
    tokens_used = int(getattr(usage, "total_token_count", 0) or 0)

    parsed: dict = {}
    if parse_json and text:
        json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        raw = json_match.group(1) if json_match else text
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            parsed = {}

    return ModelResponse(text=text, cost_usd=0.0, tokens_used=tokens_used, data=parsed)


# ── Code safety gate ──────────────────────────────────────────────────────────

_BLOCKED_BUILTINS = {"exec", "eval", "compile", "__import__", "breakpoint"}
_BLOCKED_PATTERNS = [
    "os.system", "os.popen", "subprocess.call", "subprocess.run",
    "subprocess.Popen", "__builtins__", "ctypes.", "socket.connect",
    "pickle.loads", "pickle.load",
]
_ALLOWED_IMPORTS = {
    "datetime", "json", "math", "re", "uuid", "hashlib", "time",
    "typing", "dataclasses", "collections", "functools", "itertools",
    "pathlib", "enum", "abc", "copy", "textwrap",
    "google.cloud.bigquery", "google.cloud.logging", "google.cloud.pubsub",
    "google.cloud.secretmanager", "google.cloud.storage",
    "google.adk", "google.genai", "google.auth",
    "gspread", "pydantic", "yaml",
    "config", "models", "tools", "agents",
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
                    alias.name == a or alias.name.startswith(a + ".")
                    for a in _ALLOWED_IMPORTS
                ):
                    return {"passed": False, "reason": f"Unapproved import: {alias.name}"}

        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module:
                for alias in node.names:
                    full_name = f"{module}.{alias.name}" if alias.name != "*" else module
                    if not any(
                        full_name == a or full_name.startswith(a + ".")
                        for a in _ALLOWED_IMPORTS
                    ):
                        return {"passed": False, "reason": f"Unapproved import: {full_name}"}

    for pattern in _BLOCKED_PATTERNS:
        if pattern in code:
            return {"passed": False, "reason": f"Blocked pattern: {pattern}"}

    return {"passed": True, "reason": ""}


# ── Self-evolution loop ───────────────────────────────────────────────────────

_EVOLUTION_MAX_ITERATIONS = 5
_EVOLUTION_MAX_TTL_S = 1800   # 30 minutes
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
    Write a status row to the agent's designated Sheet tab.
    Must be called at the end of every work cycle (within 60 seconds).
    Silently swallows Sheet errors so a transient API failure does not
    crash the agent loop.
    """
    try:
        from tools.google_sheets import append_row
        row = {
            "timestamp": utcnow_iso(),
            "agent_id": agent_id,
            "project_id": project_id,
            "status": status,
            "current_objective": objective[:255],
            "open_proposals": open_proposals,
            "last_error": (last_error or "")[:512],
        }
        append_row(tab, row, project_id)
    except Exception:
        pass
