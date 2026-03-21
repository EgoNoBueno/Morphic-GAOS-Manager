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
