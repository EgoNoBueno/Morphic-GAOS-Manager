"""
scripts/smoke_test_archivist.py — Live integration test for Archivist pipeline.

Exercises the full Steward → Archivist chain against the real Drive Inbound/
folder and local Ollama instance. No writes are performed — the Archivist
only returns move proposals.

Prerequisites:
  - Ollama running locally with llama3:latest
  - ADC configured (`gcloud auth application-default login`)
  - config/settings.yaml with valid drive_folder_id for the project
  - User must have roles/iam.serviceAccountTokenCreator on steward-sa

Usage:
    python scripts/smoke_test_archivist.py [--project morphic-gaos-prod]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from config import get_settings  # noqa: E402


def _header(text: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {text}")
    print(f"{'─' * 60}")


def _ok(text: str) -> None:
    print(f"  ✅ {text}")


def _fail(text: str) -> None:
    print(f"  ❌ {text}")


def _info(text: str) -> None:
    print(f"  ℹ️  {text}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Live smoke test for Archivist pipeline")
    parser.add_argument(
        "--project",
        default="morphic-gaos-prod",
        help="GCP project_id (default: morphic-gaos-prod)",
    )
    args = parser.parse_args()

    project_id: str = args.project

    # ── 0. Load settings + set SA credentials ───────────────────────────
    _header("Step 0: Load settings")
    settings_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    config.load_settings(settings_path)
    settings = get_settings()
    _ok(f"Project: {project_id}")
    _ok(f"LOCAL_MODEL: {settings.models.LOCAL_MODEL}")
    _project = settings.get_project(project_id)
    _ok(f"Drive root: {_project.drive_folder_id if _project else '(project not found)'}")

    # ── 0b. Build impersonated Drive service (local ADC lacks Drive scope) ─
    _header("Step 0b: Build impersonated Drive credentials")
    try:
        import google.auth
        import google.auth.transport.requests
        from google.auth import impersonated_credentials
        from googleapiclient.discovery import build as drive_build

        source_creds, _ = google.auth.default()
        source_creds.refresh(google.auth.transport.requests.Request())

        sa_email = f"steward-sa@{project_id}.iam.gserviceaccount.com"
        drive_creds = impersonated_credentials.Credentials(
            source_credentials=source_creds,
            target_principal=sa_email,
            target_scopes=["https://www.googleapis.com/auth/drive"],
        )
        _ok(f"Impersonating {sa_email}")

        def _patched_build_service(_project_id: str) -> Any:
            return drive_build("drive", "v3", credentials=drive_creds, cache_discovery=False)

    except Exception as exc:
        _fail(f"SA impersonation failed: {exc}")
        _info("Ensure your user has roles/iam.serviceAccountTokenCreator on steward-sa.")
        sys.exit(1)

    # ── 1. Verify Ollama reachable ────────────────────────────────────────
    _header("Step 1: Verify Ollama is reachable")
    try:
        import httpx

        r = httpx.get("http://localhost:11434/api/tags", timeout=5)
        models = [m["name"] for m in r.json().get("models", [])]
        _ok(f"Ollama up — models: {models}")
    except Exception as exc:
        _fail(f"Ollama unreachable: {exc}")
        print("\n  Cannot proceed without LOCAL_MODEL. Start Ollama and retry.")
        sys.exit(1)

    # ── 2. List Inbound/ via Drive API ────────────────────────────────────
    _header("Step 2: List Drive Inbound/ folder")
    try:
        from tools.drive import KnowledgeFolderNotFoundError, list_folder

        with patch("tools.drive._build_service", _patched_build_service):
            paths = list_folder("Inbound", project_id)
        _ok(f"Found {len(paths)} file(s) in Inbound/")
        for p in paths[:10]:
            _info(f"  {p}")
        if len(paths) > 10:
            _info(f"  ... and {len(paths) - 10} more")
    except KnowledgeFolderNotFoundError:
        _fail("Inbound/ folder not found under Knowledge/ root.")
        _info("Create an 'Inbound' subfolder in your Drive Knowledge/ folder and add test files.")
        sys.exit(1)
    except Exception as exc:
        _fail(f"Drive listing failed: {exc}")
        sys.exit(1)

    if not paths:
        _info("Inbound/ folder exists but is empty.")
        _info("Using 5 synthetic FileRecords to exercise the classification pipeline.")
        # Synthesize realistic test records — skips Drive listing, exercises Archivist core
        SYNTHETIC_FILES = [
            ("Inbound/2025-Q1-Invoice-Acme-Corp.pdf", "application/pdf"),
            (
                "Inbound/marketing-campaign-brief-draft.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            ("Inbound/employee-onboarding-checklist.md", "text/markdown"),
            ("Inbound/random-notes.txt", "text/plain"),
            ("Inbound/project-phoenix-strategy-v2.pdf", "application/pdf"),
        ]

        paths = [p for p, _ in SYNTHETIC_FILES]
        # Override the file_records build below with typed mime
        _synthetic_mime = {p: m for p, m in SYNTHETIC_FILES}
        _use_synthetic = True
        _ok(f"Synthetic mode: {len(paths)} records prepared")
    else:
        _synthetic_mime = {}
        _use_synthetic = False

    # ── 3. Run Archivist directly ─────────────────────────────────────────
    _header("Step 3: Run Archivist classification pass")
    import hashlib
    import mimetypes

    from agents.steward.archivist.orchestrator import (
        ArchivistContext,
        ArchivistInput,
        FileRecord,
    )
    from agents.steward.archivist.orchestrator import (
        run as archivist_run,
    )

    file_records: list[FileRecord] = []
    for path in paths[:50]:
        name = path.rsplit("/", 1)[-1] if "/" in path else path
        if _use_synthetic:
            mime = _synthetic_mime.get(path, "application/octet-stream")
        else:
            mime, _ = mimetypes.guess_type(name)
            mime = mime or "application/octet-stream"
        file_records.append(
            FileRecord(
                file_id=hashlib.sha256(path.encode()).hexdigest()[:16],
                name=name,
                mime_type=mime,
                current_path=path,
            )
        )

    _info(f"Sending {len(file_records)} file(s) to Archivist...")

    archivist_input = ArchivistInput(
        task_id="smoke-test-archivist-001",
        project_id=project_id,
        instruction="Classify all files in Inbound/ and return move proposals.",
        context=ArchivistContext(files=file_records, taxonomy_hint=""),
    )

    t0 = time.perf_counter()
    result = asyncio.run(archivist_run(archivist_input))
    elapsed = time.perf_counter() - t0

    # ── 4. Display results ────────────────────────────────────────────────
    _header(f"Step 4: Results (status={result.status}, {elapsed:.1f}s)")

    if result.status == "escalated":
        _fail(f"Archivist escalated: {result.result}")
        sys.exit(1)
    elif result.status == "failed":
        _fail(f"Archivist failed: {result.result}")
        sys.exit(1)

    # result.result is ArchivistResult
    from agents.steward.archivist.orchestrator import ArchivistResult

    res = result.result
    res_dict = res.model_dump() if isinstance(res, ArchivistResult) else dict(res)

    _ok(f"Files processed: {res_dict.get('files_processed', '?')}")
    _ok(f"Approved moves:  {len(res_dict.get('approved_moves', []))}")
    _ok(f"Ambiguous files: {len(res_dict.get('ambiguous_files', []))}")
    _ok(f"Duplicate groups: {len(res_dict.get('duplicate_candidates', []))}")
    _ok(f"Cost:            ${res_dict.get('cost_usd', 0):.6f}")

    if res_dict.get("approved_moves"):
        _header("Proposed moves (no writes performed)")
        for mv in res_dict["approved_moves"]:
            print(f"  📁 {mv['original_name']}")
            print(f"     → {mv['destination_path']}")
            print(f"       type={mv['classification']}, conf={mv['confidence']:.0%}")
            print()

    if res_dict.get("ambiguous_files"):
        _header("Ambiguous files (returned unmodified)")
        for fid in res_dict["ambiguous_files"]:
            print(f"  ❓ {fid}")

    # ── 5. Run drive_maintenance bridge (optional) ────────────────────────
    _header("Step 5: Run drive_maintenance bridge (full pipeline)")
    if _use_synthetic:
        _info("Inbound/ is empty — drive_maintenance bridge would return 'no files to classify'.")
        _info("Verifying that expected no-op path works...")
        try:
            from agents.steward.tasks.drive_maintenance import run as dm_run
            from models import AgentInput

            dm_input = AgentInput(
                task_id="smoke-test-dm-noop",
                project_id=project_id,
                instruction="Scan Inbound/ and classify files.",
                context={},
            )
            with patch("tools.drive._build_service", _patched_build_service):
                dm_output = dm_run(dm_input)
            _ok(f"drive_maintenance no-op: status={dm_output.status}")
            expected_msg = "Inbound/ is empty"
            if expected_msg in str(dm_output.result):
                _ok("Correct: empty-Inbound message returned.")
            else:
                _info(f"Result: {dm_output.result}")
        except Exception as exc:
            _fail(f"drive_maintenance no-op check failed: {exc}")
        _info("To test the full pipeline with real files, drop files into Inbound/ in Drive")
        _info("and re-run this script.")
    else:
        try:
            from agents.steward.tasks.drive_maintenance import run as dm_run
            from models import AgentInput

            dm_input = AgentInput(
                task_id="smoke-test-dm-001",
                project_id=project_id,
                instruction="Scan Inbound/ and classify files.",
                context={},
            )

            t0 = time.perf_counter()
            with patch("tools.drive._build_service", _patched_build_service):
                dm_output = dm_run(dm_input)
            dm_elapsed = time.perf_counter() - t0

            _ok(f"drive_maintenance status={dm_output.status} ({dm_elapsed:.1f}s)")
            _ok(f"requires_approval={dm_output.result.get('requires_approval', False)}")
            _ok(f"cost=${dm_output.cost_usd:.6f}")

            if dm_output.status != "success":
                _fail(f"Unexpected status: {dm_output.status}")
                _info(f"Result: {json.dumps(dm_output.result, indent=2, default=str)}")

        except Exception as exc:
            _fail(f"drive_maintenance failed: {exc}")
            import traceback

            traceback.print_exc()

    # ── Summary ───────────────────────────────────────────────────────────
    _header("SMOKE TEST COMPLETE")
    _ok("Archivist classified files via LOCAL_MODEL (Ollama).")
    _ok("Drive API read succeeded.")
    _ok("No writes performed — proposals only.")
    print()


if __name__ == "__main__":
    main()
