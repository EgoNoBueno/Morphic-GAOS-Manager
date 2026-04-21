"""Check Ollama's current work state via /api/ps and /api/tags.

Shows what models are loaded, whether any generation is in progress,
and estimated VRAM usage.  Reads OLLAMA_HOST from Secret Manager.
"""

import sys

import google.auth
from google.cloud import secretmanager
import httpx

PROJECT = "morphic-gaos-prod"

# ── Resolve host ──────────────────────────────────────────────────────────────
try:
    sm = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT}/secrets/OLLAMA_HOST/versions/latest"
    host = (
        sm.access_secret_version(request={"name": name})
        .payload.data.decode()
        .lstrip("\ufeff")
        .strip()
        .rstrip("/")
    )
except Exception as exc:
    print(f"ERROR: could not read OLLAMA_HOST from Secret Manager: {exc}")
    sys.exit(1)
print(f"OLLAMA_HOST = {host}\n")

headers = {"Bypass-Tunnel-Reminder": "true"} if ".loca.lt" in host else {}

# ── /api/ps — running models ──────────────────────────────────────────────────
print("=== /api/ps (loaded models) ===")
try:
    r = httpx.get(f"{host}/api/ps", timeout=10.0, headers=headers)
    r.raise_for_status()
    data = r.json()
    models = data.get("models", [])
    if not models:
        print("  (no models currently loaded — Ollama is idle)")
    for m in models:
        name_str = m.get("name", "?")
        size_mb = m.get("size", 0) // (1024 * 1024)
        expires = m.get("expires_at", "?")
        details = m.get("details", {})
        print(f"  model={name_str}  size={size_mb}MB  expires={expires}")
        print(f"    details={details}")
except Exception as exc:
    print(f"  ERROR: {exc}")

# ── /api/tags — available models ──────────────────────────────────────────────
print("\n=== /api/tags (available models) ===")
try:
    r = httpx.get(f"{host}/api/tags", timeout=10.0, headers=headers)
    r.raise_for_status()
    for m in r.json().get("models", []):
        size_gb = m.get("size", 0) / (1024 ** 3)
        modified = m.get("modified_at", "?")[:19]
        print(f"  {m.get('name', '<unknown>'):<40} {size_gb:.1f}GB  modified={modified}")
except Exception as exc:
    print(f"  ERROR: {exc}")

print("\nDone.")
