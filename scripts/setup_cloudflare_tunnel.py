"""One-time migration from loca.lt to Cloudflare Tunnel for Ollama connectivity.

Cloudflare Tunnel gives GAOS a permanent, stable URL for reaching the local Ollama
server from Cloud Run — completely free, no subdomain theft, no HTML challenge pages,
no Secret Manager updates needed after setup.

What this script does:
  1. Installs cloudflared (via winget, falls back to direct download)
  2. Authenticates with Cloudflare (opens browser once)
  3. Creates a named tunnel "morphic-gaos-ollama" (idempotent — skips if exists)
  4. Writes ~/.cloudflared/config.yml
  5. Installs cloudflared as a Windows service (auto-start at boot, runs hidden)
  6. Verifies Ollama is reachable through the tunnel
  7. Updates OLLAMA_HOST in Secret Manager with the permanent tunnel URL
  8. Unregisters the old GAOS-OllamaTunnel loca.lt Task Scheduler task (if present)

Post-setup:
  - OLLAMA_HOST stays the same forever — no more Secret Manager drift
  - No watchdog process needed — cloudflared service handles restarts automatically
  - Reboot safe — Windows SCM starts cloudflared before user login

Usage:
    python scripts/setup_cloudflare_tunnel.py --project morphic-gaos-prod

    # Dry-run (skip Secret Manager write and service install)
    python scripts/setup_cloudflare_tunnel.py --dry-run

    # Already authenticated; skip browser step
    python scripts/setup_cloudflare_tunnel.py --skip-login

    # Uninstall (remove service + config; does NOT delete the tunnel on Cloudflare)
    python scripts/setup_cloudflare_tunnel.py --uninstall

Requirements:
  - Windows 10/11 with winget OR internet access for direct cloudflared download
  - Ollama running locally on --port (default 11434)
  - gcloud ADC credentials with Secret Manager write access
  - Run from an elevated (Administrator) terminal for service install
    (non-elevated: script will detect and print the escalation command)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import httpx

# ── Constants ─────────────────────────────────────────────────────────────────

TUNNEL_NAME = "morphic-gaos-ollama"
CLOUDFLARED_DIRECT_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
CF_CONFIG_DIR = Path.home() / ".cloudflared"
CF_CONFIG_FILE = CF_CONFIG_DIR / "config.yml"
OLLAMA_SECRET_NAME = "OLLAMA_HOST"  # pragma: allowlist secret
OLD_TASK_NAME = "GAOS-OllamaTunnel"  # legacy loca.lt task to remove

log = logging.getLogger("cf-tunnel-setup")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _run(
    cmd: list[str],
    *,
    capture: bool = True,
    check: bool = True,
    no_window: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, optionally capturing output."""
    flags = 0x08000000 if (no_window and platform.system() == "Windows") else 0  # CREATE_NO_WINDOW
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        check=check,
        creationflags=flags,
        input=input_text,
    )


def _is_admin() -> bool:
    """Return True if the current process has administrator/elevated privileges."""
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _find_cloudflared() -> str | None:
    """Return the path to cloudflared.exe if already installed, else None."""
    # Check PATH first
    found = shutil.which("cloudflared")
    if found:
        return found
    # Common install locations
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "cloudflared"
        / "cloudflared.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "cloudflared" / "cloudflared.exe",
        Path.home() / "cloudflared" / "cloudflared.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _install_cloudflared(dry_run: bool = False) -> str:
    """Install cloudflared via winget, falling back to direct GitHub download.

    Args:
        dry_run: If True, skip actual installation and return a placeholder path.

    Returns:
        Absolute path to the installed cloudflared.exe.

    Raises:
        RuntimeError: If installation fails via both methods.
    """
    log.info("cloudflared not found — installing...")

    if dry_run:
        log.info("[dry-run] Skipping cloudflared install.")
        return "cloudflared"

    # Method 1: winget
    if shutil.which("winget"):
        log.info("Trying: winget install Cloudflare.cloudflared")
        try:
            _run(
                [
                    "winget",
                    "install",
                    "--id",
                    "Cloudflare.cloudflared",
                    "--silent",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                ],
                no_window=False,
            )
            found = _find_cloudflared()
            if found:
                log.info("cloudflared installed via winget: %s", found)
                return found
        except subprocess.CalledProcessError as exc:
            log.warning("winget install failed (exit %d) — trying direct download.", exc.returncode)

    # Method 2: direct download from GitHub releases
    log.info("Downloading cloudflared from GitHub releases...")
    install_dir = Path.home() / "cloudflared"
    install_dir.mkdir(exist_ok=True)
    dest = install_dir / "cloudflared.exe"

    with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        urllib.request.urlretrieve(CLOUDFLARED_DIRECT_URL, tmp_path)
        os.replace(tmp_path, dest)
        log.info("cloudflared downloaded to: %s", dest)
    except Exception as exc:
        raise RuntimeError(f"Direct download failed: {exc}") from exc

    # Add to PATH for this process
    os.environ["PATH"] = str(install_dir) + os.pathsep + os.environ.get("PATH", "")
    return str(dest)


def _get_cloudflared_version(exe: str) -> str:
    """Return cloudflared version string."""
    result = _run([exe, "--version"])
    return result.stdout.strip()


def _list_tunnels(exe: str) -> list[dict]:
    """Return list of existing tunnels as dicts with 'id' and 'name' keys."""
    try:
        result = _run([exe, "tunnel", "list", "--output", "json"])
        return json.loads(result.stdout) or []
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return []


def _get_or_create_tunnel(exe: str, name: str, dry_run: bool = False) -> tuple[str, str]:
    """Get an existing tunnel by name or create a new one.

    Args:
        exe:     Path to cloudflared.exe.
        name:    Desired tunnel name.
        dry_run: If True, skip actual creation.

    Returns:
        Tuple of (tunnel_id, tunnel_url) where tunnel_url is the permanent
        https://<tunnel_id>.cfargotunnel.com address.
    """
    tunnels = _list_tunnels(exe)
    for t in tunnels:
        if t.get("name") == name:
            tunnel_id = t["id"]
            log.info("Tunnel already exists: %s (%s)", name, tunnel_id)
            return tunnel_id, f"https://{tunnel_id}.cfargotunnel.com"

    if dry_run:
        fake_id = "dry-run-0000-0000-0000-000000000000"
        log.info("[dry-run] Would create tunnel '%s'", name)
        return fake_id, f"https://{fake_id}.cfargotunnel.com"

    log.info("Creating tunnel: %s", name)
    result = _run([exe, "tunnel", "create", name])
    # Parse tunnel ID from output: "Created tunnel <name> with id <uuid>"
    match = re.search(r"with id ([0-9a-f-]{36})", result.stdout)
    if not match:
        # Try JSON output
        try:
            data = json.loads(result.stdout)
            tunnel_id = data["id"]
        except Exception as exc:
            raise RuntimeError(f"Could not parse tunnel ID from output:\n{result.stdout}") from exc
    else:
        tunnel_id = match.group(1)

    log.info("Tunnel created: %s (%s)", name, tunnel_id)
    return tunnel_id, f"https://{tunnel_id}.cfargotunnel.com"


def _write_config(tunnel_id: str, port: int, dry_run: bool = False) -> None:
    """Write ~/.cloudflared/config.yml for the named tunnel.

    Args:
        tunnel_id: The Cloudflare tunnel UUID.
        port:      Local Ollama port.
        dry_run:   If True, print the config but don't write it.
    """
    creds_file = CF_CONFIG_DIR / f"{tunnel_id}.json"
    config_content = f"""# Cloudflare Tunnel config for GAOS Ollama connectivity
# Generated by scripts/setup_cloudflare_tunnel.py
# Tunnel URL: https://{tunnel_id}.cfargotunnel.com
tunnel: {tunnel_id}
credentials-file: {creds_file}

ingress:
  - service: http://localhost:{port}
"""
    if dry_run:
        log.info("[dry-run] Would write config to %s:\n%s", CF_CONFIG_FILE, config_content)
        return

    CF_CONFIG_DIR.mkdir(exist_ok=True)
    CF_CONFIG_FILE.write_text(config_content, encoding="utf-8")
    log.info("Config written: %s", CF_CONFIG_FILE)


def _install_service(exe: str, dry_run: bool = False) -> None:
    """Install cloudflared as a Windows service.

    Requires Administrator privileges. Detects if running non-elevated
    and prints the escalation command instead of failing silently.

    Args:
        exe:     Path to cloudflared.exe.
        dry_run: If True, skip actual service install.
    """
    if dry_run:
        log.info("[dry-run] Would run: %s service install", exe)
        return

    if not _is_admin():
        log.warning(
            "Not running as Administrator — cannot install Windows service.\n"
            "Run this command in an elevated terminal to finish service setup:\n\n"
            "    %s service install\n\n"
            "Until then, run the tunnel manually: %s tunnel run %s",
            exe,
            exe,
            TUNNEL_NAME,
        )
        return

    try:
        # Uninstall first to handle re-runs cleanly
        _run([exe, "service", "uninstall"], check=False)
    except Exception:
        pass

    _run([exe, "service", "install"])
    log.info("cloudflared Windows service installed — auto-starts at boot.")

    # Start the service immediately
    try:
        _run(["sc", "start", "cloudflared"])
        log.info("Service started.")
    except subprocess.CalledProcessError:
        # May already be running
        pass


def _verify_ollama_reachable(tunnel_url: str, timeout: float = 15.0) -> bool:
    """Poll the tunnel URL until Ollama responds or timeout is reached.

    Args:
        tunnel_url: Base URL, e.g. https://<id>.cfargotunnel.com
        timeout:    Max seconds to wait.

    Returns:
        True if Ollama responded with HTTP 200, False otherwise.
    """
    deadline = time.time() + timeout
    url = f"{tunnel_url}/api/tags"
    log.info("Verifying Ollama reachable at %s (timeout=%ss)...", url, timeout)

    while time.time() < deadline:
        try:
            resp = httpx.get(url, timeout=5.0)
            if resp.status_code == 200:
                log.info("Ollama reachable: %s", tunnel_url)
                return True
            log.debug("HTTP %d — retrying...", resp.status_code)
        except (httpx.ConnectError, httpx.TimeoutException):
            log.debug("Connect error — retrying...")
        time.sleep(2)

    log.error("Ollama not reachable at %s after %ss.", tunnel_url, timeout)
    return False


def _update_secret_manager(tunnel_url: str, project_id: str, dry_run: bool = False) -> None:
    """Write OLLAMA_HOST to Secret Manager.

    Args:
        tunnel_url: The permanent Cloudflare Tunnel URL.
        project_id: GCP project ID.
        dry_run:    If True, print but don't write.
    """
    if dry_run:
        log.info("[dry-run] Would update OLLAMA_HOST → %s", tunnel_url)
        return

    try:
        from google.cloud import secretmanager  # type: ignore[import-untyped]

        client = secretmanager.SecretManagerServiceClient()
        secret_path = f"projects/{project_id}/secrets/{OLLAMA_SECRET_NAME}"

        # Read current value — skip write if unchanged
        try:
            current = (
                client.access_secret_version(request={"name": f"{secret_path}/versions/latest"})
                .payload.data.decode("utf-8")
                .strip()
            )
            if current == tunnel_url:
                log.info("OLLAMA_HOST already set to %s — no update needed.", tunnel_url)
                return
        except Exception:
            pass

        parent = client.secret_path(project_id, OLLAMA_SECRET_NAME)
        response = client.add_secret_version(
            request={"parent": parent, "payload": {"data": tunnel_url.encode("utf-8")}}
        )
        log.info("OLLAMA_HOST → '%s'  (%s)", tunnel_url, response.name)
    except ImportError:
        log.error(
            "google-cloud-secret-manager not installed. Run: pip install google-cloud-secret-manager"
        )
    except Exception as exc:
        log.error("Secret Manager update failed: %s", exc)
        raise


def _remove_legacy_task(dry_run: bool = False) -> None:
    """Unregister the old GAOS-OllamaTunnel Windows Task Scheduler task if it exists.

    Args:
        dry_run: If True, only check and report.
    """
    try:
        result = _run(
            ["schtasks", "/Query", "/TN", OLD_TASK_NAME, "/FO", "LIST"],
            check=False,
        )
        if result.returncode != 0:
            log.info("Legacy task '%s' not found — nothing to remove.", OLD_TASK_NAME)
            return
    except Exception:
        return

    if dry_run:
        log.info("[dry-run] Would remove legacy Task Scheduler task: %s", OLD_TASK_NAME)
        return

    try:
        _run(["schtasks", "/Delete", "/TN", OLD_TASK_NAME, "/F"])
        log.info("Removed legacy Task Scheduler task: %s", OLD_TASK_NAME)
    except subprocess.CalledProcessError as exc:
        log.warning(
            "Could not remove legacy task (exit %d) — remove manually if needed.", exc.returncode
        )


def _kill_legacy_localtunnel() -> None:
    """Kill any running loca.lt node processes (cleanup after migration)."""
    try:
        result = _run(
            [
                "wmic",
                "process",
                "where",
                "name='node.exe'",
                "get",
                "ProcessId,CommandLine",
                "/FORMAT:CSV",
            ],
            check=False,
        )
        killed = 0
        for line in result.stdout.splitlines():
            if "localtunnel" in line.lower():
                parts = [p.strip() for p in line.split(",") if p.strip()]
                # CSV format: Node,CommandLine,ProcessId — PID is last field
                pid = parts[-1] if parts else ""
                if pid.isdigit():
                    _run(["taskkill", "/F", "/PID", pid], check=False)
                    killed += 1
        if killed:
            time.sleep(1)
            log.info("Killed %d legacy loca.lt node.exe process(es).", killed)
    except Exception:
        pass


def _uninstall(exe: str | None) -> int:
    """Remove the cloudflared Windows service and config file.

    Does NOT delete the tunnel from Cloudflare — run `cloudflared tunnel delete <name>`
    manually if you want to permanently remove it.

    Args:
        exe: Path to cloudflared.exe, or None if not found.

    Returns:
        Exit code (0 = success, 1 = error).
    """
    log.info("Uninstalling cloudflared service...")
    if exe:
        try:
            _run([exe, "service", "uninstall"], check=False, no_window=False)
            log.info("Service uninstalled.")
        except Exception as exc:
            log.warning("Service uninstall failed: %s", exc)
    else:
        log.warning("cloudflared not found on PATH — cannot uninstall service.")

    if CF_CONFIG_FILE.exists():
        CF_CONFIG_FILE.unlink()
        log.info("Removed config: %s", CF_CONFIG_FILE)

    log.info(
        "Done. Tunnel still exists on Cloudflare.\nTo fully delete: cloudflared tunnel delete %s",
        TUNNEL_NAME,
    )
    return 0


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    """Entry point."""
    p = argparse.ArgumentParser(
        description="Migrate Ollama connectivity from loca.lt to Cloudflare Tunnel (free, permanent)."
    )
    p.add_argument("--project", default="morphic-gaos-prod", help="GCP project ID")
    p.add_argument("--port", type=int, default=11434, help="Local Ollama port (default: 11434)")
    p.add_argument(
        "--skip-login",
        action="store_true",
        help="Skip `cloudflared tunnel login` (use if already authenticated)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without executing Secret Manager writes or service install",
    )
    p.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the cloudflared service and config (does not delete tunnel on Cloudflare)",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # ── Uninstall path ────────────────────────────────────────────────────────
    if args.uninstall:
        exe = _find_cloudflared()
        return _uninstall(exe)

    # ── Step 1: Install cloudflared ───────────────────────────────────────────
    exe = _find_cloudflared()
    if exe:
        ver = _get_cloudflared_version(exe)
        log.info("cloudflared found: %s (%s)", exe, ver)
    else:
        exe = _install_cloudflared(dry_run=args.dry_run)
        if not args.dry_run:
            log.info("cloudflared installed: %s", _get_cloudflared_version(exe))

    # ── Step 2: Authenticate with Cloudflare ──────────────────────────────────
    if not args.skip_login:
        log.info(
            "Opening Cloudflare login in your browser...\n"
            "  • Sign in with your Cloudflare account (free account is fine)\n"
            "  • Select a zone/domain if prompted (or 'I don't have a domain')\n"
            "  • The cert will be saved to ~/.cloudflared/cert.pem\n"
        )
        if not args.dry_run:
            try:
                subprocess.run([exe, "tunnel", "login"], check=True)
            except subprocess.CalledProcessError as exc:
                log.error(
                    "Login failed (exit %d). Re-run with --skip-login if already authenticated.",
                    exc.returncode,
                )
                return 1
        else:
            log.info("[dry-run] Would run: %s tunnel login", exe)
    else:
        log.info("Skipping login (--skip-login).")

    # ── Step 3: Create tunnel ─────────────────────────────────────────────────
    tunnel_id, tunnel_url = _get_or_create_tunnel(exe, TUNNEL_NAME, dry_run=args.dry_run)
    log.info("Permanent tunnel URL: %s", tunnel_url)

    # ── Step 4: Write config ──────────────────────────────────────────────────
    _write_config(tunnel_id, args.port, dry_run=args.dry_run)

    # ── Step 5: Install Windows service ──────────────────────────────────────
    _install_service(exe, dry_run=args.dry_run)

    # ── Step 6: Kill legacy loca.lt processes ─────────────────────────────────
    if not args.dry_run:
        _kill_legacy_localtunnel()

    # ── Step 7: Verify Ollama reachable ──────────────────────────────────────
    if not args.dry_run:
        # Give service a moment to start if just installed
        time.sleep(3)
        reachable = _verify_ollama_reachable(tunnel_url, timeout=30.0)
        if not reachable:
            log.warning(
                "Ollama not yet reachable through tunnel.\n"
                "If the service just started, wait 30 seconds and run:\n"
                "    python scripts/check_ollama_reachable.py --url %s",
                tunnel_url,
            )
    else:
        log.info("[dry-run] Skipping reachability check.")
        reachable = True

    # ── Step 8: Update Secret Manager ─────────────────────────────────────────
    _update_secret_manager(tunnel_url, args.project, dry_run=args.dry_run)

    # ── Step 9: Remove legacy Task Scheduler task ─────────────────────────────
    _remove_legacy_task(dry_run=args.dry_run)

    # ── Summary ───────────────────────────────────────────────────────────────
    log.info(
        "\n"
        "═══════════════════════════════════════════════════════\n"
        " Cloudflare Tunnel setup complete\n"
        "═══════════════════════════════════════════════════════\n"
        " Tunnel name : %s\n"
        " Tunnel URL  : %s  ← permanent, never changes\n"
        " OLLAMA_HOST : updated in Secret Manager\n"
        " Service     : cloudflared (auto-starts at boot)\n"
        " loca.lt     : removed\n"
        "═══════════════════════════════════════════════════════\n"
        " Cloud Run will use the new URL on the next LOCAL_MODEL call.\n"
        " No further maintenance required.\n",
        TUNNEL_NAME,
        tunnel_url,
    )
    return 0 if reachable else 2


if __name__ == "__main__":
    raise SystemExit(main())
