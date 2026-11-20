# Daily job: re-scan the web pages of live agents whose functional contexts ask for a refresh
# every N days (governor /internal/v1/tasks/rescan-due). Runs as the governor's service account;
# Cloud Scheduler counts as internal traffic for the internal-only governor service.
resource "google_cloud_scheduler_job" "web_refresh" {
  name        = "${local.name}-web-refresh"
  region      = var.tasks_location
  description = "Refresh web knowledge of live agents (functional contexts with web_refresh_days)"
  schedule    = var.web_refresh_schedule
  time_zone   = "Europe/Amsterdam"

  http_target {
    http_method = "POST"
    uri         = "${local.governor_internal_url}/internal/v1/tasks/rescan-due"
    oidc_token {
      service_account_email = google_service_account.governor.email
      audience              = local.governor_internal_url
    }
  }

  retry_config {
    retry_count = 1
  }

  depends_on = [google_project_service.apis]
}
