variable "project_id" {
  description = "GCP project for this environment (one project per environment)."
  type        = string
}

variable "environment" {
  description = "Environment name (dev, staging, prod)."
  type        = string
}

variable "region" {
  description = "Primary region."
  type        = string
  default     = "europe-west4"
}

variable "governor_image" {
  description = "Container image for the governor (Artifact Registry)."
  type        = string
}

variable "runtime_image" {
  description = "Container image for the agent runtime (Artifact Registry)."
  type        = string
}

variable "console_access" {
  description = "cloud_run_iap: IAP directly on the governor's Cloud Run URL (no load balancer, no domain). load_balancer: HTTPS LB + custom domain + IAP + Cloud Armor."
  type        = string
  default     = "cloud_run_iap"
  validation {
    condition     = contains(["cloud_run_iap", "load_balancer"], var.console_access)
    error_message = "console_access must be cloud_run_iap or load_balancer."
  }
}

variable "console_domain" {
  description = "DNS name for the console. Only used when console_access = load_balancer."
  type        = string
  default     = ""
}

variable "console_access_members" {
  description = "Principals allowed through IAP to the console, e.g. [\"group:agent-builders@example.com\"]."
  type        = list(string)
  default     = []
}

variable "iap_oauth_client_id" {
  description = "Optional OAuth client for IAP (load_balancer mode only). Leave empty to use the Google-managed client."
  type        = string
  default     = ""
}

variable "iap_oauth_client_secret" {
  description = "Secret for iap_oauth_client_id."
  type        = string
  default     = ""
  sensitive   = true
}

variable "bootstrap_admins" {
  description = "Emails granted platform_admin in the governor on startup."
  type        = list(string)
  default     = []
}

variable "runtime_ingress" {
  description = "Cloud Run ingress for the runtime: INGRESS_TRAFFIC_INTERNAL_ONLY or INGRESS_TRAFFIC_ALL."
  type        = string
  default     = "INGRESS_TRAFFIC_INTERNAL_ONLY"
}

variable "runtime_invokers" {
  description = "Principals allowed to call the runtime API (Cloud Run invoker), e.g. client service accounts."
  type        = list(string)
  default     = []
}

variable "oidc_issuer" {
  description = "OIDC issuer for end-user tokens on the runtime API (empty = Google ID tokens only)."
  type        = string
  default     = ""
}

variable "oidc_audience" {
  description = "Expected audience for OIDC end-user tokens."
  type        = string
  default     = ""
}

variable "db_tier" {
  description = "Cloud SQL machine tier."
  type        = string
  default     = "db-custom-2-7680"
}

variable "db_high_availability" {
  description = "Regional (HA) Cloud SQL instance."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Protect stateful resources (Cloud SQL, buckets) from deletion."
  type        = bool
  default     = true
}

variable "alert_email" {
  description = "Email for monitoring alerts (empty = no notification channel)."
  type        = string
  default     = ""
}

variable "network_cidr" {
  description = "Subnet range for Cloud Run direct VPC egress."
  type        = string
  default     = "10.10.0.0/24"
}

variable "psa_prefix_length" {
  description = "Prefix length of the private services access range (Cloud SQL)."
  type        = number
  default     = 20
}

variable "min_instances" {
  description = "Minimum Cloud Run instances per service."
  type        = number
  default     = 0
}

variable "enable_nat" {
  description = "Cloud NAT for internet egress (needed only for tools on external hosts)."
  type        = bool
  default     = false
}

variable "tasks_location" {
  description = "Cloud Tasks location. Cloud Tasks is not offered in every region (check `gcloud tasks locations list`); europe-west1 keeps payloads in the EU."
  type        = string
  default     = "europe-west1"
}
