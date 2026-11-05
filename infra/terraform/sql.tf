resource "google_sql_database_instance" "pg" {
  name                = "${local.name}-pg"
  database_version    = "POSTGRES_16"
  region              = var.region
  encryption_key_name = google_kms_crypto_key.sql.id
  deletion_protection = var.deletion_protection

  settings {
    tier              = var.db_tier
    edition           = "ENTERPRISE"
    availability_type = var.db_high_availability ? "REGIONAL" : "ZONAL"
    disk_autoresize   = true
    user_labels       = local.labels

    ip_configuration {
      ipv4_enabled                                  = false
      private_network                               = google_compute_network.vpc.id
      enable_private_path_for_google_cloud_services = true
      ssl_mode                                      = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "02:00"
      backup_retention_settings {
        retained_backups = 14
      }
    }

    insights_config {
      query_insights_enabled = true
    }

    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }
  }

  depends_on = [google_service_networking_connection.psa, google_kms_crypto_key_iam_member.sql]
}

# Separate databases and users: the runtime cannot read governor tables.
resource "google_sql_database" "db" {
  for_each = toset(["governor", "runtime"])
  name     = each.value
  instance = google_sql_database_instance.pg.name
}

resource "google_sql_user" "u" {
  for_each = toset(["governor", "runtime"])
  name     = each.value
  instance = google_sql_database_instance.pg.name
  password = random_password.db[each.value].result
}

locals {
  # {password} is substituted at startup from DB_PASSWORD (Secret Manager)
  database_urls = {
    for db in ["governor", "runtime"] :
    db => "postgresql://${db}:{password}@${google_sql_database_instance.pg.private_ip_address}:5432/${db}?sslmode=require"
  }
}
