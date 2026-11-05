# Demo chat app: a separate front end (not part of the governor) where signed-in users talk to
# published, chat-enabled agents. IAP directly on Cloud Run; it calls the governor-internal and
# runtime services with its own service account and X-EAP-On-Behalf-Of: <user>.
resource "google_cloud_run_v2_service" "chat" {
  provider            = google-beta # iap_enabled
  name                = "${local.name}-chat"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  iap_enabled         = true
  deletion_protection = var.deletion_protection
  labels              = local.labels

  template {
    service_account = google_service_account.chat.email
    timeout         = "300s"
    scaling {
      min_instance_count = 0
      max_instance_count = 5
    }
    vpc_access {
      egress = "ALL_TRAFFIC" # reach the internal-only runtime and governor-internal
      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.run.id
      }
    }
    containers {
      image = local.chat_image
      ports {
        container_port = 8080
      }
      resources {
        limits = { cpu = "1", memory = "512Mi" }
      }
      env {
        name  = "EAP_AUTH_MODES"
        value = "iap"
      }
      env {
        name  = "EAP_IAP_AUDIENCE"
        value = "/projects/${local.project_number}/locations/${var.region}/services/${local.name}-chat"
      }
      env {
        name  = "EAP_GOVERNOR_URL"
        value = local.governor_internal_url
      }
      env {
        name  = "EAP_RUNTIME_URL"
        value = local.runtime_url
      }
      env {
        name  = "EAP_UPSTREAM_AUTH"
        value = "google_id"
      }
      startup_probe {
        http_get {
          path = "/healthz"
        }
      }
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "chat_iap_invoker" {
  name     = google_cloud_run_v2_service.chat.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_project_service_identity.iap.email}"
}

resource "google_iap_web_cloud_run_service_iam_member" "chat_access" {
  for_each               = toset(local.chat_members)
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.chat.name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value
}
