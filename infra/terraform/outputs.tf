output "governor_url" {
  value = google_cloud_run_v2_service.governor.uri
}

output "runtime_url" {
  value = google_cloud_run_v2_service.runtime.uri
}

output "console_ip" {
  description = "Point console_domain's A record here."
  value       = google_compute_global_address.console.address
}

output "artifact_registry" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "events_topic" {
  value = google_pubsub_topic.events.id
}

output "service_accounts" {
  value = {
    governor    = google_service_account.governor.email
    runtime     = google_service_account.runtime.email
    pubsub_push = google_service_account.pubsub_push.email
    deployer    = google_service_account.deployer.email
  }
}

output "sql_instance" {
  value = google_sql_database_instance.pg.connection_name
}
