# Deploying to GCP (first time, from Cloud Shell)

Target: the `dev` environment in one GCP project, region `europe-west4`. Total time is about
45–90 minutes. Most of that is waiting for Cloud SQL (~15 min) and the managed TLS certificate
(15–60 min).

Rough cost while running: about $3–4/day (Cloud SQL ~$1.7, load balancer ~$0.6, the rest is small).
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

## 5. Create the foundations (APIs, image registry, console IP)

```bash
cd infra/terraform
```

```bash
terraform init -backend-config=envs/dev.backend.hcl
```

```bash
terraform apply -var-file=envs/dev.tfvars -target=google_project_service.apis -target=google_artifact_registry_repository.images -target=google_compute_global_address.console
```

Terraform warns that `-target` is for exceptional use. That's expected here.

## 6. Give the console a hostname

The managed certificate needs a DNS name that resolves to the load balancer IP. For dev,
`<ip>.nip.io` works without owning a domain. For prod, use your own domain with an A record.

```bash
export CONSOLE_IP=$(gcloud compute addresses describe eap-dev-console --global --format='value(address)')
```

```bash
sed -i "s|REPLACE.nip.io|${CONSOLE_IP}.nip.io|" envs/dev.tfvars && grep console_domain envs/dev.tfvars
```

## 7. OAuth client for IAP (needed when the project has no organization)

Check whether the project is under an organization:

```bash
gcloud projects describe $PROJECT_ID --format='value(parent.type)'
```

If this prints `organization`, you can try skipping this step (IAP then uses a Google-managed
client). If IAP later shows an OAuth error, come back to it. With a personal Gmail account
(empty output) you need this step:

1. Cloud Console → **Google Auth Platform** (APIs & Services → OAuth consent screen) →
   *Get started*. App name "Agent Governor", audience **External**, your email as the contact.
   Under **Audience → Test users**, add your email.
2. **Clients → Create client** → type *Web application* → Create. Copy the client ID and secret.
3. Edit the client and add this **Authorized redirect URI** (replace CLIENT_ID):
   `https://iap.googleapis.com/v1/oauth/clientIds/CLIENT_ID:handleRedirect`

Pass the values as environment variables, so the secret is not written to a file:

```bash
export TF_VAR_iap_oauth_client_id=CLIENT_ID
```

```bash
export TF_VAR_iap_oauth_client_secret=CLIENT_SECRET
```

## 8. Build and push the images

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

## 9. Create everything else

```bash
cd ~/EnterpriseAIStudio/infra/terraform
```

```bash
terraform apply -var-file=envs/dev.tfvars
```

This takes 15–25 minutes. If it fails with "API ... has not been used" or a service-networking
error, wait 2 minutes and run the same command again. Fresh projects need time for APIs to
propagate.

## 10. Check it's up

```bash
gcloud run services list --region $REGION
```

```bash
gcloud run services logs read eap-dev-governor --region $REGION --limit 30
```

```bash
gcloud run services logs read eap-dev-runtime --region $REGION --limit 30
```

Wait until the certificate status is `ACTIVE`:

```bash
gcloud compute ssl-certificates list --global --format='table(name,managed.domains,managed.status)'
```

Then open the console:

```bash
echo "https://${CONSOLE_IP}.nip.io"
```

Sign in with your Google account. You are `platform_admin` through `bootstrap_admins`.

## 11. Try it

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
