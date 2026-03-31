"""Check reachability of an Ollama host (e.g. https://abc.ngrok.io or http://localhost:11434).

Usage:
    python scripts/check_ollama_reachable.py --url https://<host>

Exits 0 when reachable and returns JSON tags, non-zero otherwise.
"""

import argparse
import sys

import httpx


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True, help="Full base URL for Ollama (no trailing slash)")
    p.add_argument("--timeout", type=float, default=5.0, help="Request timeout seconds")
    args = p.parse_args()

    url = args.url.rstrip("/")
    try:
        resp = httpx.get(f"{url}/api/tags", timeout=args.timeout)
        resp.raise_for_status()
        print("OK: Ollama reachable")
        print(resp.text)
        return 0
    except Exception as exc:
        print(f"ERROR: Ollama not reachable at {url}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
