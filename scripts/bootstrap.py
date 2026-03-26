"""
scripts/bootstrap.py — One-time CI/CD infrastructure bootstrap for Morphic-G AOS.

Automates Phase 4 Bootstrap Runbook (GAOS-Deploy-Spec.md §20). Run once on a
blank GCP project to provision everything the CI/CD pipeline needs to function.

Resources created (all idempotent — safe to re-run):
  - Required GCP APIs enabled
  - GCS state bucket: gs://morphic-gaos-tfstate (versioning on)
  - Artifact Registry repo: cloud-run-source-deploy
  - Deployer service account: deployer-sa
  - IAM bindings: Cloud Run admin, AR writer, GCS objectAdmin, actAs 7 agent SAs
  - Workload Identity Federation pool: github-actions + provider: github-oidc
  - GitHub Secrets via `gh` CLI (optional — skipped if `gh` not installed)

Prerequisites:
  - gcloud CLI installed and authenticated: gcloud auth application-default login
  - gh CLI installed (optional): gh auth login
  - .venv activated
  - Run from repo root: python scripts/bootstrap.py [--project morphic-gaos-prod]

Spec: GAOS-Deploy-Spec.md §20 (Bootstrap Runbook) and §21 (Gap 2)
"""

from __future__ import annotations

import argparse
import base64
import shutil
import subprocess
import sys
from pathlib import Path

from google.api_core.exceptions import Conflict
from google.cloud import storage

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_PROJECT = "morphic-gaos-prod"
DEFAULT_REGION = "us-central1"
DEFAULT_AR_REPO = "cloud-run-source-deploy"
DEFAULT_DEPLOYER_SA = "deployer-sa"
DEFAULT_GITHUB_REPO = "EgoNoBueno/Morphic-GAOS-Manager"
DEFAULT_WIF_POOL = "github-actions"
DEFAULT_WIF_PROVIDER = "github-oidc"

ALL_AGENTS = [
    "nexus-prime",
    "ledger",
    "beacon",
    "pursuit",
    "foreman",
    "steward",
    "scout",
]

# GCP APIs required by all agents and CI/CD pipeline
REQUIRED_APIS = [
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "pubsub.googleapis.com",
    "secretmanager.googleapis.com",
    "bigquery.googleapis.com",
    "sheets.googleapis.com",
    "drive.googleapis.com",
    "chat.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "aiplatform.googleapis.com",
    "logging.googleapis.com",
    "storage.googleapis.com",
    "customsearch.googleapis.com",
]


# ── Shell helpers ─────────────────────────────────────────────────────────────


def _run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a shell command and return the result.

    Args:
        cmd: Command and arguments as a list of strings.
        check: If True, raise CalledProcessError on non-zero exit.
        capture: If True, capture stdout/stderr instead of streaming.
        quiet: If True, suppress the echoed command line.

    Returns:
        CompletedProcess with stdout/stderr as strings.

    Raises:
        subprocess.CalledProcessError: If check=True and exit code is non-zero.
    """
    if not quiet:
        print(f"  $ {' '.join(cmd)}")
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def _gcloud(
    *args: str, capture: bool = False, quiet: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run a gcloud command.

    Args:
        *args: gcloud subcommand arguments.
        capture: Capture stdout/stderr.
        quiet: Suppress echoed command.

    Returns:
        CompletedProcess result.

    Raises:
        SystemExit: If gcloud is not installed.
        subprocess.CalledProcessError: On non-zero exit.
    """
    if not shutil.which("gcloud"):
        print(
            "ERROR: gcloud CLI not found. Install from https://cloud.google.com/sdk/",
            file=sys.stderr,
        )
        sys.exit(1)
    return _run(["gcloud", *args], capture=capture, quiet=quiet)


# ── Step implementations ───────────────────────────────────────────────────────


def check_prerequisites(project: str) -> None:
    """Verify gcloud is installed and ADC credentials are active.

    Args:
        project: GCP project ID to verify access to.

    Raises:
        SystemExit: If gcloud is missing or credentials are not configured.
    """
    print("\n── Checking prerequisites ──────────────────────────────────────────────")
    if not shutil.which("gcloud"):
        print(
            "ERROR: gcloud CLI is required. Install from https://cloud.google.com/sdk/",
            file=sys.stderr,
        )
        sys.exit(1)

    result = _gcloud(
        "auth",
        "list",
        "--filter=status:ACTIVE",
        "--format=value(account)",
        capture=True,
        quiet=True,
    )
    account = result.stdout.strip()
    if not account:
        print(
            "ERROR: No active gcloud credentials. Run: gcloud auth application-default login",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"  ✓ gcloud authenticated as: {account}")

    result = _gcloud(
        "projects", "describe", project, "--format=value(projectId)", capture=True, quiet=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        print(
            f"ERROR: Cannot access project '{project}'. Check project ID and IAM permissions.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"  ✓ Project '{project}' accessible")


def enable_apis(project: str) -> None:
    """Enable all GCP APIs required by GAOS agents and the CI/CD pipeline.

    Args:
        project: GCP project ID.
    """
    print("\n── Enabling GCP APIs ───────────────────────────────────────────────────")
    print(f"  Enabling {len(REQUIRED_APIS)} APIs (this may take 1-2 minutes)...")
    _gcloud("services", "enable", *REQUIRED_APIS, "--project", project)
    print("  ✓ All APIs enabled")


def create_tfstate_bucket(project: str, region: str) -> str:
    """Create the GCS bucket used as the OpenTofu state backend.

    Args:
        project: GCP project ID.
        region: GCS bucket location (e.g. us-central1).

    Returns:
        The gs:// URI of the bucket.
    """
    bucket_name = "morphic-gaos-tfstate"
    bucket_uri = f"gs://{bucket_name}/"

    print(f"\n── Creating TF state bucket: {bucket_uri} ──────────────────────────────")
    client = storage.Client(project=project)

    try:
        bucket = client.create_bucket(bucket_name, location=region)
        bucket.versioning_enabled = True
        bucket.patch()
        print(f"  ✓ Created {bucket_uri} with versioning enabled")
    except Conflict:
        # Bucket already exists — ensure versioning is on
        bucket = client.bucket(bucket_name)
        bucket.reload()
        if not bucket.versioning_enabled:
            bucket.versioning_enabled = True
            bucket.patch()
            print("  ✓ Bucket exists — versioning enabled")
        else:
            print("  ✓ Bucket exists with versioning (no changes)")

    return bucket_uri


def create_artifact_registry_repo(project: str, region: str, repo: str) -> None:
    """Create the Artifact Registry Docker repository for Cloud Run images.

    Args:
        project: GCP project ID.
        region: AR repository location.
        repo: Repository name.
    """
    print(f"\n── Creating Artifact Registry repo: {repo} ─────────────────────────────")
    result = _gcloud(
        "artifacts",
        "repositories",
        "describe",
        repo,
        "--location",
        region,
        "--project",
        project,
        "--format=value(name)",
        capture=True,
        quiet=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        print(f"  ✓ Repo '{repo}' already exists (no changes)")
        return

    _gcloud(
        "artifacts",
        "repositories",
        "create",
        repo,
        "--repository-format=docker",
        "--location",
        region,
        "--project",
        project,
        "--description=GAOS Cloud Run images",
    )
    print(f"  ✓ Created Artifact Registry repo '{repo}'")


def create_deployer_sa(project: str, sa_name: str) -> str:
    """Create the CI/CD deployer service account.

    Args:
        project: GCP project ID.
        sa_name: Service account name (without @project.iam.gserviceaccount.com).

    Returns:
        Full service account email.
    """
    sa_email = f"{sa_name}@{project}.iam.gserviceaccount.com"
    print(f"\n── Creating deployer service account: {sa_email} ────────────────────────")

    result = _gcloud(
        "iam",
        "service-accounts",
        "describe",
        sa_email,
        "--project",
        project,
        "--format=value(email)",
        capture=True,
        quiet=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        print("  ✓ Service account exists (no changes)")
        return sa_email

    _gcloud(
        "iam",
        "service-accounts",
        "create",
        sa_name,
        "--display-name=GAOS CI/CD Deployer",
        "--project",
        project,
    )
    print(f"  ✓ Created service account: {sa_email}")
    return sa_email


def apply_iam_bindings(project: str, region: str, repo: str, sa_email: str) -> None:
    """Apply all IAM bindings required by the deployer service account.

    Bindings applied:
      - roles/run.admin on the project (deploy all Cloud Run services)
      - roles/artifactregistry.writer on the AR repo (push images)
      - roles/storage.objectAdmin on the TF state bucket (tofu state)
      - roles/iam.serviceAccountUser on each of the 7 agent SAs (actAs)

    Args:
        project: GCP project ID.
        region: AR repository location.
        repo: AR repository name.
        sa_email: Deployer SA email.
    """
    print("\n── Applying IAM bindings ───────────────────────────────────────────────")
    member = f"serviceAccount:{sa_email}"

    # Cloud Run admin (project-level)
    print("  Binding: roles/run.admin → project")
    _gcloud(
        "projects",
        "add-iam-policy-binding",
        project,
        f"--member={member}",
        "--role=roles/run.admin",
        "--condition=None",
        "--quiet",
        capture=True,
        quiet=True,
    )

    # AR writer (repo-level)
    print(f"  Binding: roles/artifactregistry.writer → {repo}")
    _gcloud(
        "artifacts",
        "repositories",
        "add-iam-policy-binding",
        repo,
        f"--location={region}",
        f"--project={project}",
        f"--member={member}",
        "--role=roles/artifactregistry.writer",
        "--quiet",
        capture=True,
        quiet=True,
    )

    # GCS state bucket — objectAdmin
    print("  Binding: roles/storage.objectAdmin → morphic-gaos-tfstate")
    _gcloud(
        "storage",
        "buckets",
        "add-iam-policy-binding",
        "gs://morphic-gaos-tfstate",
        f"--member={member}",
        "--role=roles/storage.objectAdmin",
        capture=True,
        quiet=True,
    )

    # actAs each agent SA
    for agent in ALL_AGENTS:
        agent_sa = f"{agent}-sa@{project}.iam.gserviceaccount.com"
        print(f"  Binding: roles/iam.serviceAccountUser → {agent_sa}")
        _gcloud(
            "iam",
            "service-accounts",
            "add-iam-policy-binding",
            agent_sa,
            "--role=roles/iam.serviceAccountUser",
            f"--member={member}",
            f"--project={project}",
            capture=True,
            quiet=True,
        )

    print("  ✓ All IAM bindings applied")


def setup_wif(
    project: str,
    sa_email: str,
    pool: str,
    provider: str,
    github_repo: str,
) -> tuple[str, str]:
    """Create the WIF pool and OIDC provider for GitHub Actions, and return secret values.

    The provider is scoped to the specific GitHub repository via attribute-condition.
    If the pool or provider already exist, the creation step is skipped.

    Args:
        project: GCP project ID.
        sa_email: Deployer SA email.
        pool: WIF pool name (e.g. github-actions).
        provider: WIF OIDC provider name (e.g. github-oidc).
        github_repo: GitHub repository in owner/name format.

    Returns:
        Tuple of (WIF_PROVIDER value, WIF_SERVICE_ACCOUNT value) for GitHub Secrets.

    Raises:
        subprocess.CalledProcessError: If gcloud commands fail unexpectedly.
    """
    print("\n── Setting up Workload Identity Federation ──────────────────────────────")

    result = _gcloud(
        "projects",
        "describe",
        project,
        "--format=value(projectNumber)",
        capture=True,
        quiet=True,
    )
    project_number = result.stdout.strip()
    if not project_number:
        raise RuntimeError(f"Could not retrieve project number for '{project}'")

    # Create WIF pool (idempotent)
    pool_exists = (
        _gcloud(
            "iam",
            "workload-identity-pools",
            "describe",
            pool,
            "--location=global",
            f"--project={project}",
            "--format=value(name)",
            capture=True,
            quiet=True,
            check=False,
        ).returncode
        == 0
    )

    if pool_exists:
        print(f"  ✓ WIF pool '{pool}' exists (no changes)")
    else:
        _gcloud(
            "iam",
            "workload-identity-pools",
            "create",
            pool,
            "--location=global",
            "--display-name=GitHub Actions",
            f"--project={project}",
        )
        print(f"  ✓ Created WIF pool '{pool}'")

    # Create OIDC provider (idempotent)
    provider_exists = (
        _gcloud(
            "iam",
            "workload-identity-pools",
            "providers",
            "describe",
            provider,
            "--location=global",
            f"--workload-identity-pool={pool}",
            f"--project={project}",
            "--format=value(name)",
            capture=True,
            quiet=True,
            check=False,
        ).returncode
        == 0
    )

    if provider_exists:
        print(f"  ✓ WIF provider '{provider}' exists (no changes)")
    else:
        _gcloud(
            "iam",
            "workload-identity-pools",
            "providers",
            "create-oidc",
            provider,
            "--location=global",
            f"--workload-identity-pool={pool}",
            "--display-name=GitHub OIDC",
            "--attribute-mapping=google.subject=assertion.sub,attribute.repository=assertion.repository",
            f"--attribute-condition=attribute.repository=='{github_repo}'",
            "--issuer-uri=https://token.actions.githubusercontent.com",
            f"--project={project}",
        )
        print(f"  ✓ Created WIF OIDC provider '{provider}'")

    # Bind deployer SA to the WIF pool
    pool_resource = (
        f"principalSet://iam.googleapis.com/projects/{project_number}"
        f"/locations/global/workloadIdentityPools/{pool}"
        f"/attribute.repository/{github_repo}"
    )
    print("  Binding deployer-sa → WIF principal set")
    _gcloud(
        "iam",
        "service-accounts",
        "add-iam-policy-binding",
        sa_email,
        "--role=roles/iam.workloadIdentityUser",
        f"--member=principalSet://{pool_resource.split('://', 1)[1]}",
        f"--project={project}",
        capture=True,
        quiet=True,
    )

    wif_provider_value = (
        f"projects/{project_number}/locations/global"
        f"/workloadIdentityPools/{pool}/providers/{provider}"
    )
    print("  ✓ WIF binding applied")
    return wif_provider_value, sa_email


def set_github_secrets(
    github_repo: str,
    wif_provider: str,
    wif_sa: str,
) -> None:
    """Set GitHub Secrets via `gh` CLI. Skips gracefully if gh is not installed.

    Sets WIF_PROVIDER, WIF_SERVICE_ACCOUNT, and SETTINGS_YAML if config/settings.yaml exists.

    Args:
        github_repo: GitHub repository in owner/name format.
        wif_provider: WIF provider resource name.
        wif_sa: Deployer service account email.
    """
    print("\n── Setting GitHub Secrets ───────────────────────────────────────────────")
    if not shutil.which("gh"):
        print("  ⚠ gh CLI not found — set these secrets manually in GitHub:")
        print(f"    WIF_PROVIDER        = {wif_provider}")
        print(f"    WIF_SERVICE_ACCOUNT = {wif_sa}")
        print("    SETTINGS_YAML       = <base64 of config/settings.yaml>")
        return

    result = _run(["gh", "auth", "status"], capture=True, quiet=True, check=False)
    if result.returncode != 0:
        print("  ⚠ gh CLI not authenticated — run 'gh auth login' then set:")
        print(f"    WIF_PROVIDER        = {wif_provider}")
        print(f"    WIF_SERVICE_ACCOUNT = {wif_sa}")
        return

    _run(["gh", "secret", "set", "WIF_PROVIDER", "--body", wif_provider, "--repo", github_repo])
    print("  ✓ WIF_PROVIDER set")

    _run(["gh", "secret", "set", "WIF_SERVICE_ACCOUNT", "--body", wif_sa, "--repo", github_repo])
    print("  ✓ WIF_SERVICE_ACCOUNT set")

    settings_path = Path("config/settings.yaml")
    if settings_path.exists():
        b64 = base64.b64encode(settings_path.read_bytes()).decode()
        _run(["gh", "secret", "set", "SETTINGS_YAML", "--body", b64, "--repo", github_repo])
        print("  ✓ SETTINGS_YAML set (from config/settings.yaml)")
    else:
        print("  ⚠ config/settings.yaml not found — set SETTINGS_YAML manually after creating it")


def print_next_steps(github_repo: str) -> None:
    """Print the remaining manual steps after bootstrap completes.

    Args:
        github_repo: GitHub repository in owner/name format.
    """
    print("""
── Next steps ──────────────────────────────────────────────────────────────────

  1. Create the 'production' GitHub Environment (manual — GitHub UI only):
       Repository → Settings → Environments → New environment → name: production
       Add yourself as required reviewer → Save protection rules

  2. Run the remaining setup scripts if not already done:
       python scripts/setup_workspace.py  # Drive/Sheets structure
       python scripts/setup_secrets.py   # Secret Manager secrets

  3. Push to master to trigger the CI/CD pipeline:
       git push origin master
       → build: Docker image pushed to Artifact Registry
       → plan: tofu plan runs (watch for errors)
       → apply: approve the production environment gate → all 7 agents deploy

  4. After apply succeeds — wiring:
       a. Set VERTEX_AGENT_ENDPOINT in Apps Script Script Properties:
            Apps Script editor → Project Settings → Script Properties
            Key: VERTEX_AGENT_ENDPOINT
            Value: https://nexus-prime-7bu22bxlda-uc.a.run.app/sync
       b. CLOUD_RUN_URL on nexus-prime is set automatically by CI/CD — no action needed.

  5. Validate: python scripts/gaos_doctor.py --project morphic-gaos-prod

  Full runbook: GAOS-Deploy-Spec.md §20
""")


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    """Bootstrap the full CI/CD infrastructure for Morphic-G AOS."""
    parser = argparse.ArgumentParser(
        description=(
            "One-time CI/CD infrastructure bootstrap for Morphic-G AOS.\n"
            "Automates GAOS-Deploy-Spec.md §20 (Phase 4 Bootstrap Runbook)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--project", default=DEFAULT_PROJECT, help=f"GCP project ID (default: {DEFAULT_PROJECT})"
    )
    parser.add_argument(
        "--region", default=DEFAULT_REGION, help=f"GCP region (default: {DEFAULT_REGION})"
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_AR_REPO,
        help=f"Artifact Registry repo name (default: {DEFAULT_AR_REPO})",
    )
    parser.add_argument(
        "--deployer-sa",
        default=DEFAULT_DEPLOYER_SA,
        help=f"Deployer SA name (default: {DEFAULT_DEPLOYER_SA})",
    )
    parser.add_argument(
        "--github-repo",
        default=DEFAULT_GITHUB_REPO,
        help=f"GitHub repo owner/name (default: {DEFAULT_GITHUB_REPO})",
    )
    parser.add_argument(
        "--wif-pool", default=DEFAULT_WIF_POOL, help=f"WIF pool name (default: {DEFAULT_WIF_POOL})"
    )
    parser.add_argument(
        "--wif-provider",
        default=DEFAULT_WIF_PROVIDER,
        help=f"WIF OIDC provider name (default: {DEFAULT_WIF_PROVIDER})",
    )
    parser.add_argument(
        "--skip-github-secrets", action="store_true", help="Skip setting GitHub Secrets via gh CLI."
    )
    args = parser.parse_args()

    print("══════════════════════════════════════════════════════════════════════════")
    print("  Morphic-G AOS — CI/CD Infrastructure Bootstrap")
    print("══════════════════════════════════════════════════════════════════════════")
    print(f"  Project:     {args.project}")
    print(f"  Region:      {args.region}")
    print(f"  AR Repo:     {args.repo}")
    print(f"  Deployer SA: {args.deployer_sa}@{args.project}.iam.gserviceaccount.com")
    print(f"  GitHub:      {args.github_repo}")
    print()

    confirm = input("Proceed? [y/N] ").strip().lower()
    if confirm not in ("y", "yes"):
        print("Aborted.")
        sys.exit(0)

    try:
        check_prerequisites(args.project)
        enable_apis(args.project)
        create_tfstate_bucket(args.project, args.region)
        create_artifact_registry_repo(args.project, args.region, args.repo)
        sa_email = create_deployer_sa(args.project, args.deployer_sa)
        apply_iam_bindings(args.project, args.region, args.repo, sa_email)
        wif_provider_value, wif_sa = setup_wif(
            args.project,
            sa_email,
            args.wif_pool,
            args.wif_provider,
            args.github_repo,
        )
        if not args.skip_github_secrets:
            set_github_secrets(args.github_repo, wif_provider_value, wif_sa)
    except subprocess.CalledProcessError as exc:
        print(f"\nERROR: gcloud command failed (exit {exc.returncode})", file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)

    print("\n══════════════════════════════════════════════════════════════════════════")
    print("  Bootstrap complete ✓")
    print("══════════════════════════════════════════════════════════════════════════")
    print_next_steps(args.github_repo)


if __name__ == "__main__":
    main()
