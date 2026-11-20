# External agents (e.g. Microsoft Copilot Studio) stay governed by the governor:
#
#  * edge service: the runtime image in EAP_EDGE mode, public, serving only the governance API
#    (/gov/v1: profile, check/input, check/output, runs), /mcp and /healthz. Callers authenticate with
#    OIDC tokens from your identity provider (Entra ID: client-credentials token of the connector's app);
#    each agent lists its allowed callers in hosting.callers. Cloud Run IAM is disabled for this service
#    because Entra tokens are not Google identities: authentication happens in the application.
#  * external agent secrets: Direct Line secrets / bearer tokens / webhook signing keys, referenced by
#    name in hosting.auth_secret and hosting.webhook_secret. Terraform creates empty secrets; add the
#    values with `gcloud secrets versions add`. They are mounted on the governor as environment variables.

locals {
  edge     = var.enable_edge
  edge_url = local.edge ? "https://${local.name}-edge-${local.project_number}.${var.region}.run.app" : ""
  edge_env = merge(local.runtime_env, {
    EAP_EDGE             = "true"
    EAP_AUTH_MODES       = "oidc"
    EAP_OIDC_ISSUER      = var.edge_oidc_issuer
    EAP_OIDC_AUDIENCE    = var.edge_oidc_audience
    EAP_OIDC_ROLES_CLAIM = "roles"
    EAP_GOOGLE_AUDIENCES = ""
  })
}

resource "google_cloud_run_v2_service" "edge" {
  count                = local.edge ? 1 : 0
  name                 = "${local.name}-edge"
  location             = var.region
  ingress              = "INGRESS_TRAFFIC_ALL"
  invoker_iam_disabled = true # OIDC (Entra ID) is verified by the application
  deletion_protection  = var.deletion_protection
  labels               = local.labels

  template {
    service_account = google_service_account.runtime.email
    timeout         = "60s"
    scaling {
      min_instance_count = 0
      max_instance_count = 20
    }
    vpc_access {
      egress = "ALL_TRAFFIC" # resolve definitions from governor-internal
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
        limits = { cpu = "1", memory = "1Gi" }
      }
      dynamic "env" {
        for_each = local.edge_env
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

  lifecycle {
    precondition {
      condition     = var.edge_oidc_issuer != "" && var.edge_oidc_audience != ""
      error_message = "enable_edge needs edge_oidc_issuer and edge_oidc_audience (e.g. Entra ID: https://login.microsoftonline.com/<tenant-id>/v2.0 and api://<app-id>)."
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.access, google_sql_database.db]
}

# Secrets for external agents, readable by the governor only.
resource "google_secret_manager_secret" "external" {
  for_each  = toset(var.external_agent_secrets)
  secret_id = "${local.name}-ext-${lower(replace(each.key, "_", "-"))}"
  labels    = local.labels
  replication {
    user_managed {
      replicas {
        location = var.region
        customer_managed_encryption {
          kms_key_name = google_kms_crypto_key.secrets.id
        }
      }
    }
  }
  depends_on = [google_kms_crypto_key_iam_member.secrets]
}

resource "google_secret_manager_secret_iam_member" "external_governor" {
  for_each  = toset(var.external_agent_secrets)
  secret_id = google_secret_manager_secret.external[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.governor.email}"
}
