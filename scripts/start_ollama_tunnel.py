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

With --subdomain (default: morphic-gaos-ollama), the URL is always https://morphic-gaos-ollama.loca.lt.
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


def _stop_watchdog() -> int:
    """Stop a running tunnel watchdog process and all its node.exe children.

    Reads the PID from the lock file, kills the watchdog (which triggers its
    atexit handler to clean up children), then force-kills any remaining
    localtunnel node.exe processes and removes the lock file.

    Returns:
        0 on success or if no watchdog was running.
        1 if the PID file was found but the kill failed.
    """
    if not os.path.exists(_PID_FILE):
        log.info("No PID file found — watchdog is not running (or already stopped).")
        _kill_orphaned_localtunnel_nodes()
        return 0

    try:
        with open(_PID_FILE) as f:
            pid = int(f.read().strip())
    except (ValueError, OSError) as exc:
        log.warning("Could not read PID file: %s — cleaning up anyway.", exc)
        _kill_orphaned_localtunnel_nodes()
        try:
            os.unlink(_PID_FILE)
        except OSError:
            pass
        return 0

    log.info("Stopping watchdog PID %d …", pid)
    kill_failed = False
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            creationflags=0x08000000,
        )
        if result.returncode != 0:
            log.warning("taskkill exited %d for PID %d.", result.returncode, pid)
            kill_failed = True
    except Exception as exc:
        log.warning("taskkill failed for PID %d: %s", pid, exc)
        kill_failed = True

    # Give the process tree a moment to die, then kill any surviving localtunnel nodes
    time.sleep(1.0)
    _kill_orphaned_localtunnel_nodes()

    try:
        os.unlink(_PID_FILE)
    except OSError:
        pass

    if kill_failed:
        log.info("Watchdog cleanup finished but taskkill reported failure.")
        return 1

    log.info("Watchdog stopped.")
    return 0


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


def _kill_orphaned_localtunnel_nodes() -> None:
    """Kill any node.exe processes running localtunnel that the current user owns.

    Called on startup before spawning a new tunnel so that stale node workers
    left by a previously crashed watchdog cannot hold the loca.lt subdomain.
    After killing, sleeps 3 seconds to allow loca.lt's server-side connection
    to clear before the new tunnel attempts to claim the same subdomain.
    """
    killed: list[int] = []

    def _get_localtunnel_pids() -> list[int]:
        """Return PIDs of node.exe processes with 'localtunnel' in their command line.

        Tries PowerShell Get-CimInstance first (works on all Windows 10/11 builds).
        Falls back to the deprecated wmic command if PowerShell is unavailable.
        """
        # Primary: PowerShell CIM (replaces deprecated wmic on Windows 11)
        try:
            ps_result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "Get-CimInstance Win32_Process -Filter \"name='node.exe'\" "
                    "| Where-Object { $_.CommandLine -like '*localtunnel*' } "
                    "| Select-Object -ExpandProperty ProcessId",
                ],
                capture_output=True,
                text=True,
                creationflags=0x08000000,
                timeout=10,
            )
            pids = [
                int(line.strip())
                for line in ps_result.stdout.splitlines()
                if line.strip().isdigit()
            ]
            if pids or ps_result.returncode == 0:
                return pids
        except Exception:
            pass

        # Fallback: deprecated wmic (still works on Win 10 / early Win 11)
        try:
            wmic_result = subprocess.run(
                [
                    "wmic",
                    "process",
                    "where",
                    "(name='node.exe' and commandline like '%localtunnel%')",
                    "get",
                    "ProcessId",
                    "/VALUE",
                ],
                capture_output=True,
                text=True,
                creationflags=0x08000000,
                timeout=10,
            )
            return [
                int(line.split("=", 1)[1].strip())
                for line in wmic_result.stdout.splitlines()
                if line.startswith("ProcessId=")
            ]
        except Exception:
            return []

    try:
        for pid_val in _get_localtunnel_pids():
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid_val)],
                    capture_output=True,
                    creationflags=0x08000000,
                )
                killed.append(pid_val)
            except (ValueError, OSError):
                pass
    except Exception as exc:
        log.debug("orphan-kill scan failed (non-fatal): %s", exc)
        return

    if killed:
        log.info("Killed %d orphaned localtunnel node.exe process(es): %s", len(killed), killed)
        log.info("Waiting 3 s for loca.lt server-side connection to clear…")
        time.sleep(3.0)


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

    The HTTP request timeout is fixed at 8 s per attempt. What doubles on each
    failure is the *sleep between attempts* (the retry delay): initial_delay →
    initial_delay * 2 → initial_delay * 4 → ..., capped at 30 s. This lets fast
    tunnel startups proceed with a short first wait while still giving slow ones
    enough time to stabilise.

    Args:
        url:           The public tunnel base URL.
        retries:       Number of attempts before giving up.
        initial_delay: Seconds to sleep after the first failure. Doubles on each
                       subsequent failure (min(backoff * 2, 30.0)).

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
    health_interval: float = 30.0,
) -> bool:
    """Spawn one localtunnel process, push the URL, drain output until it dies.

    A background health-check thread polls Ollama every *health_interval* seconds.
    If two consecutive checks fail the tunnel process is killed so the watchdog
    loop can restart it immediately (catches silent 502 / Bad Gateway states).

    Args:
        cmd:             Full command list to spawn (e.g. ["npx", "localtunnel", ...]).
        no_secret:       When True, skip the Secret Manager update.
        project:         GCP project id for the secret update.
        health_interval: Seconds between /api/tags health polls (default 30).

    Returns:
        True  — the tunnel ran and the process exited naturally (e.g. loca.lt
                server-side disconnect). The caller may reset its failure streak.
        False — the health-check thread killed the process due to two consecutive
                /api/tags failures (flapping / crash-loop). The caller must *not*
                reset consecutive_failures or alert_sent so that sustained
                flapping eventually triggers the alert email.

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
        creationflags=0x08000000,  # CREATE_NO_WINDOW — suppresses console flash when headless
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
            "Another process has claimed the preferred subdomain. "
            "Will publish this random URL to Secret Manager if Ollama responds. "
            "Stop and restart this script to attempt subdomain reclaim.",
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

    if ok and not no_secret:
        current = _current_secret_url(project)
        if current == tunnel_url:
            log.info("OLLAMA_HOST already set to %r — skipping Secret Manager update.", tunnel_url)
        else:
            if subdomain_mismatch:
                log.warning(
                    "Publishing random URL %r to Secret Manager — Cloud Run will use this URL "
                    "until the preferred subdomain is reclaimed. Kill and restart this script "
                    "to attempt subdomain reclaim.",
                    tunnel_url,
                )
            _update_secret(tunnel_url, project)

    log.info(
        "Tunnel active. OLLAMA_HOST=%r — Cloud Run picks the new URL on next "
        "_call_model_ollama invocation.",
        tunnel_url,
    )
    # ── Health-check thread ────────────────────────────────────────────────────
    # Polls /api/tags every health_interval seconds. Two consecutive failures
    # kill the process so the outer watchdog loop restarts it immediately.
    # This catches the silent 502 / Bad Gateway state localtunnel enters without
    # crashing the process.
    stop_health = threading.Event()
    # Tracks whether *this* thread killed the process — used to distinguish
    # a health-kill (flapping) from a natural process exit so the caller can
    # decide whether to reset consecutive_failures and alert_sent.
    health_killed = threading.Event()

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
                    health_killed.set()  # signal to _run_tunnel_once that this was a health-kill
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
    # Return False when the health thread killed the process — the caller must
    # not reset consecutive_failures/alert_sent in that case so flapping
    # accumulates toward the alert threshold.
    return not health_killed.is_set()


def _send_tunnel_alert(project: str, consecutive_failures: int, subdomain: str) -> None:
    """Send an alert email when the tunnel watchdog cannot recover after repeated failures.

    Called from the main retry loop once ``--max-alert-retries`` consecutive
    RuntimeErrors have occurred. Uses ``tools.gmail.send_email`` with GAOS
    credentials; silently no-ops if the tools are unavailable so the watchdog
    keeps running regardless.

    Args:
        project:             GCP project id for Secret Manager and Gmail creds.
        consecutive_failures: Number of consecutive failures that triggered this alert.
        subdomain:           The loca.lt subdomain being attempted.
    """
    try:
        import pathlib

        _parent = str(pathlib.Path(__file__).parent.parent)
        if _parent not in sys.path:
            sys.path.insert(0, _parent)
        from config import get_settings  # noqa: PLC0415
        from tools.gmail import send_email  # noqa: PLC0415

        settings = get_settings()
        to = settings.gmail.alert_address
        tunnel_url = f"https://{subdomain}.loca.lt"
        subject = (
            f"[GAOS] Ollama tunnel DOWN — {subdomain}.loca.lt unreachable "
            f"after {consecutive_failures} attempts"
        )
        body = (
            f"The Ollama tunnel watchdog (start_ollama_tunnel.py) has failed to restore\n"
            f"the loca.lt tunnel after {consecutive_failures} consecutive launch failures.\n\n"
            f"  Tunnel URL: {tunnel_url}\n"
            f"  Project:    {project}\n\n"
            f"Manual intervention required:\n"
            f"  1. Confirm Ollama is running locally on port 11434\n"
            f"  2. Confirm Node.js / npx is available on PATH\n"
            f"  3. Restart the GAOS-OllamaTunnel scheduled task (Task Scheduler)\n"
            f"     or run:  python scripts/start_ollama_tunnel.py\n\n"
            f"While the tunnel is down, GAOS email replies will fail silently.\n"
            f"The watchdog will continue retrying — another alert will not be sent\n"
            f"until the tunnel recovers and fails again."
        )
        send_email(
            project_id=project,
            to=to,
            subject=subject,
            body=body,
            from_addr=settings.gmail.sender_address,
        )
        log.warning(
            "Tunnel alert email sent to %s after %d consecutive failures.",
            to,
            consecutive_failures,
        )
    except Exception as exc:
        log.error("Could not send tunnel alert email: %s", exc)


def main() -> int:
    """Entry point — parse args and run the watchdog loop."""
    p = argparse.ArgumentParser(
        description="Start localtunnel for Ollama with auto-restart and Secret Manager sync."
    )
    p.add_argument("--port", type=int, default=11434, help="Local Ollama port (default: 11434)")
    p.add_argument("--project", default="morphic-gaos-prod", help="GCP project id")
    p.add_argument(
        "--subdomain",
        default="morphic-gaos-ollama",
        metavar="NAME",
        help="Request a fixed localtunnel subdomain (default: morphic-gaos-ollama → https://morphic-gaos-ollama.loca.lt). "
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
    p.add_argument(
        "--stop",
        action="store_true",
        help="Stop a running watchdog: kills the watchdog process, all child node.exe workers, "
        "and removes the PID lock file. Safe to run from any terminal.",
    )
    p.add_argument(
        "--max-alert-retries",
        type=int,
        default=5,
        metavar="N",
        help="Send an alert email after this many consecutive tunnel launch failures (default: 5). "
        "The alert fires once and resets when the tunnel successfully runs.",
    )
    args = p.parse_args()
    _configure_logging(args.log_file)

    if args.stop:
        return _stop_watchdog()

    if not _acquire_pid_lock():
        return 1
    log.info("PID lock acquired (%d) — single instance confirmed.", os.getpid())

    _kill_orphaned_localtunnel_nodes()
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

    try:
        attempt = 0
        consecutive_failures = 0
        alert_sent = False
        while True:
            attempt += 1
            log.info("--- Tunnel attempt %d ---", attempt)
            if attempt > 1:
                # Kill any orphaned localtunnel nodes from the previous crashed attempt
                # before trying to reclaim the preferred subdomain.
                _kill_orphaned_localtunnel_nodes()
            try:
                clean_exit = _run_tunnel_once(
                    cmd, args.no_secret, args.project, args.health_interval
                )
                if clean_exit:
                    # Tunnel ran and exited naturally — reset failure streak.
                    consecutive_failures = 0
                    alert_sent = False
                else:
                    # Health thread killed the process (flapping / crash-loop).
                    # Accumulate consecutive_failures so the alert fires if
                    # the flapping continues across --max-alert-retries attempts.
                    consecutive_failures += 1
                    log.warning(
                        "Health-kill exit — consecutive_failures now %d (alert threshold %d).",
                        consecutive_failures,
                        args.max_alert_retries,
                    )
                    if consecutive_failures >= args.max_alert_retries and not alert_sent:
                        _send_tunnel_alert(args.project, consecutive_failures, args.subdomain)
                        alert_sent = True
            except FileNotFoundError:
                log.error("npx not found. Install Node.js from https://nodejs.org")
                return 1
            except RuntimeError as exc:
                log.error("Tunnel error: %s", exc)
                consecutive_failures += 1
                if consecutive_failures >= args.max_alert_retries and not alert_sent:
                    _send_tunnel_alert(args.project, consecutive_failures, args.subdomain)
                    alert_sent = True

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
