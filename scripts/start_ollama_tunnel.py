"""Start a localtunnel for Ollama, update OLLAMA_HOST in Secret Manager, and self-heal on drops.

Requires:
  - Node.js + npx on PATH  (install from https://nodejs.org)
  - Ollama running locally on --port (default 11434)
  - gcloud credentials with Secret Manager write access

Usage:
    python scripts/start_ollama_tunnel.py
    python scripts/start_ollama_tunnel.py --port 11434 --project morphic-gaos-prod

The script:
  1. Spawns:  npx localtunnel --port <PORT>
  2. Waits for the "your url is: https://..." line
  3. Verifies Ollama is reachable through the tunnel
  4. Updates the OLLAMA_HOST secret in Secret Manager
  5. Blocks, streaming tunnel output
  6. On any crash or unexpected exit, waits --retry-delay seconds and restarts from step 1
     (loca.lt issues a new URL on each restart — Secret Manager is updated automatically)

To run at Windows login without a visible terminal window, register it with:
    powershell -ExecutionPolicy Bypass scripts\\register_ollama_tunnel_task.ps1

Note: localtunnel API clients must send  Bypass-Tunnel-Reminder: true  — this header
is added automatically by _call_model_ollama when the host contains '.loca.lt'.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import time

import httpx

_TUNNEL_URL_RE = re.compile(r"your url is:\s*(https?://\S+)", re.IGNORECASE)
_STARTUP_TIMEOUT_S = 60

log = logging.getLogger("ollama-tunnel")

_NODE_SEARCH_DIRS = [
    r"C:\Program Files\nodejs",
    r"C:\Program Files (x86)\nodejs",
    os.path.expandvars(r"%APPDATA%\npm"),
    os.path.expandvars(r"%ProgramFiles%\nodejs"),
]


def _ensure_node_on_path() -> None:
    """Inject common Node.js install dirs into PATH when running headless (e.g. Scheduled Task)."""
    existing = os.environ.get("PATH", "")
    additions = [d for d in _NODE_SEARCH_DIRS if os.path.isdir(d) and d not in existing]
    if additions:
        os.environ["PATH"] = ";".join(additions) + ";" + existing
        log.info("Added Node.js dirs to PATH: %s", ", ".join(additions))


def _configure_logging(log_file: str | None = None) -> None:
    """Configure root logger: always to stderr, optionally also to a file."""
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)
    if log_file:
        import os

        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


def _update_secret(url: str, project: str) -> None:
    """Push *url* to Secret Manager as OLLAMA_HOST.

    Args:
        url:     The public localtunnel URL to store.
        project: GCP project that owns the secret.
    """
    from google.api_core.exceptions import AlreadyExists
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{project}"
    secret_id = "OLLAMA_HOST"
    secret_name = f"{parent}/secrets/{secret_id}"

    try:
        client.create_secret(
            request={
                "parent": parent,
                "secret_id": secret_id,
                "secret": {"replication": {"automatic": {}}},
            }
        )
        log.info("Created secret %s in %s", secret_id, project)
    except AlreadyExists:
        pass

    resp = client.add_secret_version(
        request={"parent": secret_name, "payload": {"data": url.encode("utf-8")}}
    )
    log.info("OLLAMA_HOST → %r  (%s)", url, resp.name)


def _verify_tunnel(url: str, retries: int = 5, delay: float = 3.0) -> bool:
    """Probe Ollama /api/tags through the tunnel with retries.

    Args:
        url:     The public tunnel base URL.
        retries: Number of attempts before giving up.
        delay:   Seconds to wait between attempts.

    Returns:
        True when Ollama responds successfully, False after all retries fail.
    """
    for attempt in range(1, retries + 1):
        try:
            r = httpx.get(
                f"{url}/api/tags",
                timeout=10.0,
                headers={"Bypass-Tunnel-Reminder": "true"},
            )
            r.raise_for_status()
            log.info("Ollama reachable at %s", url)
            return True
        except Exception as exc:
            log.warning("Attempt %d/%d — tunnel not ready: %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(delay)
    return False


def _run_tunnel_once(cmd: list[str], no_secret: bool, project: str) -> None:
    """Spawn one localtunnel process, push the URL, drain output until it dies.

    Args:
        cmd:       Full command list to spawn (e.g. ["npx", "localtunnel", ...]).
        no_secret: When True, skip the Secret Manager update.
        project:   GCP project id for the secret update.

    Raises:
        FileNotFoundError: If npx is not on PATH.
        RuntimeError:      If the tunnel exits without ever printing a URL,
                           or if the URL is never seen within the startup timeout.
    """
    log.info("Spawning: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    tunnel_url: str | None = None
    deadline = time.monotonic() + _STARTUP_TIMEOUT_S

    for line in proc.stdout:  # type: ignore[union-attr]
        log.info("[lt] %s", line.rstrip())
        m = _TUNNEL_URL_RE.search(line)
        if m:
            tunnel_url = m.group(1).rstrip("/")
            break
        if time.monotonic() > deadline:
            proc.terminate()
            proc.wait()
            raise RuntimeError("Timed out waiting for localtunnel URL.")

    if not tunnel_url:
        proc.wait()
        raise RuntimeError("localtunnel exited before providing a URL.")

    log.info("Tunnel URL: %s", tunnel_url)
    time.sleep(2)  # let the tunnel stabilise

    ok = _verify_tunnel(tunnel_url)
    if not ok:
        log.warning(
            "Ollama did not respond through the tunnel — is Ollama running on the configured port?"
        )

    if not no_secret:
        _update_secret(tunnel_url, project)

    log.info(
        "Tunnel active. OLLAMA_HOST=%r — Secret Manager updated. "
        "Cloud Run picks the new URL on next _call_model_ollama invocation.",
        tunnel_url,
    )

    # Drain remaining output until the process exits
    for line in proc.stdout:  # type: ignore[union-attr]
        log.info("[lt] %s", line.rstrip())

    proc.wait()
    log.warning("localtunnel process exited with code %d.", proc.returncode)


def main() -> int:
    """Entry point — parse args and run the watchdog loop."""
    p = argparse.ArgumentParser(
        description="Start localtunnel for Ollama with auto-restart and Secret Manager sync."
    )
    p.add_argument("--port", type=int, default=11434, help="Local Ollama port (default: 11434)")
    p.add_argument("--project", default="morphic-gaos-prod", help="GCP project id")
    p.add_argument(
        "--no-secret",
        action="store_true",
        help="Skip Secret Manager update (useful for local testing)",
    )
    p.add_argument(
        "--retry-delay",
        type=float,
        default=10.0,
        metavar="SECONDS",
        help="Seconds to wait before restarting after a tunnel crash (default: 10)",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Run tunnel once and exit on crash instead of restarting (default: restart forever)",
    )
    p.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="Write logs to this file (in addition to stderr). Useful when running headless via pythonw.exe.",
    )
    args = p.parse_args()
    _configure_logging(args.log_file)

    _ensure_node_on_path()
    npx_path = shutil.which("npx")
    if not npx_path:
        log.error("npx not found on PATH. Install Node.js from https://nodejs.org")
        return 1
    log.info("Using npx at: %s", npx_path)
    cmd = [npx_path, "--yes", "localtunnel", "--port", str(args.port)]

    try:
        attempt = 0
        while True:
            attempt += 1
            log.info("--- Tunnel attempt %d ---", attempt)
            try:
                _run_tunnel_once(cmd, args.no_secret, args.project)
            except FileNotFoundError:
                log.error("npx not found. Install Node.js from https://nodejs.org")
                return 1
            except RuntimeError as exc:
                log.error("Tunnel error: %s", exc)

            if args.once:
                log.info("--once flag set — not restarting.")
                return 1

            log.info("Restarting in %.0f seconds …", args.retry_delay)
            time.sleep(args.retry_delay)

    except KeyboardInterrupt:
        log.info("Ctrl-C received — stopping.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
