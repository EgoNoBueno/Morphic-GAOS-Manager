"""Diagnostic: dumps PATH and npx location to a temp file when run as a scheduled task."""

import os
import shutil

log_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "diag.txt"
)
os.makedirs(os.path.dirname(log_path), exist_ok=True)

# Mirror the fix from start_ollama_tunnel.py
_NODE_DIRS = [r"C:\Program Files\nodejs", r"C:\Program Files (x86)\nodejs"]
existing = os.environ.get("PATH", "")
existing_entries = existing.split(os.pathsep)
additions = [d for d in _NODE_DIRS if os.path.isdir(d) and d not in existing_entries]
if additions:
    os.environ["PATH"] = os.pathsep.join(additions) + os.pathsep + existing

npx = shutil.which("npx")
with open(log_path, "w", encoding="utf-8") as f:
    f.write(f"npx: {npx}\n")
    f.write(f"PATH:\n{os.environ.get('PATH', '<unset>')}\n")
    f.write(f"PATHEXT: {os.environ.get('PATHEXT', '<unset>')}\n")
