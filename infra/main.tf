# Note: OpenTofu is used here as a 100% open-source, community-driven replacement
# for Terraform, ensuring long-term cost sovereignty and avoiding platform lock-in.
#
# Architecture: One image, 7 configurations.
# All agents share a single container image (gaos-agent), differentiated only by
# the AGENT_NAME environment variable. This reduces build time, registry storage,
# and deployment complexity compared to per-agent images.

terraform {
  required_version = "~> 1.8"  # OpenTofu 1.8.x through 1.x; blocks accidental 2.x upgrades

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }

  backend "gcs" {
    # GCS backend provides stable state storage with negligible (~$0.001/mo) usage costs.
    # PREREQUISITE: Create this bucket manually before running `tofu init` (see §9.3):
    #   gcloud storage buckets create gs://morphic-gaos-tfstate --location=us-central1
    bucket = "morphic-gaos-tfstate"
    prefix = "tofu/state"
  }
}

provider "google" {
  project = var.project_id
  region  = "us-central1"
}

variable "project_id" {
  type        = string
  description = "GCP project ID."
  default     = "morphic-gaos-prod"
}

variable "image_tag" {
  type        = string
  description = "Container image tag (Git SHA). Passed in by CI: tofu plan -var='image_tag=<sha>'."
}

locals {
  region = "us-central1"
  image  = "us-central1-docker.pkg.dev/${var.project_id}/cloud-run-source-deploy/gaos-agent:${var.image_tag}"
  agents = toset([
    "nexus-prime",
    "ledger",
    "beacon",
    "pursuit",
    "foreman",
    "steward",
    "scout",
  ])
}

# Import blocks — bring pre-existing Cloud Run services into TF state.
# Required because services were deployed manually before the IaC pipeline existed.
# These are idempotent on first apply; OpenTofu skips them once state is populated.
# Safe to leave in place: subsequent plans will show 0 changes for already-managed resources.
import {
  to = google_cloud_run_v2_service.agent["nexus-prime"]
  id = "projects/morphic-gaos-prod/locations/us-central1/services/nexus-prime"
}
import {
  to = google_cloud_run_v2_service.agent["ledger"]
  id = "projects/morphic-gaos-prod/locations/us-central1/services/ledger"
}
import {
  to = google_cloud_run_v2_service.agent["beacon"]
  id = "projects/morphic-gaos-prod/locations/us-central1/services/beacon"
}
import {
  to = google_cloud_run_v2_service.agent["pursuit"]
  id = "projects/morphic-gaos-prod/locations/us-central1/services/pursuit"
}
import {
  to = google_cloud_run_v2_service.agent["foreman"]
  id = "projects/morphic-gaos-prod/locations/us-central1/services/foreman"
}
import {
  to = google_cloud_run_v2_service.agent["steward"]
  id = "projects/morphic-gaos-prod/locations/us-central1/services/steward"
}
import {
  to = google_cloud_run_v2_service.agent["scout"]
  id = "projects/morphic-gaos-prod/locations/us-central1/services/scout"
}

resource "google_cloud_run_v2_service" "agent" {
  for_each = local.agents

  name     = each.value
  location = local.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = "${each.value}-sa@${var.project_id}.iam.gserviceaccount.com"

    scaling {
      min_instance_count = 0
      max_instance_count = 5
    }

    containers {
      image = local.image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          memory = "512Mi"
          cpu    = "1"
        }
      }

      env {
        name  = "AGENT_NAME"
        value = each.value
      }
    }

    # concurrency = 1 is mandatory for LangGraph state integrity.
    # Pub/Sub handles queuing — never increase this without revisiting the graph design.
    max_instance_request_concurrency = 1
    timeout                          = "60s"
  }
}

# Service URLs — used post-apply to update Pub/Sub subscriptions and Chat JWT wiring.
# The CI/CD apply job reads nexus_prime_url to wire CLOUD_RUN_URL automatically.
output "service_urls" {
  description = "Cloud Run service URLs, keyed by agent name."
  value       = { for k, v in google_cloud_run_v2_service.agent : k => v.uri }
}

output "nexus_prime_url" {
  description = "Nexus-Prime Cloud Run service URL. Used to wire CLOUD_RUN_URL env var post-apply."
  value       = google_cloud_run_v2_service.agent["nexus-prime"].uri
}

# Agent SAs need dataEditor (streaming insert) on the BigQuery dataset so they
# can write heartbeats, task outcomes, api_call_log, and circuit_breaker_events.
# dataViewer alone (read-only) causes silent 403s on all insert_row() calls.
resource "google_project_iam_member" "agent_bq_editor" {
  for_each = local.agents
  project  = var.project_id
  role     = "roles/bigquery.dataEditor"
  member   = "serviceAccount:${each.value}-sa@${var.project_id}.iam.gserviceaccount.com"
}

# ── Grafana CEO Dashboard ─────────────────────────────────────────────────────
# Separate service account for Grafana. Granted BigQuery read-only access so
# the dashboard can query aos_logs.* tables. No agent credentials are shared.

# Import block: grafana-sa was created by a partial apply run (first attempt
# failed on IAM permissions) and now exists in GCP but not in TF state.
# This import block is idempotent — once the resource is in state it is a no-op.
import {
  id = "projects/morphic-gaos-prod/serviceAccounts/grafana-sa@morphic-gaos-prod.iam.gserviceaccount.com"
  to = google_service_account.grafana
}

resource "google_service_account" "grafana" {
  account_id   = "grafana-sa"
  display_name = "Grafana CEO Dashboard"
  project      = var.project_id
}

resource "google_project_iam_member" "grafana_bq_viewer" {
  project = var.project_id
  role    = "roles/bigquery.dataViewer"
  member  = "serviceAccount:${google_service_account.grafana.email}"
}

resource "google_project_iam_member" "grafana_bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.grafana.email}"
}

resource "google_secret_manager_secret_iam_member" "grafana_admin_pw" {
  secret_id = "GRAFANA_ADMIN_PASSWORD"
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.grafana.email}"
}

resource "google_cloud_run_v2_service" "grafana" {
  name     = "grafana"
  location = local.region
  # allUsers access is intentional — Grafana's own login screen is the auth gate.
  # Upgrade to IAP in a future phase if SSO is required.
  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.grafana.email

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = "us-central1-docker.pkg.dev/${var.project_id}/cloud-run-source-deploy/grafana:latest"

      ports {
        container_port = 3000
      }

      resources {
        limits = {
          memory = "512Mi"
          cpu    = "1"
        }
      }

      env {
        name  = "GF_DATABASE_TYPE"
        value = "sqlite3"
      }

      env {
        name = "GF_SECURITY_ADMIN_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = "GRAFANA_ADMIN_PASSWORD"  # pragma: allowlist secret
            version = "latest"
          }
        }
      }
    }

    max_instance_request_concurrency = 10
    timeout                          = "30s"
  }
}

resource "google_cloud_run_v2_service_iam_member" "grafana_public" {
  name     = google_cloud_run_v2_service.grafana.name
  location = local.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "grafana_url" {
  description = "Grafana CEO dashboard Cloud Run URL."
  value       = google_cloud_run_v2_service.grafana.uri
}

# Note: The URLs mentioned in this section are dynamically generated based on the specific Google Cloud project and deployment configuration. They will differ for each new deployment.
