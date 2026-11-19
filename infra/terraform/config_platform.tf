# Configuration-first platform (spec v1.0, 19 Nov 2026): GCP resources for C03-C13.
#
# Off by default (var.enable_config_platform) so existing governor/runtime environments are
# unchanged. Reuses the VPC, Cloud SQL instance, KMS keys, Pub/Sub topic, Artifact Registry and
# naming of the rest of this module. One image (services/launcher/Dockerfile) runs as several
# Cloud Run services; EAP_SERVICE selects the component.
#
# NOT YET VALIDATED with `terraform validate` (no terraform binary in the build session).
# Check google_memorystore_instance arguments against the provider version in versions.tf.

variable "enable_config_platform" {
  description = "Create the configuration-first platform services (registry, workflow, resolver, OPA, Valkey, audit bucket)"
  type        = bool
  default     = false
}

variable "platform_image" {
  description = "Image built from services/launcher/Dockerfile"
  type        = string
  default     = ""
}

variable "opa_image" {
  description = "OPA image; pin a digest in production"
  type        = string
  default     = "docker.io/openpolicyagent/opa:latest-static"
}

variable "audit_retention_days" {
  description = "Audit copy retention (spec 12 default: 7 years, confirm with legal)"
  type        = number
  default     = 2557
}

variable "audit_bucket_lock" {
  description = "Lock the retention policy. IRREVERSIBLE: enable only in production once retention is confirmed"
  type        = bool
  default     = false
}

variable "agent_identities" {
  description = "agentId=principal pairs for agents not on Agent Runtime (C07-F5), e.g. customer-profile=sa@p.iam.gserviceaccount.com"
  type        = list(string)
  default     = []
}

locals {
  cp      = var.enable_config_platform ? 1 : 0
  cp_name = "${local.name}-cp"
  cp_urls = {
    registry = "https://${local.cp_name}-registry-${local.project_number}.${var.region}.run.app"
    workflow = "https://${local.cp_name}-workflow-${local.project_number}.${var.region}.run.app"
    resolver = "https://${local.cp_name}-resolver-${local.project_number}.${var.region}.run.app"
    opa      = "https://${local.cp_name}-opa-${local.project_number}.${var.region}.run.app"
  }
  cp_sas = ["registry", "workflow", "resolver", "opa"]
}

# ------------------------------------------------------------------ identities

resource "google_service_account" "cp" {
  for_each     = var.enable_config_platform ? toset(local.cp_sas) : toset([])
  account_id   = "eap-${var.environment}-cp-${each.key}"
  display_name = "EAP config platform ${each.key}"
}

resource "google_project_iam_member" "cp" {
  for_each = var.enable_config_platform ? {
    "registry/pubsub"  = { sa = "registry", role = "roles/pubsub.publisher" }
    "registry/tasks"   = { sa = "registry", role = "roles/cloudtasks.enqueuer" }
    "registry/sql"     = { sa = "registry", role = "roles/cloudsql.client" }
    "workflow/pubsub"  = { sa = "workflow", role = "roles/pubsub.publisher" }
    "workflow/tasks"   = { sa = "workflow", role = "roles/cloudtasks.enqueuer" }
    "workflow/sql"     = { sa = "workflow", role = "roles/cloudsql.client" }
    "workflow/aiplat"  = { sa = "workflow", role = "roles/aiplatform.user" } # Gen AI evaluation + Agent Platform adapter
    "workflow/trace"   = { sa = "workflow", role = "roles/cloudtrace.agent" }
    "resolver/trace"   = { sa = "resolver", role = "roles/cloudtrace.agent" }
    "registry/trace"   = { sa = "registry", role = "roles/cloudtrace.agent" }
  } : {}
  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.cp[each.value.sa].email}"
}

resource "google_service_account_iam_member" "cp_actas_self" {
  for_each           = var.enable_config_platform ? toset(["registry", "workflow"]) : toset([])
  service_account_id = google_service_account.cp[each.key].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.cp[each.key].email}"
}

# ------------------------------------------------------------------ C04 registry database

resource "google_sql_database" "registry" {
  count    = local.cp
  name     = "registry"
  instance = google_sql_database_instance.pg.name
}

resource "random_password" "registry_db" {
  count   = local.cp
  length  = 32
  special = false
}

# Migration owner. Services log in as IAM users that are granted registry_app (SELECT/INSERT only)
# after the first migration:  GRANT registry_app TO "<sa>@<project>.iam";
resource "google_sql_user" "registry_owner" {
  count    = local.cp
  name     = "registry_owner"
  instance = google_sql_database_instance.pg.name
  password = random_password.registry_db[0].result
}

resource "google_sql_user" "cp_iam" {
  for_each = var.enable_config_platform ? toset(["registry", "workflow"]) : toset([])
  name     = trimsuffix(google_service_account.cp[each.key].email, ".gserviceaccount.com")
  instance = google_sql_database_instance.pg.name
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}

resource "google_secret_manager_secret" "cp" {
  for_each  = var.enable_config_platform ? toset(["registry-db-password", "mock-platform-token"]) : toset([])
  secret_id = "${local.name}-${each.key}"
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
}

resource "google_secret_manager_secret_version" "registry_db" {
  count       = local.cp
  secret      = google_secret_manager_secret.cp["registry-db-password"].id
  secret_data = random_password.registry_db[0].result
}

resource "google_secret_manager_secret_iam_member" "cp" {
  for_each = var.enable_config_platform ? {
    "registry-db-password/registry" = "registry"
    "registry-db-password/workflow" = "workflow"
    "mock-platform-token/workflow"  = "workflow" # C10-F6: adapter credentials from the vault
  } : {}
  secret_id = google_secret_manager_secret.cp[split("/", each.key)[0]].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.cp[each.value].email}"
}

# ------------------------------------------------------------------ C03-F7 audit copy (Bucket Lock)

resource "google_storage_bucket" "audit" {
  count                       = local.cp
  name                        = "${var.project_id}-${local.name}-config-audit"
  location                    = var.region # EU residency (spec 11)
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = local.labels
  encryption {
    default_kms_key_name = google_kms_crypto_key.storage.id
  }
  retention_policy {
    retention_period = var.audit_retention_days * 86400
    is_locked        = var.audit_bucket_lock
  }
  depends_on = [google_kms_crypto_key_iam_member.storage]
}

resource "google_storage_bucket_iam_member" "audit_writer" {
  for_each = var.enable_config_platform ? toset(["registry", "workflow"]) : toset([])
  bucket   = google_storage_bucket.audit[0].name
  role     = "roles/storage.objectCreator" # create only: no overwrite, no delete
  member   = "serviceAccount:${google_service_account.cp[each.key].email}"
}

# ------------------------------------------------------------------ C07 cache: Memorystore for Valkey

resource "google_network_connectivity_service_connection_policy" "valkey" {
  count         = local.cp
  name          = "${local.cp_name}-valkey"
  location      = var.region
  service_class = "gcp-memorystore"
  network       = google_compute_network.vpc.id
  psc_config {
    subnetworks = [google_compute_subnetwork.run.id]
  }
}

resource "google_memorystore_instance" "valkey" {
  count                   = local.cp
  instance_id             = "${local.cp_name}-valkey"
  location                = var.region
  shard_count             = 1
  replica_count           = var.environment == "prod" ? 1 : 0
  node_type               = "SHARED_CORE_NANO"
  engine_version          = "VALKEY_8_0"
  transit_encryption_mode = "TRANSIT_ENCRYPTION_DISABLED" # private PSC endpoint only; enable TLS for prod if required
  authorization_mode      = "AUTH_DISABLED"
  deletion_protection_enabled = var.deletion_protection
  desired_psc_auto_connections {
    network    = google_compute_network.vpc.id
    project_id = var.project_id
  }
  labels     = local.labels
  depends_on = [google_network_connectivity_service_connection_policy.valkey]
}

# ------------------------------------------------------------------ workflow queue (C05/C11)

resource "google_cloud_tasks_queue" "workflow" {
  count    = local.cp
  name     = "${local.cp_name}-workflow"
  location = var.tasks_location # Cloud Tasks is not offered in europe-west4
  retry_config {
    max_attempts  = 10
    min_backoff   = "5s"
    max_backoff   = "300s"
  }
}

# ------------------------------------------------------------------ Cloud Run services

locals {
  cp_registry_db = "postgresql://registry_owner:{password}@${google_sql_database_instance.pg.private_ip_address}:5432/registry?sslmode=require&options=-csearch_path%3Dregistry,public"
  cp_common_env = var.enable_config_platform ? {
    GOOGLE_CLOUD_PROJECT  = var.project_id
    EAP_EVENTS_TOPIC      = google_pubsub_topic.events.id
    EAP_OPA_URL           = local.cp_urls.opa
    EAP_AUDIT_BUCKET      = google_storage_bucket.audit[0].name
    EAP_SECRETS_PROJECT   = var.project_id
    EAP_TASKS_QUEUE       = google_cloud_tasks_queue.workflow[0].id
    EAP_TASKS_BASE_URL    = local.cp_urls.workflow
    EAP_ALLOW_BOOTSTRAP   = var.environment == "dev" ? "true" : "false"
    EAP_ADAPTER           = "mock" # switch to agent-platform once ADR-002 is implemented
    DATABASE_URL          = local.cp_registry_db
  } : {}
  cp_services = var.enable_config_platform ? {
    registry = {
      ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY", min = var.min_instances, iap = false
      env = merge(local.cp_common_env, {
        EAP_SERVICE                = "registry"
        EAP_AUTH_MODES             = "iap,google_id"
        EAP_GOOGLE_AUDIENCES       = local.cp_urls.registry
        EAP_TASKS_SERVICE_ACCOUNT  = google_service_account.cp["workflow"].email
        EAP_INTERNAL_PRINCIPALS    = join(",", [google_service_account.cp["resolver"].email, google_service_account.cp["workflow"].email])
      })
    }
    workflow = {
      ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY", min = 0, iap = false
      env = merge(local.cp_common_env, {
        EAP_SERVICE               = "workflow"
        EAP_AUTH_MODES            = "google_id"
        EAP_GOOGLE_AUDIENCES      = local.cp_urls.workflow
        EAP_TASKS_SERVICE_ACCOUNT = google_service_account.cp["workflow"].email
        EAP_INTERNAL_PRINCIPALS   = google_service_account.cp["workflow"].email
      })
    }
    resolver = {
      # Runtime path (C07): 99.9%, never scaled to zero, survives the authoring path being down.
      ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY", min = max(var.min_instances, 2), iap = false
      env = {
        EAP_SERVICE          = "resolver"
        EAP_AUTH_MODES       = "google_id"
        EAP_GOOGLE_AUDIENCES = local.cp_urls.resolver
        EAP_REGISTRY_URL     = local.cp_urls.registry
        EAP_VALKEY_URL       = "redis://${google_memorystore_instance.valkey[0].discovery_endpoints[0].address}:6379/0"
        EAP_AGENT_IDENTITIES = join(",", var.agent_identities)
        EAP_EVENT_PUSHERS    = google_service_account.pubsub_push.email
        EAP_LKG_DIR          = "/tmp/eap-lkg"
      }
    }
  } : {}
}

resource "google_cloud_run_v2_service" "cp" {
  for_each            = local.cp_services
  name                = "${local.cp_name}-${each.key}"
  location            = var.region
  ingress             = each.value.ingress
  deletion_protection = var.deletion_protection
  labels              = local.labels

  template {
    service_account = google_service_account.cp[each.key].email
    scaling {
      min_instance_count = each.value.min
      max_instance_count = 20
    }
    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.run.id
      }
    }
    containers {
      image = var.platform_image
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
      dynamic "env" {
        for_each = contains(["registry", "workflow"], each.key) ? [1] : []
        content {
          name = "DB_PASSWORD"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.cp["registry-db-password"].secret_id
              version = "latest"
            }
          }
        }
      }
      startup_probe {
        http_get {
          path = "/healthz"
        }
        period_seconds    = 5
        failure_threshold = 12
      }
    }
  }
  depends_on = [google_secret_manager_secret_iam_member.cp]
}

# OPA policy engine (spec 2.3): internal only; policies baked from /policies at deploy time
# (or served as a bundle from GCS: --set=bundles.eap.resource=...).
resource "google_cloud_run_v2_service" "opa" {
  count               = local.cp
  name                = "${local.cp_name}-opa"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  deletion_protection = var.deletion_protection
  labels              = local.labels
  template {
    service_account = google_service_account.cp["opa"].email
    scaling {
      min_instance_count = 1
      max_instance_count = 5
    }
    containers {
      image = var.opa_image
      args  = ["run", "--server", "--addr=0.0.0.0:8080", "--log-level=info"]
      ports {
        container_port = 8080
      }
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "cp_invokers" {
  for_each = var.enable_config_platform ? {
    "registry/resolver" = { svc = "registry", sa = google_service_account.cp["resolver"].email }
    "workflow/workflow" = { svc = "workflow", sa = google_service_account.cp["workflow"].email } # Cloud Tasks OIDC
    "resolver/runtime"  = { svc = "resolver", sa = google_service_account.runtime.email }        # reference agent
    "resolver/pubsub"   = { svc = "resolver", sa = google_service_account.pubsub_push.email }
  } : {}
  name     = google_cloud_run_v2_service.cp[each.value.svc].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${each.value.sa}"
}

resource "google_cloud_run_v2_service_iam_member" "opa_invokers" {
  for_each = var.enable_config_platform ? toset(["registry", "workflow"]) : toset([])
  name     = google_cloud_run_v2_service.opa[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.cp[each.key].email}"
}

# ------------------------------------------------------------------ C07-F3: activation events -> resolver

resource "google_pubsub_subscription" "resolver_invalidation" {
  count                = local.cp
  name                 = "${local.cp_name}-resolver-invalidation"
  topic                = google_pubsub_topic.events.id
  ack_deadline_seconds = 20
  filter               = "attributes.event_type = \"config.version.activated\""
  labels               = local.labels
  push_config {
    push_endpoint = "${local.cp_urls.resolver}/internal/v1/events"
    oidc_token {
      service_account_email = google_service_account.pubsub_push.email
      audience              = local.cp_urls.resolver
    }
  }
  retry_policy {
    minimum_backoff = "1s"
    maximum_backoff = "30s"
  }
  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.events_dlq.id
    max_delivery_attempts = 10
  }
  expiration_policy {
    ttl = ""
  }
}

# ------------------------------------------------------------------ C13 outcome analytics (basic, Phase 2)

resource "google_bigquery_dataset" "outcomes" {
  count                      = local.cp
  dataset_id                 = "eap_${var.environment}_outcomes"
  location                   = var.region
  delete_contents_on_destroy = false
  labels                     = local.labels
}

resource "google_bigquery_table" "outcome_events" {
  count               = local.cp
  dataset_id          = google_bigquery_dataset.outcomes[0].dataset_id
  table_id            = "outcome_events"
  deletion_protection = var.deletion_protection
  time_partitioning {
    type  = "DAY"
    field = "ended_at"
  }
  clustering = ["agent_id", "business_unit"]
  schema = jsonencode([ # spec 7.7
    { name = "journey", type = "STRING", mode = "REQUIRED" },
    { name = "agent_id", type = "STRING", mode = "REQUIRED" },
    { name = "business_unit", type = "STRING", mode = "REQUIRED" },
    { name = "versions", type = "JSON", mode = "REQUIRED" },
    { name = "run_ids", type = "STRING", mode = "REPEATED" },
    { name = "outcome", type = "STRING", mode = "REQUIRED" },
    { name = "started_at", type = "TIMESTAMP", mode = "REQUIRED" },
    { name = "ended_at", type = "TIMESTAMP", mode = "REQUIRED" },
  ])
}

output "config_platform_urls" {
  value = var.enable_config_platform ? local.cp_urls : {}
}
