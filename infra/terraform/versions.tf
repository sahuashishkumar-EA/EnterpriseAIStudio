terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.12"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 6.12"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
  # One state per environment/project:  terraform init -backend-config=envs/<env>.backend.hcl
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# Only used for google_project_service_identity (service agents for CMEK grants).
provider "google-beta" {
  project = var.project_id
  region  = var.region
}
