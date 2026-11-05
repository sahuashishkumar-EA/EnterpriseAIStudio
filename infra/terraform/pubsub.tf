# One topic for all platform events; consumers filter on the event_type attribute.
resource "google_pubsub_topic" "events" {
  name                       = "${local.name}-events"
  labels                     = local.labels
  message_retention_duration = "86400s"
  depends_on                 = [google_project_service.apis]
}

resource "google_pubsub_topic" "events_dlq" {
  name   = "${local.name}-events-dlq"
  labels = local.labels
}

resource "google_pubsub_topic_iam_member" "governor_publisher" {
  topic  = google_pubsub_topic.events.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.governor.email}"
}

# Runtime cache invalidation (alias.moved, version.*, policy.updated, model.status_changed ...)
resource "google_pubsub_subscription" "runtime_invalidation" {
  name                 = "${local.name}-runtime-invalidation"
  topic                = google_pubsub_topic.events.id
  ack_deadline_seconds = 20
  labels               = local.labels

  push_config {
    push_endpoint = "${local.runtime_url}/internal/v1/events"
    oidc_token {
      service_account_email = google_service_account.pubsub_push.email
      audience              = local.runtime_url
    }
  }

  retry_policy {
    minimum_backoff = "5s"
    maximum_backoff = "60s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.events_dlq.id
    max_delivery_attempts = 10
  }

  expiration_policy {
    ttl = ""
  }
}

resource "google_pubsub_subscription" "events_dlq_pull" {
  name   = "${local.name}-events-dlq"
  topic  = google_pubsub_topic.events_dlq.id
  labels = local.labels
}

# The Pub/Sub service agent must be able to publish dead letters and ack the source subscription.
resource "google_pubsub_topic_iam_member" "dlq_publisher" {
  topic  = google_pubsub_topic.events_dlq.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:service-${local.project_number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_subscription_iam_member" "dlq_subscriber" {
  subscription = google_pubsub_subscription.runtime_invalidation.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:service-${local.project_number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}
