data "google_project" "this" {
  project_id = var.project_id
}

locals {
  name           = "eap-${var.environment}"
  project_number = data.google_project.this.number
  # Deterministic Cloud Run URLs break the governor <-> runtime reference cycle.
  governor_url          = "https://${local.name}-governor-${local.project_number}.${var.region}.run.app"
  governor_internal_url = "https://${local.name}-governor-internal-${local.project_number}.${var.region}.run.app"
  console_direct        = var.console_access == "cloud_run_iap"
  chat_url              = "https://${local.name}-chat-${local.project_number}.${var.region}.run.app"
  relaxed_release       = coalesce(var.relaxed_release, var.environment == "dev")
  chat_image            = var.chat_image != "" ? var.chat_image : replace(var.governor_image, "/governor:", "/chat:")
  chat_members          = length(var.chat_access_members) > 0 ? var.chat_access_members : var.console_access_members
  console_url           = local.console_direct ? local.governor_url : "https://${var.console_domain}"
  runtime_url           = "https://${local.name}-runtime-${local.project_number}.${var.region}.run.app"
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
