"""
agents/__init__.py — Shared utilities for all Morphic-G AOS agent orchestrators.

Provides:
  - ModelResponse and _call_model()   — thin wrapper over google.genai
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
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Timestamp helpers ─────────────────────────────────────────────────────────

_DOCS_ROOT = Path(__file__).parent.parent / "Docs" / "agents"


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
) -> ModelResponse:
    """
    Thin wrapper around google.genai.  API key is read from Secret Manager
    at call-time (not module init) so it respects per-project scoping.

    Args:
        prompt:        User-turn content.
        model:         Resolved model alias string (e.g. "gemini-2.0-flash").
        system_prompt: Optional system instruction prefix.
        parse_json:    If True, attempt to extract and parse JSON from response.

    Returns:
        ModelResponse with text, rough cost_usd, and parsed data dict.
    """
    import google.genai as genai
    from config import get_settings

    settings = get_settings()

    try:
        from tools.secrets import get_secret
        api_key = get_secret("GEMINI_API_KEY", settings.GCP_PROJECT_ID)
        client = genai.Client(api_key=api_key)
    except Exception:
        # Fall back to Application Default Credentials / Vertex AI
        client = genai.Client(
            vertexai=True,
            project=settings.GCP_PROJECT_ID,
            location=settings.memory_bank.region,
        )

    full_prompt = f"System: {system_prompt}\n\n{prompt}" if system_prompt else prompt

    response = client.models.generate_content(model=model, contents=full_prompt)
    text = response.text or ""

    usage = getattr(response, "usage_metadata", None)
    tokens_used = int(getattr(usage, "total_token_count", 0) or 0)
    cost_usd = tokens_used * 1e-6  # conservative placeholder

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
                if not any(alias.name.startswith(a) for a in _ALLOWED_IMPORTS):
                    return {"passed": False, "reason": f"Unapproved import: {alias.name}"}

        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module and not any(module.startswith(a) for a in _ALLOWED_IMPORTS):
                return {"passed": False, "reason": f"Unapproved import: {module}"}

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

def _load_identity_file(agent_name: str) -> str:
    """
    Read Docs/agents/<agent_name>.md and return its content.
    Falls back to an empty string if the file is missing (logs a warning).
    """
    path = _DOCS_ROOT / f"{agent_name}.md"
    if not path.exists():
        return f"# {agent_name.title()} Agent\n(Identity file not found — ensure Docs/agents/{agent_name}.md exists in the container image.)\n"
    return path.read_text(encoding="utf-8")


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
