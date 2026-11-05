resource "google_kms_key_ring" "eap" {
  name       = "${local.name}-keys"
  location   = var.region
  depends_on = [google_project_service.apis]
}

resource "google_kms_crypto_key" "storage" {
  name            = "storage"
  key_ring        = google_kms_key_ring.eap.id
  rotation_period = "7776000s" # 90 days
}

resource "google_kms_crypto_key" "sql" {
  name            = "cloudsql"
  key_ring        = google_kms_key_ring.eap.id
  rotation_period = "7776000s"
}

resource "google_kms_crypto_key" "secrets" {
  name            = "secrets"
  key_ring        = google_kms_key_ring.eap.id
  rotation_period = "7776000s"
}

data "google_storage_project_service_account" "gcs" {}

resource "google_project_service_identity" "sql" {
  provider = google-beta
  service  = "sqladmin.googleapis.com"
}

resource "google_project_service_identity" "secretmanager" {
  provider = google-beta
  service  = "secretmanager.googleapis.com"
}

resource "google_kms_crypto_key_iam_member" "storage" {
  crypto_key_id = google_kms_crypto_key.storage.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${data.google_storage_project_service_account.gcs.email_address}"
}

resource "google_kms_crypto_key_iam_member" "sql" {
  crypto_key_id = google_kms_crypto_key.sql.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_project_service_identity.sql.email}"
}

resource "google_kms_crypto_key_iam_member" "secrets" {
  crypto_key_id = google_kms_crypto_key.secrets.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_project_service_identity.secretmanager.email}"
}
