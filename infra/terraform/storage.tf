resource "google_artifact_registry_repository" "images" {
  repository_id = "${local.name}-images"
  location      = var.region
  format        = "DOCKER"
  labels        = local.labels
  depends_on    = [google_project_service.apis]
}

# Uploaded eval datasets, knowledge source documents and run artifacts. CMEK-encrypted.
resource "google_storage_bucket" "artifacts" {
  name                        = "${var.project_id}-${local.name}-artifacts"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = !var.deletion_protection
  labels                      = local.labels
  encryption {
    default_kms_key_name = google_kms_crypto_key.storage.id
  }
  versioning {
    enabled = true
  }
  lifecycle_rule {
    condition {
      num_newer_versions = 5
    }
    action {
      type = "Delete"
    }
  }
  depends_on = [google_kms_crypto_key_iam_member.storage]
}

resource "google_storage_bucket" "knowledge" {
  name                        = "${var.project_id}-${local.name}-knowledge"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = !var.deletion_protection
  labels                      = local.labels
  encryption {
    default_kms_key_name = google_kms_crypto_key.storage.id
  }
  depends_on = [google_kms_crypto_key_iam_member.storage]
}

resource "google_storage_bucket_iam_member" "artifacts_governor" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.governor.email}"
}

resource "google_storage_bucket_iam_member" "knowledge_runtime" {
  bucket = google_storage_bucket.knowledge.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.runtime.email}"
}
