# Console access, two options (var.console_access):
#   cloud_run_iap  (default) IAP directly on the governor's Cloud Run service; URL = its run.app URL.
#   load_balancer  global external HTTPS LB -> serverless NEG -> governor, IAP on the backend
#                  service, custom domain + managed cert + Cloud Armor. Resources below.
resource "google_compute_global_address" "console" {
  count = local.console_direct ? 0 : 1
  name  = "${local.name}-console"
}

resource "google_compute_region_network_endpoint_group" "governor" {
  count                 = local.console_direct ? 0 : 1
  name                  = "${local.name}-governor-neg"
  region                = var.region
  network_endpoint_type = "SERVERLESS"
  cloud_run {
    service = "${local.name}-governor"
  }
}

resource "google_compute_managed_ssl_certificate" "console" {
  count = local.console_direct ? 0 : 1
  # Name tracks the domain so a domain change can create the new cert before
  # detaching the old one (the HTTPS proxy keeps serving during provisioning).
  name = "${local.name}-console-${substr(md5(var.console_domain), 0, 6)}"
  managed {
    domains = [var.console_domain]
  }
  lifecycle {
    create_before_destroy = true
  }
}

resource "google_compute_security_policy" "console" {
  count = local.console_direct ? 0 : 1
  name  = "${local.name}-console"
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
  count                 = local.console_direct ? 0 : 1
  name                  = "${local.name}-console"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTPS"
  security_policy       = google_compute_security_policy.console[0].id
  backend {
    group = google_compute_region_network_endpoint_group.governor[0].id
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
  count           = local.console_direct ? 0 : 1
  name            = "${local.name}-console"
  default_service = google_compute_backend_service.console[0].id
}

resource "google_compute_target_https_proxy" "console" {
  count            = local.console_direct ? 0 : 1
  name             = "${local.name}-console"
  url_map          = google_compute_url_map.console[0].id
  ssl_certificates = [google_compute_managed_ssl_certificate.console[0].id]
}

resource "google_compute_global_forwarding_rule" "console" {
  count                 = local.console_direct ? 0 : 1
  name                  = "${local.name}-console"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_address            = google_compute_global_address.console[0].address
  port_range            = "443"
  target                = google_compute_target_https_proxy.console[0].id
}

resource "google_iap_web_backend_service_iam_member" "console_access" {
  for_each            = local.console_direct ? toset([]) : toset(var.console_access_members)
  web_backend_service = google_compute_backend_service.console[0].name
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
  name     = google_cloud_run_v2_service.governor["console"].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_project_service_identity.iap.email}"
}

# Who may sign in to the console when IAP is enabled directly on Cloud Run.
resource "google_iap_web_cloud_run_service_iam_member" "console_access" {
  for_each               = local.console_direct ? toset(var.console_access_members) : toset([])
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.governor["console"].name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value
}
