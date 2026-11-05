# Console: global external HTTPS load balancer -> serverless NEG -> governor, behind IAP.
resource "google_compute_global_address" "console" {
  name = "${local.name}-console"
}

resource "google_compute_region_network_endpoint_group" "governor" {
  name                  = "${local.name}-governor-neg"
  region                = var.region
  network_endpoint_type = "SERVERLESS"
  cloud_run {
    service = "${local.name}-governor"
  }
}

resource "google_compute_managed_ssl_certificate" "console" {
  name = "${local.name}-console"
  managed {
    domains = [var.console_domain]
  }
}

resource "google_compute_security_policy" "console" {
  name = "${local.name}-console"
  rule {
    action   = "allow"
    priority = 2147483647
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
    description = "default allow (IAP authenticates every request)"
  }
  rule {
    action   = "throttle"
    priority = 1000
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = 600
        interval_sec = 60
      }
    }
  }
}

resource "google_compute_backend_service" "console" {
  name                  = "${local.name}-console"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTPS"
  security_policy       = google_compute_security_policy.console.id
  backend {
    group = google_compute_region_network_endpoint_group.governor.id
  }
  iap {
    enabled              = true
    oauth2_client_id     = var.iap_oauth_client_id == "" ? null : var.iap_oauth_client_id
    oauth2_client_secret = var.iap_oauth_client_secret == "" ? null : var.iap_oauth_client_secret
  }
  log_config {
    enable = true
  }
}

resource "google_compute_url_map" "console" {
  name            = "${local.name}-console"
  default_service = google_compute_backend_service.console.id
}

resource "google_compute_target_https_proxy" "console" {
  name             = "${local.name}-console"
  url_map          = google_compute_url_map.console.id
  ssl_certificates = [google_compute_managed_ssl_certificate.console.id]
}

resource "google_compute_global_forwarding_rule" "console" {
  name                  = "${local.name}-console"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_address            = google_compute_global_address.console.address
  port_range            = "443"
  target                = google_compute_target_https_proxy.console.id
}

resource "google_iap_web_backend_service_iam_member" "console_access" {
  for_each            = toset(var.console_access_members)
  web_backend_service = google_compute_backend_service.console.name
  role                = "roles/iap.httpsResourceAccessor"
  member              = each.value
}

# IAP's service agent invokes the governor on behalf of authenticated users.
# It does not exist in a new project until requested, so create it explicitly.
resource "google_project_service_identity" "iap" {
  provider   = google-beta
  service    = "iap.googleapis.com"
  depends_on = [google_project_service.apis]
}

resource "google_cloud_run_v2_service_iam_member" "iap_invoker" {
  name     = google_cloud_run_v2_service.governor.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_project_service_identity.iap.email}"
}
