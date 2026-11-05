resource "google_cloud_tasks_queue" "runtime_runs" {
  name     = "${local.name}-runtime-runs"
  location = var.region
  rate_limits {
    max_concurrent_dispatches = 50
    max_dispatches_per_second = 20
  }
  retry_config {
    max_attempts  = 3
    min_backoff   = "5s"
    max_backoff   = "120s"
    max_doublings = 3
  }
  depends_on = [google_project_service.apis]
}

resource "google_cloud_tasks_queue" "governor_evals" {
  name     = "${local.name}-governor-evals"
  location = var.region
  rate_limits {
    max_concurrent_dispatches = 10
    max_dispatches_per_second = 5
  }
  retry_config {
    max_attempts = 2
    min_backoff  = "10s"
  }
  depends_on = [google_project_service.apis]
}

resource "google_cloud_tasks_queue_iam_member" "runtime_enqueuer" {
  name     = google_cloud_tasks_queue.runtime_runs.name
  location = var.region
  role     = "roles/cloudtasks.enqueuer"
  member   = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_cloud_tasks_queue_iam_member" "governor_enqueuer" {
  name     = google_cloud_tasks_queue.governor_evals.name
  location = var.region
  role     = "roles/cloudtasks.enqueuer"
  member   = "serviceAccount:${google_service_account.governor.email}"
}
