# Deploying to GCP (first time, from Cloud Shell)

Target: the `dev` environment in one GCP project, region `europe-west4`. Total time is about
30–45 minutes, mostly waiting for Cloud SQL (~15 min).

Rough cost while running: about $2–3/day (Cloud SQL ~$1.7; the load balancer, if enabled, adds ~$0.6).
Run `terraform destroy` when you're done testing.

## 0. Get the latest code

```bash
cd ~/EnterpriseAIStudio && git pull
```

## 1. Set your project

```bash
export PROJECT_ID=your-project-id
```

```bash
export REGION=europe-west4
```

```bash
gcloud config set project $PROJECT_ID
```

Billing must be enabled (`True`):

```bash
gcloud billing projects describe $PROJECT_ID --format='value(billingEnabled)'
```

## 2. Enable bootstrap APIs (Terraform enables the rest)

```bash
gcloud services enable cloudresourcemanager.googleapis.com serviceusage.googleapis.com iam.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com compute.googleapis.com iap.googleapis.com
```

## 3. Create the Terraform state bucket

```bash
gcloud storage buckets create gs://${PROJECT_ID}-tfstate --location=$REGION --uniform-bucket-level-access
```

```bash
gcloud storage buckets update gs://${PROJECT_ID}-tfstate --versioning
```

## 4. Fill in the dev config

```bash
export ME=$(gcloud config get-value account)
```

```bash
sed -i "s/my-eap-dev/${PROJECT_ID}/g; s/:latest/:v1/g; s|group:agent-builders@example.com|user:${ME}|; s|platform-admin@example.com|${ME}|" infra/terraform/envs/dev.tfvars infra/terraform/envs/dev.backend.hcl
```

```bash
cat infra/terraform/envs/dev.tfvars
```

Check that `project_id`, both image paths, `console_access_members` and `bootstrap_admins` now
contain your project and email. Leave `console_domain` as is for now.

## 5. Choose how people reach the console

| `console_access` | URL | Needs | Notes |
|---|---|---|---|
| `cloud_run_iap` (**default**) | the governor's `https://…run.app` URL | nothing extra | IAP directly on Cloud Run: no load balancer, no domain or certificate to wait for. Uses the Google-managed OAuth client. |
| `load_balancer` | `https://<your domain>` | a DNS name + A record; OAuth client if the project has no organization | Adds Cloud Armor rate limiting and a custom domain. Used in `prod.tfvars`. |

For `cloud_run_iap` there is nothing to set. `console_domain` is ignored in this mode.

For `load_balancer`, add `console_access = "load_balancer"` and `console_domain = "<host>"` to
`envs/dev.tfvars`. Create an A record for the host pointing at the `console_ip` output after
apply. Many corporate web filters block `nip.io`, so use a real domain. If the project has no
organization, also create an OAuth client and set `TF_VAR_iap_oauth_client_id` /
`TF_VAR_iap_oauth_client_secret`: Google Auth Platform → consent screen *External* with yourself
as a test user → *Web application* client → redirect URI
`https://iap.googleapis.com/v1/oauth/clientIds/CLIENT_ID:handleRedirect`.

## 6. Create the foundations (APIs and the image registry)

```bash
cd infra/terraform
```

```bash
terraform init -upgrade -backend-config=envs/dev.backend.hcl
```

```bash
terraform apply -var-file=envs/dev.tfvars -target=google_project_service.apis -target=google_artifact_registry_repository.images
```

Terraform warns that `-target` is for exceptional use. That's expected here.

## 7. Build and push the images

```bash
cd ~/EnterpriseAIStudio
```

```bash
gcloud builds submit --config cloudbuild.images.yaml --substitutions=_REPO=eap-dev-images,_TAG=v1
```

If the push fails with a permission error, grant the build service account write access and
rerun the build:

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID --member="serviceAccount:$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')-compute@developer.gserviceaccount.com" --role=roles/artifactregistry.writer
```

## 8. Create everything else

```bash
cd ~/EnterpriseAIStudio/infra/terraform
```

```bash
terraform apply -var-file=envs/dev.tfvars
```

This takes 15–25 minutes. If it fails with "API ... has not been used" or a service-networking
error, wait 2 minutes and run the same command again. Fresh projects need time for APIs to
propagate.

## 9. Check it's up

```bash
gcloud run services list --region $REGION
```

You should see three services: `eap-dev-governor` (console), `eap-dev-governor-internal` and
`eap-dev-runtime`.

```bash
gcloud run services logs read eap-dev-governor --region $REGION --limit 30
```

Print the console URL:

```bash
terraform output -raw console_url
```

Open it and sign in with Google. Only accounts in `console_access_members` get through IAP, and
you are `platform_admin` through `bootstrap_admins`. The `-internal` URL is meant to be
unreachable from outside.

If sign-in says the app is restricted to users of an organization (possible on projects without
an organization), switch to `console_access = "load_balancer"` with an OAuth client (step 5).

## 10. Try it

1. **Roles** → grant yourself `bu_admin` on a BU (e.g. `platform-quality`).
2. **Agents** → New agent `evaluator`, BU `platform-quality`. Open it, click **Toggle JSON**,
   paste `examples/agents/evaluator.json`, and click **Save draft** → **Validate**.
3. **Evals** → name `regression`, upload `examples/datasets/evaluator.json`, Save suite →
   **Run all suites on draft**. This runs on the runtime through the governor.
4. **Approve & publish**: separation of duties stops you approving your own edit. Either
   - sign in as a second Google account (add it to `console_access_members`, and as an OAuth
     test user, then re-apply), or
   - for **dev only**: **Policies** → platform → set `"separation_of_duties": false` → Save.

   Then Approve, and Publish with alias `prod`.

Two things are not reachable from Cloud Shell in this setup: the runtime API (internal ingress
by default) and knowledge ingestion (RAG data for `entity-resolver`).

## Later deployments

Build with a new tag, then apply with the new image vars:

```bash
gcloud builds submit --config cloudbuild.images.yaml --substitutions=_REPO=eap-dev-images,_TAG=v2
```

```bash
terraform apply -var-file=envs/dev.tfvars -var governor_image=$REGION-docker.pkg.dev/$PROJECT_ID/eap-dev-images/governor:v2 -var runtime_image=$REGION-docker.pkg.dev/$PROJECT_ID/eap-dev-images/runtime:v2
```

For CI, connect a Cloud Build trigger to `cloudbuild.yaml`.

## Tear down

```bash
terraform destroy -var-file=envs/dev.tfvars
```

KMS key rings can't be deleted in GCP. The keys are scheduled for destruction, and the empty
key ring stays.
