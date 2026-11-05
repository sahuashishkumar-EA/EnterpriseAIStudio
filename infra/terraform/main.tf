data "google_project" "this" {
  project_id = var.project_id
}

locals {
  name           = "eap-${var.environment}"
  project_number = data.google_project.this.number
  # Deterministic Cloud Run URLs break the governor <-> runtime reference cycle.
  governor_url = "https://${local.name}-governor-${local.project_number}.${var.region}.run.app"
  runtime_url  = "https://${local.name}-runtime-${local.project_number}.${var.region}.run.app"
  labels = {
    app         = "enterprise-agent-platform"
    environment = var.environment
  }
}

resource "google_project_service" "apis" {
  for_each = toset([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "bigquery.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudkms.googleapis.com",
    "cloudtasks.googleapis.com",
    "compute.googleapis.com",
    "iap.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "servicenetworking.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}
