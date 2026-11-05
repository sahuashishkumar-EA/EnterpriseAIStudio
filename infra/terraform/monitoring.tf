resource "google_monitoring_notification_channel" "email" {
  count        = var.alert_email == "" ? 0 : 1
  display_name = "${local.name} alerts"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
}

locals {
  channels = google_monitoring_notification_channel.email[*].id
  run_services = {
    governor          = google_cloud_run_v2_service.governor["console"].name
    governor-internal = google_cloud_run_v2_service.governor["internal"].name
    chat              = google_cloud_run_v2_service.chat.name
    runtime           = google_cloud_run_v2_service.runtime.name
  }
}

resource "google_monitoring_alert_policy" "run_5xx" {
  for_each     = local.run_services
  display_name = "${each.value}: 5xx responses"
  combiner     = "OR"
  conditions {
    display_name = "5xx rate > 1/s for 5 min"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"${each.value}\" AND metric.type = \"run.googleapis.com/request_count\" AND metric.labels.response_code_class = \"5xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "300s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_RATE"
      }
    }
  }
  notification_channels = local.channels
}

resource "google_monitoring_alert_policy" "runtime_latency" {
  display_name = "${local.name}-runtime: p95 latency"
  combiner     = "OR"
  conditions {
    display_name = "p95 request latency > 60s"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"${google_cloud_run_v2_service.runtime.name}\" AND metric.type = \"run.googleapis.com/request_latencies\""
      comparison      = "COMPARISON_GT"
      threshold_value = 60000
      duration        = "600s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_PERCENTILE_95"
      }
    }
  }
  notification_channels = local.channels
}

resource "google_monitoring_alert_policy" "events_backlog" {
  display_name = "${local.name}: cache-invalidation events not delivered"
  combiner     = "OR"
  conditions {
    display_name = "oldest unacked message > 5 min"
    condition_threshold {
      filter          = "resource.type = \"pubsub_subscription\" AND resource.labels.subscription_id = \"${google_pubsub_subscription.runtime_invalidation.name}\" AND metric.type = \"pubsub.googleapis.com/subscription/oldest_unacked_message_age\""
      comparison      = "COMPARISON_GT"
      threshold_value = 300
      duration        = "300s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
  conditions {
    display_name = "messages in dead-letter topic"
    condition_threshold {
      filter          = "resource.type = \"pubsub_subscription\" AND resource.labels.subscription_id = \"${google_pubsub_subscription.events_dlq_pull.name}\" AND metric.type = \"pubsub.googleapis.com/subscription/num_undelivered_messages\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "60s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
  notification_channels = local.channels
}

resource "google_monitoring_alert_policy" "sql_cpu" {
  display_name = "${local.name}-pg: CPU > 80%"
  combiner     = "OR"
  conditions {
    display_name = "Cloud SQL CPU utilization"
    condition_threshold {
      filter          = "resource.type = \"cloudsql_database\" AND resource.labels.database_id = \"${var.project_id}:${google_sql_database_instance.pg.name}\" AND metric.type = \"cloudsql.googleapis.com/database/cpu/utilization\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.8
      duration        = "600s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }
  notification_channels = local.channels
}

# Guardrail blocks and budget exhaustion are logged by the runtime; surface spikes.
resource "google_logging_metric" "guardrail_blocks" {
  name   = "${local.name}-guardrail-blocks"
  filter = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${google_cloud_run_v2_service.runtime.name}\" AND textPayload:\"guardrail_blocked\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}
