# Terraform: one GCP project per environment

Default region: `europe-west4`. All state lives in a GCS bucket per environment.

```bash
cd infra/terraform
terraform init -backend-config=envs/dev.backend.hcl
terraform plan  -var-file=envs/dev.tfvars
terraform apply -var-file=envs/dev.tfvars
```

What it creates:

| File | Resources |
|---|---|
| main.tf | project APIs; deterministic Cloud Run URLs (break the governor ↔ runtime reference cycle) |
| network.tf | VPC, subnet for Cloud Run direct VPC egress, private services access (PSA) for Cloud SQL |
| sql.tf | Cloud SQL Postgres 16, private IP only, CMEK, PITR backups; databases + users `governor`, `runtime` |
| kms.tf | key ring + keys (storage, Cloud SQL, secrets), service-agent grants |
| secrets.tf | Secret Manager (CMEK): DB passwords, call-context signing key (runtime only) |
| iam.tf | separate service accounts: governor, runtime, Pub/Sub push, deployer |
| run.tf | Cloud Run v2 services (governor ingress = load balancer only), env wiring, invoker bindings |
| lb.tf | global HTTPS LB → serverless NEG → governor, managed cert, Cloud Armor rate limit, IAP |
| pubsub.tf | single events topic, push subscription → runtime `/internal/v1/events` (OIDC), dead-letter topic |
| tasks.tf | Cloud Tasks queues: runtime async runs, governor evals |
| storage.tf | Artifact Registry, CMEK buckets (artifacts, knowledge) |
| monitoring.tf | alerts: 5xx, runtime p95 latency, undelivered events / DLQ, Cloud SQL CPU; guardrail-block log metric |

Before the first apply:

1. Create the project and state bucket.
2. Build and push the images (they are needed by Cloud Run). One option: apply
   `google_artifact_registry_repository.images` with `-target`, then run Cloud Build.
3. After the first apply, point `console_domain` at the `console_ip` output. The managed
   certificate becomes active once DNS resolves.

Notes:

- Database isolation: each service connects with its own user to its own database, and the
  `*_isolate_database.pg.sql` migrations revoke `CONNECT` from `PUBLIC`.
- `runtime_ingress` defaults to internal only. Expose the runtime to callers via
  `runtime_invokers` (Cloud Run IAM) plus `INGRESS_TRAFFIC_ALL`, or through an internal load balancer.
- `terraform validate` passed with hashicorp/google 6.12 in the build session. The configuration
  has not been applied to a real project yet.
