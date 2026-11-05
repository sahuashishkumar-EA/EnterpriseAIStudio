# Separate identities: the governor and the runtime never share credentials.
resource "google_service_account" "governor" {
  account_id   = "${local.name}-governor"
  display_name = "EAP governor (control plane)"
}

resource "google_service_account" "runtime" {
  account_id   = "${local.name}-runtime"
  display_name = "EAP agent runtime (data plane)"
}

resource "google_service_account" "chat" {
  account_id   = "${local.name}-chat"
  display_name = "EAP demo chat app"
}

resource "google_project_iam_member" "chat" {
  for_each = toset(["roles/logging.logWriter", "roles/monitoring.metricWriter"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.chat.email}"
}

resource "google_service_account" "pubsub_push" {
  account_id   = "${local.name}-push"
  display_name = "EAP Pub/Sub push to runtime"
}

resource "google_service_account" "deployer" {
  account_id   = "${local.name}-deployer"
  display_name = "EAP Cloud Build deployer"
}

# ---------------------------------------------------------------- governor
resource "google_project_iam_member" "governor" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/cloudtrace.agent",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.governor.email}"
}

# ----------------------------------------------------------------- runtime
resource "google_project_iam_member" "runtime" {
  for_each = toset([
    "roles/aiplatform.user",  # Gemini generateContent, Claude rawPredict, embeddings
    "roles/bigquery.jobUser", # read-only query tool; dataset read access is granted per dataset by BUs
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/cloudtrace.agent",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

# Cloud Tasks: services enqueue tasks that call themselves with their own OIDC identity.
resource "google_service_account_iam_member" "runtime_actas_self" {
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_service_account_iam_member" "governor_actas_self" {
  service_account_id = google_service_account.governor.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.governor.email}"
}

# Pub/Sub service agent mints OIDC tokens for the push identity.
resource "google_service_account_iam_member" "pubsub_token_creator" {
  service_account_id = google_service_account.pubsub_push.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${local.project_number}@gcp-sa-pubsub.iam.gserviceaccount.com"
  depends_on         = [google_project_service.apis]
}

# ---------------------------------------------------------------- deployer
resource "google_project_iam_member" "deployer" {
  for_each = toset([
    "roles/run.developer",
    "roles/artifactregistry.writer",
    "roles/logging.logWriter",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "deployer_actas" {
  for_each           = { governor = google_service_account.governor.name, runtime = google_service_account.runtime.name }
  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}
