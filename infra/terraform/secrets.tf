resource "random_password" "db" {
  for_each = toset(["governor", "runtime"])
  length   = 32
  special  = false
}

resource "random_password" "call_context_key" {
  length  = 64
  special = false
}

locals {
  secrets = {
    "governor-db-password" = { value = random_password.db["governor"].result, accessors = [google_service_account.governor.email] }
    "runtime-db-password"  = { value = random_password.db["runtime"].result, accessors = [google_service_account.runtime.email] }
    # Only the runtime signs and verifies agent-to-agent call contexts.
    "call-context-key" = { value = random_password.call_context_key.result, accessors = [google_service_account.runtime.email] }
  }
  secret_access = merge([
    for name, s in local.secrets : { for a in s.accessors : "${name}/${a}" => { secret = name, member = a } }
  ]...)
}

resource "google_secret_manager_secret" "s" {
  for_each  = local.secrets
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
  depends_on = [google_kms_crypto_key_iam_member.secrets]
}

resource "google_secret_manager_secret_version" "s" {
  for_each    = local.secrets
  secret      = google_secret_manager_secret.s[each.key].id
  secret_data = each.value.value
}

resource "google_secret_manager_secret_iam_member" "access" {
  for_each  = local.secret_access
  secret_id = google_secret_manager_secret.s[each.value.secret].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value.member}"
}

# Optional Gemini Developer API key (only the runtime can read it)
locals {
  google_ai = nonsensitive(var.google_ai_api_key != "")
}

resource "google_secret_manager_secret" "google_ai" {
  count     = local.google_ai ? 1 : 0
  secret_id = "${local.name}-google-ai-api-key"
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

resource "google_secret_manager_secret_version" "google_ai" {
  count       = local.google_ai ? 1 : 0
  secret      = google_secret_manager_secret.google_ai[0].id
  secret_data = var.google_ai_api_key
}

resource "google_secret_manager_secret_iam_member" "google_ai_runtime" {
  count     = local.google_ai ? 1 : 0
  secret_id = google_secret_manager_secret.google_ai[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}
