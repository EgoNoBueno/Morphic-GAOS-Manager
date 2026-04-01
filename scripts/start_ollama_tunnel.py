"""Start a localtunnel for Ollama, update OLLAMA_HOST in Secret Manager, and self-heal on drops.

Requires:
  - Node.js + npx on PATH  (install from https://nodejs.org)
  - Ollama running locally on --port (default 11434)
  - gcloud credentials with Secret Manager write access

Usage:
    python scripts/start_ollama_tunnel.py
    python scripts/start_ollama_tunnel.py --port 11434 --project morphic-gaos-prod

The script:
  1. Spawns:  npx localtunnel --port <PORT> --subdomain <SUBDOMAIN>
  2. Waits for the "your url is: https://..." line
  3. Verifies Ollama is reachable through the tunnel
  4. Updates OLLAMA_HOST in Secret Manager (only when the URL has changed)
  5. Runs a background health-check thread that polls /api/tags every --health-interval
     seconds and kills the process if two consecutive checks fail (catches silent 502s)
  6. Blocks, streaming tunnel output
  7. On any crash or unexpected exit, waits --retry-delay seconds and restarts from step 1

With --subdomain (default: gaos-ollama), the URL is always https://gaos-ollama.loca.lt.
Secret Manager is only written when the URL differs from the stored value, so restarts
don't create new secret versions.

To run at Windows login without a visible terminal window, register it with:
    powershell -ExecutionPolicy Bypass scripts\\register_ollama_tunnel_task.ps1

Note: localtunnel API clients must send  Bypass-Tunnel-Reminder: true  — this header
is added automatically by _call_model_ollama when the host contains '.loca.lt'.
"""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import httpx

_TUNNEL_URL_RE = re.compile(r"your url is:\s*(https?://\S+)", re.IGNORECASE)
_STARTUP_TIMEOUT_S = 60
_PID_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "ollama-tunnel.pid"
)

log = logging.getLogger("ollama-tunnel")


def _acquire_pid_lock() -> bool:
    """Atomically write the current PID to the lock file if no other instance is running.

    Uses O_CREAT | O_EXCL for an atomic create (avoids the TOCTOU race where two
    processes both pass the "file missing" check before either writes its PID).

    Returns:
        True if the lock was acquired (safe to proceed).
        False if another live instance is already running (caller should exit).
    """
    import ctypes

    os.makedirs(os.path.dirname(_PID_FILE), exist_ok=True)

    def _is_pid_alive(pid: int) -> bool:
        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False

    while True:
        try:
            # Atomic create: fails with FileExistsError if file already exists
            fd = os.open(_PID_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            atexit.register(lambda: os.unlink(_PID_FILE) if os.path.exists(_PID_FILE) else None)
            return True
        except FileExistsError:
            # File exists — check if the stored PID is still alive
            try:
                with open(_PID_FILE) as _f:
                    existing_pid = int(_f.read().strip())
            except (ValueError, OSError):
                # Unreadable/empty file — remove and retry the atomic create
                try:
                    os.unlink(_PID_FILE)
                except OSError:
                    pass
                continue
            if _is_pid_alive(existing_pid):
                log.error(
                    "Another tunnel watchdog is already running (PID %d). "
                    "Kill it first or delete %s to override.",
                    existing_pid,
                    _PID_FILE,
                )
                return False
            # Stale PID (process dead) — remove and retry
            try:
                os.unlink(_PID_FILE)
            except OSError:
                pass
            continue


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


def _kill_tree(pid: int) -> None:
    """Kill a process and all its descendants using taskkill /F /T (Windows).

    proc.terminate() only kills the direct child (cmd.exe when npx.CMD is used).
    Without /T the grandchildren (node.exe localtunnel workers) are left as orphans,
    holding the loca.lt subdomain and preventing re-claim on restart.

    Args:
        pid: Root process ID of the tree to terminate.
    """
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            creationflags=0x08000000,  # CREATE_NO_WINDOW — no console flash on Windows
        )
        log.info("Killed process tree rooted at PID %d.", pid)
    except Exception as exc:
        log.warning("taskkill /T for PID %d failed: %s", pid, exc)


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
    # Suppress httpx INFO-level request logs — they appear as "[lt] HTTP Request: GET ..."
    # which implies they are tunnel output but are actually httpx instrumentation noise.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _current_secret_url(project: str) -> str | None:
    """Read the current OLLAMA_HOST value from Secret Manager, or None on any error.

    Args:
        project: GCP project that owns the secret.

    Returns:
        The stored URL string, or None if the secret doesn't exist or can't be read.
    """
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project}/secrets/OLLAMA_HOST/versions/latest"
        resp = client.access_secret_version(request={"name": name})
        return resp.payload.data.decode("utf-8").strip()
    except Exception:
        return None


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


def _verify_tunnel(url: str, retries: int = 5, initial_delay: float = 2.0) -> bool:
    """Probe Ollama /api/tags through the tunnel with retries and exponential backoff.

    Uses a short initial timeout that doubles on each failure: 2s → 4s → 8s → ...
    This lets fast tunnel startups proceed quickly while still handling slow ones.

    Args:
        url:           The public tunnel base URL.
        retries:       Number of attempts before giving up.
        initial_delay: Seconds to wait after the first failure (doubles each attempt).

    Returns:
        True when Ollama responds successfully, False after all retries fail.
    """
    backoff = initial_delay
    for attempt in range(1, retries + 1):
        try:
            r = httpx.get(
                f"{url}/api/tags",
                timeout=8.0,
                headers={"Bypass-Tunnel-Reminder": "true"},
            )
            r.raise_for_status()
            log.info("Ollama reachable at %s", url)
            return True
        except Exception as exc:
            log.warning("Attempt %d/%d — tunnel not ready: %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)  # cap at 30s
    return False


def _run_tunnel_once(
    cmd: list[str],
    no_secret: bool,
    project: str,
    health_interval: float = 60.0,
) -> None:
    """Spawn one localtunnel process, push the URL, drain output until it dies.

    A background health-check thread polls Ollama every *health_interval* seconds.
    If two consecutive checks fail the tunnel process is killed so the watchdog
    loop can restart it immediately (catches silent 502 / Bad Gateway states).

    Args:
        cmd:             Full command list to spawn (e.g. ["npx", "localtunnel", ...]).
        no_secret:       When True, skip the Secret Manager update.
        project:         GCP project id for the secret update.
        health_interval: Seconds between /api/tags health polls (default 60).

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
    requested_subdomain = cmd[cmd.index("--subdomain") + 1] if "--subdomain" in cmd else None
    granted_subdomain = tunnel_url.split("//")[-1].split(".")[0]
    subdomain_mismatch = requested_subdomain and granted_subdomain != requested_subdomain
    if subdomain_mismatch:
        log.warning(
            "Requested subdomain was not granted by loca.lt — got %r instead of %r. "
            "This probably means another process already claimed the subdomain. "
            "Skipping Secret Manager update to avoid publishing an unstable random URL.",
            tunnel_url,
            f"https://{requested_subdomain}.loca.lt",
        )
    time.sleep(2)  # let the tunnel stabilise

    ok = _verify_tunnel(tunnel_url)
    if not ok:
        log.warning(
            "Ollama did not respond through the tunnel — is Ollama running on the configured port?"
            " Skipping Secret Manager update to avoid publishing a dead endpoint."
        )

    if ok and not no_secret and not subdomain_mismatch:
        current = _current_secret_url(project)
        if current == tunnel_url:
            log.info("OLLAMA_HOST already set to %r — skipping Secret Manager update.", tunnel_url)
        else:
            _update_secret(tunnel_url, project)

    log.info(
        "Tunnel active. OLLAMA_HOST=%r — Cloud Run picks the new URL on next "
        "_call_model_ollama invocation.",
        tunnel_url,
    )

    # ── Health-check thread ────────────────────────────────────────────────
    # Polls /api/tags every health_interval seconds. Two consecutive failures
    # kill the process so the outer watchdog loop restarts it immediately.
    # This catches the silent 502 / Bad Gateway state localtunnel enters without
    # crashing the process.
    stop_health = threading.Event()

    def _health_loop() -> None:
        consecutive_failures = 0
        while not stop_health.is_set():
            stop_health.wait(timeout=health_interval)
            if stop_health.is_set():
                break
            try:
                r = httpx.get(
                    f"{tunnel_url}/api/tags",
                    timeout=10.0,
                    headers={"Bypass-Tunnel-Reminder": "true"},
                )
                r.raise_for_status()
                consecutive_failures = 0
                log.info("[health] Ollama OK via %s", tunnel_url)
            except Exception as exc:
                consecutive_failures += 1
                log.warning("[health] Check %d/2 failed: %s", consecutive_failures, exc)
                if consecutive_failures >= 2:
                    log.error(
                        "[health] 2 consecutive failures — killing tunnel process to force restart."
                    )
                    _kill_tree(proc.pid)
                    return

    health_thread = threading.Thread(target=_health_loop, daemon=True, name="tunnel-health")
    health_thread.start()

    # Drain remaining output until the process exits
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            log.info("[lt] %s", line.rstrip())
        proc.wait()
    finally:
        stop_health.set()
        _kill_tree(proc.pid)  # ensure entire cmd.exe → node.exe chain is dead
    log.warning("localtunnel process exited with code %d.", proc.returncode)


def main() -> int:
    """Entry point — parse args and run the watchdog loop."""
    p = argparse.ArgumentParser(
        description="Start localtunnel for Ollama with auto-restart and Secret Manager sync."
    )
    p.add_argument("--port", type=int, default=11434, help="Local Ollama port (default: 11434)")
    p.add_argument("--project", default="morphic-gaos-prod", help="GCP project id")
    p.add_argument(
        "--subdomain",
        default="gaos-ollama",
        metavar="NAME",
        help="Request a fixed localtunnel subdomain (default: gaos-ollama → https://gaos-ollama.loca.lt). "
        "A stable subdomain means Secret Manager is only updated when the URL actually changes.",
    )

    def _positive_float(value: str) -> float:
        fval = float(value)
        if fval <= 0:
            raise argparse.ArgumentTypeError(
                f"--health-interval must be a positive number (got {value!r}); "
                "_health_loop() uses this as Event.wait(timeout=...) and will spin on <= 0."
            )
        return fval

    p.add_argument(
        "--health-interval",
        type=_positive_float,
        default=30.0,
        metavar="SECONDS",
        help="Seconds between health-check polls of /api/tags through the tunnel (default: 30). "
        "Must be > 0. Two consecutive failures kill the tunnel process so the watchdog restarts it.",
    )
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

    if not _acquire_pid_lock():
        return 1
    log.info("PID lock acquired (%d) — single instance confirmed.", os.getpid())

    _ensure_node_on_path()
    npx_path = shutil.which("npx")
    if not npx_path:
        log.error("npx not found on PATH. Install Node.js from https://nodejs.org")
        return 1
    log.info("Using npx at: %s", npx_path)
    cmd = [
        npx_path,
        "--yes",
        "localtunnel",
        "--port",
        str(args.port),
        "--subdomain",
        args.subdomain,
    ]
    log.info(
        "Requesting subdomain: %s (URL will be https://%s.loca.lt)", args.subdomain, args.subdomain
    )

    try:
        attempt = 0
        while True:
            attempt += 1
            log.info("--- Tunnel attempt %d ---", attempt)
            try:
                _run_tunnel_once(cmd, args.no_secret, args.project, args.health_interval)
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
