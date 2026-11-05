locals {
  service_accounts = join(",", [
    "governor=${google_service_account.governor.email}",
    "runtime=${google_service_account.runtime.email}",
    "pubsub-push=${google_service_account.pubsub_push.email}",
  ])

  # The governor image runs as two Cloud Run services sharing one database:
  #   console   - web console + public API for people, behind IAP
  #               (IAP directly on Cloud Run, or on the HTTPS load balancer; see var.console_access)
  #   internal  - service-to-service API (runtime resolve, Cloud Tasks callbacks), internal ingress, no IAP
  # Keeping machine traffic off the IAP-protected service avoids IAP in the runtime's hot path.
  governor_common_env = {
    EAP_SERVICE_ACCOUNTS      = local.service_accounts
    EAP_INTERNAL_CALLERS      = "runtime"
    EAP_RUNTIME_URL           = local.runtime_url
    EAP_RUNTIME_AUTH          = "google_id"
    EAP_EVENTS_TOPIC          = google_pubsub_topic.events.id
    EAP_TASKS_QUEUE           = google_cloud_tasks_queue.governor_evals.id
    EAP_TASKS_BASE_URL        = local.governor_internal_url
    EAP_TASKS_SERVICE_ACCOUNT = google_service_account.governor.email
    EAP_BOOTSTRAP_ADMINS      = join(",", var.bootstrap_admins)
    DATABASE_URL              = local.database_urls["governor"]
    GOOGLE_CLOUD_PROJECT      = var.project_id
  }

  # Audience of the IAP JWT differs between IAP on Cloud Run and IAP on a backend service.
  iap_audience = local.console_direct ? (
    "/projects/${local.project_number}/locations/${var.region}/services/${local.name}-governor"
    ) : (
    "/projects/${local.project_number}/global/backendServices/${google_compute_backend_service.console[0].generated_id}"
  )

  governor_services = {
    console = {
      name    = "${local.name}-governor"
      ingress = local.console_direct ? "INGRESS_TRAFFIC_ALL" : "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
      iap     = local.console_direct
      env = merge(local.governor_common_env, {
        EAP_AUTH_MODES       = "iap"
        EAP_IAP_AUDIENCE     = local.iap_audience
        EAP_GOOGLE_AUDIENCES = local.governor_url
        EAP_BASE_URL         = local.console_url
      })
    }
    internal = {
      name    = "${local.name}-governor-internal"
      ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"
      iap     = false
      env = merge(local.governor_common_env, {
        EAP_AUTH_MODES       = "google_id"
        EAP_GOOGLE_AUDIENCES = local.governor_internal_url
        EAP_BASE_URL         = local.governor_internal_url
      })
    }
  }

  runtime_env = {
    EAP_AUTH_MODES            = var.oidc_issuer == "" ? "google_id" : "google_id,oidc"
    EAP_GOOGLE_AUDIENCES      = local.runtime_url
    EAP_OIDC_ISSUER           = var.oidc_issuer
    EAP_OIDC_AUDIENCE         = var.oidc_audience
    EAP_SERVICE_ACCOUNTS      = local.service_accounts
    EAP_EXECUTE_CALLERS       = "governor"
    EAP_EVENT_CALLERS         = "pubsub-push"
    EAP_TASK_CALLERS          = "runtime"
    EAP_TRUSTED_SERVICES      = "governor,runtime"
    EAP_GOVERNOR_URL          = local.governor_internal_url
    EAP_GOVERNOR_AUTH         = "google_id"
    EAP_TASKS_QUEUE           = google_cloud_tasks_queue.runtime_runs.id
    EAP_TASKS_BASE_URL        = local.runtime_url
    EAP_TASKS_SERVICE_ACCOUNT = google_service_account.runtime.email
    EAP_EMBEDDING_LOCATION    = var.region
    DATABASE_URL              = local.database_urls["runtime"]
    GOOGLE_CLOUD_PROJECT      = var.project_id
  }
}

moved {
  from = google_cloud_run_v2_service.governor
  to   = google_cloud_run_v2_service.governor["console"]
}

resource "google_cloud_run_v2_service" "governor" {
  # google-beta: iap_enabled (IAP directly on Cloud Run) is only in the beta provider.
  provider            = google-beta
  for_each            = local.governor_services
  name                = each.value.name
  location            = var.region
  ingress             = each.value.ingress
  iap_enabled         = each.value.iap
  deletion_protection = var.deletion_protection
  labels              = local.labels

  template {
    service_account = google_service_account.governor.email
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = 10
    }
    vpc_access {
      # All egress via the VPC so calls to the internal-only runtime count as internal traffic.
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.run.id
      }
    }
    containers {
      image = var.governor_image
      ports {
        container_port = 8080
      }
      resources {
        limits = { cpu = "1", memory = "1Gi" }
      }
      dynamic "env" {
        for_each = each.value.env
        content {
          name  = env.key
          value = env.value
        }
      }
      env {
        name = "DB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.s["governor-db-password"].secret_id
            version = "latest"
          }
        }
      }
      startup_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 2
        period_seconds        = 5
        failure_threshold     = 12
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.access, google_sql_database.db]
}

resource "google_cloud_run_v2_service" "runtime" {
  name                = "${local.name}-runtime"
  location            = var.region
  ingress             = var.runtime_ingress
  deletion_protection = var.deletion_protection
  labels              = local.labels

  template {
    service_account = google_service_account.runtime.email
    timeout         = "900s"
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = 50
    }
    max_instance_request_concurrency = 40
    vpc_access {
      # All egress via the VPC: governor calls are internal; external tool hosts need Cloud NAT (enable_nat).
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.run.id
      }
    }
    containers {
      image = var.runtime_image
      ports {
        container_port = 8080
      }
      resources {
        limits = { cpu = "2", memory = "2Gi" }
      }
      dynamic "env" {
        for_each = local.runtime_env
        content {
          name  = env.key
          value = env.value
        }
      }
      env {
        name = "DB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.s["runtime-db-password"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "EAP_CALL_CONTEXT_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.s["call-context-key"].secret_id
            version = "latest"
          }
        }
      }
      startup_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 2
        period_seconds        = 5
        failure_threshold     = 12
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.access, google_sql_database.db]
}

# ------------------------------------------------------------ who may call whom
resource "google_cloud_run_v2_service_iam_member" "governor_internal_invokers" {
  for_each = {
    runtime  = "serviceAccount:${google_service_account.runtime.email}"  # resolve API
    governor = "serviceAccount:${google_service_account.governor.email}" # Cloud Tasks eval callbacks
  }
  name     = google_cloud_run_v2_service.governor["internal"].name
  location = var.region
  role     = "roles/run.invoker"
  member   = each.value
}

resource "google_cloud_run_v2_service_iam_member" "runtime_invokers" {
  for_each = merge(
    {
      governor = "serviceAccount:${google_service_account.governor.email}"    # eval execute
      push     = "serviceAccount:${google_service_account.pubsub_push.email}" # events
      runtime  = "serviceAccount:${google_service_account.runtime.email}"     # Cloud Tasks -> self
    },
    { for m in var.runtime_invokers : m => m }
  )
  name     = google_cloud_run_v2_service.runtime.name
  location = var.region
  role     = "roles/run.invoker"
  member   = each.value
}
