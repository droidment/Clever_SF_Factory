# Google Cloud migration runbook

> **Update, 2026-11-18.** Temporal has been removed. Durable execution now runs on PostgreSQL alone; see [the Postgres runtime](postgres-runtime.md), which supersedes what this document says about Temporal Cloud, its API key and the separate dispatcher pool. `python -m factory.cloud` now renders three manifests (API, worker, migrate). Those passages are kept as the record of the original design and have not been rewritten yet. `FACTORY_MODE=temporal` is now `FACTORY_MODE=gateway` with `FACTORY_RUNTIME=postgres`.

This runbook describes the future Temporal/execution-gateway topology. Its four manifests have offline configuration tests but have not been deployed. The current Anthropic draft workspace has a separate deployment in `sffactorygpt`; see [the draft workspace runbook](gcp-draft-deployment.md). Cloud resources now exist and incur charges.

## Deployment layout

Deploy the same immutable image in four roles:

| Role | Google Cloud resource | Entrypoint | Scaling |
| --- | --- | --- | --- |
| API and console | Cloud Run service | `python -m factory.server` | 0–3 instances by default |
| Workflow/activity worker | Cloud Run worker pool | `python -m factory.worker` | 1 instance initially |
| Transactional outbox dispatcher | Cloud Run worker pool | `python -m factory.dispatch` | 1 instance |
| Initial schema bootstrap | Cloud Run job | `python -m factory.migrate` | One task, no automatic retries |

The API writes requests and commands to PostgreSQL before responding. Its embedded dispatcher is disabled. Background processes keep polling independently when the web service scales down. Worker pools support continuous background workloads and manual instance counts; they do not supply a public web endpoint. [Cloud Run worker pools](https://docs.cloud.google.com/run/docs/deploy-worker-pools).

Use Cloud SQL for PostgreSQL for application records, Temporal Cloud for workflow history, Artifact Registry for images, and Secret Manager for credentials. The Compose Temporal development server and its local volume are not a production deployment strategy.

The container listens on `0.0.0.0:$PORT` (8000 locally) and runs as UID 10001. The API trusts forwarded HTTPS headers inside Cloud Run. Workers and the dispatcher handle SIGTERM; the worker allows eight seconds of graceful drain before activity cancellation. Activities must remain idempotent because a platform termination can still force a retry. [Container contract](https://docs.cloud.google.com/run/docs/container-contract).

## Resources to provision when deployment is authorized

1. Select a project and region with Cloud Run worker-pool support. Enable Cloud Run, Cloud Build, Artifact Registry, Cloud SQL Admin and Secret Manager APIs. Use an Artifact Registry Docker repository named `factory`, or change the build substitution.
2. Create Cloud SQL PostgreSQL in that region with a `factory` database. For production, configure regional HA, automated backups, point-in-time recovery, deletion protection and monitoring. Confirm capacity against API scaling and worker concurrency before rollout.
3. Create a Temporal Cloud namespace and namespace-scoped API key. Use its actual endpoint and namespace; TLS is always enabled in the rendered manifests. [Temporal Cloud API keys](https://docs.temporal.io/cloud/api-keys).
4. Supply the real HTTPS agent gateway and enterprise OIDC issuer/audience/JWKS. The gateway must implement the [execution contract](gateway.md). The current demo does not implement a production execution gateway or an enterprise browser sign-in flow.
5. Create four service accounts and corresponding database users, one per role. Grant every runtime service account `roles/cloudsql.client` and Secret Manager accessor **on only the secrets referenced by its manifest**. Do not grant runtime accounts Editor, Owner, deployment privileges or repository administrator credentials. Database grants are separate from Google IAM: the migrator needs schema/table creation; API and worker need application-table DML and sequence access; dispatcher needs SELECT/UPDATE on `outbox`. Set grants and default privileges after bootstrap for the runtime database users.
6. Give the build identity Artifact Registry writer and the logging permissions required by your build policy. The deployment identity needs Cloud Run resource administration and permission to act as the four runtime accounts. Keep deployment permissions separate from runtime permissions.

The supplied manifests use the built-in Cloud SQL Auth Proxy through `/cloudsql/PROJECT:REGION:INSTANCE`. For this baseline, configure the database instance with public IP but **no authorized networks**, and connect only through the authenticated proxy. Traffic to Cloud SQL is encrypted by the proxy; do not expose PostgreSQL directly or add `0.0.0.0/0`. A private-IP-only organization should adapt the connection path to its approved VPC/proxy design before deployment; the supplied manifests do not provision that network. [Cloud Run to Cloud SQL](https://docs.cloud.google.com/sql/docs/postgres/connect-run), [Cloud SQL Auth Proxy](https://docs.cloud.google.com/sql/docs/postgres/connect-auth-proxy).

Create Secret Manager versions for each database URL, the Temporal key and the gateway token. The database URL format is:

```text
postgresql+psycopg://ROLE_USER:URL_ENCODED_PASSWORD@/factory?host=/cloudsql/PROJECT:REGION:INSTANCE&connect_timeout=10
```

Each database URL must use its role-specific database user and the same instance socket configured in `cloud_sql_instance`. Store values through your approved secret-management process, never in the JSON config or command history. The API and migration job receive neither the Temporal key nor gateway token. The dispatcher receives only the Temporal key; the worker receives both. Numeric secret versions make releases reproducible. [Cloud Run secret configuration](https://docs.cloud.google.com/run/docs/configuring/workerpools/secrets).

## Prepare and review locally

```powershell
Copy-Item deploy/gcp/config.example.json deploy/gcp/config.local.json
# Edit config.local.json with the real resource names, endpoints, secret versions and image digest.
.\.venv\Scripts\python.exe -m factory.cloud deploy/gcp/config.local.json
```

This command makes no network calls. It creates `api.yaml`, `worker.yaml`, `dispatcher.yaml` and `migrate.yaml` under `deploy/gcp/rendered`. Their JSON syntax is valid YAML. The example's zero digest and example endpoints are placeholders, not deployable resources. The renderer requires an image digest, pinned secret versions, HTTPS endpoints, exact repository names and separate service accounts. Local configuration and rendered files are ignored by Git and cloud-build uploads.

Review those four files. They follow the [Cloud Run manifest reference](https://docs.cloud.google.com/run/docs/reference/yaml/v1). The schema job only initializes version 1; it is not a schema-upgrade framework. Future changes require reviewed forward migrations.

## Build and deploy later

The following commands publish artifacts, change cloud resources and can incur costs. Run them only for the selected, prepared project. They have not been run during local preparation.

Use a current Google Cloud CLI that supports `gcloud run worker-pools`. The installed CLI observed during preparation was 561.0.0 and did not expose this GA command group; update it before deployment. Do not rely on a help command silently installing components.

```powershell
$project = 'YOUR_PROJECT_ID'
$region = 'us-central1'
gcloud builds submit --project=$project --region=$region --config=deploy/gcp/cloudbuild.yaml --substitutions="_REGION=$region,_REPOSITORY=factory" .
```

The build pushes a unique `BUILD_ID` tag. Resolve that pushed image to its `sha256` digest in Artifact Registry and copy its full digest URL to `config.local.json`; never deploy a mutable `latest` tag. Render again. Record the image digest, source revision, secret versions and all four manifests as the release bundle.

```powershell
.\.venv\Scripts\python.exe -m factory.cloud deploy/gcp/config.local.json
gcloud run jobs replace deploy/gcp/rendered/migrate.yaml --project=$project --region=$region
gcloud run jobs execute software-factory-migrate --wait --project=$project --region=$region
# Stop if the job failed. Apply reviewed database grants before starting runtime roles.
gcloud run worker-pools replace deploy/gcp/rendered/worker.yaml --project=$project --region=$region
gcloud run worker-pools replace deploy/gcp/rendered/dispatcher.yaml --project=$project --region=$region
gcloud run services replace deploy/gcp/rendered/api.yaml --project=$project --region=$region
```

Check each command's exit status before proceeding. Adjust the job name if you changed `name` in configuration. The migration job must complete successfully before either background pool or the API starts. A successful worker-pool deployment does not establish successful polling: verify worker logs, Temporal pollers, database connectivity and outbox delivery.

API ingress is restricted to internal traffic and Cloud Load Balancing; the manifests do not disable Cloud Run IAM or grant `allUsers`. Choose an approved internal caller or authenticated load-balancer/IAP integration before browser access. Cloud Run invocation authentication and application OIDC authorization are two separate checks. An IAP identity is not automatically an application token with `tid`, `roles` and `repos` claims. Finish the BFF/PKCE login integration and explicit claim mapping before a broader release; do not switch to demo authentication to bypass it. For API clients needing both tokens, Cloud Run supports its invocation token in `X-Serverless-Authorization` while the application token uses `Authorization`. [Service authentication](https://docs.cloud.google.com/run/docs/authenticating/service-to-service).

## Acceptance, monitoring and rollback

- From an authorized caller, verify `/healthz`, `/readyz`, OIDC role/tenant isolation and a single non-deploying test request. Confirm an outbox command becomes delivered, a Temporal execution starts, evidence is persisted, and the console resumes its event stream after reconnecting.
- Exercise approval pause/resume and stop/restart a worker in staging. Confirm real gateway idempotency and evidence checks before allowing a deployment-capable request.
- Monitor the oldest undelivered outbox row, dispatcher error logs, Temporal schedule-to-start latency, failed activities, Cloud SQL connections/storage and API error rates. API readiness checks its database and any local dispatcher; when the dispatcher is separate, `/readyz` does **not** certify background-pool health. Worker pools currently have no application health-probe endpoint; set log/metric alerts and verify pollers before release.
- The single dispatcher is an initial deployment setting, not an HA claim. Delivery is at least once; duplicate starts/signals are expected across retries and revision overlap. Keep gateway operations idempotent. Increase worker capacity only after database and gateway limits are measured.
- Preserve the previous release bundle. Roll back by restoring compatible image digests/manifests; check Temporal workflow replay compatibility before changing worker code. Do not rerun schema bootstrap as a database rollback. Restore data only through a reviewed Cloud SQL recovery procedure.
- Default API maximum is three instances; worker pools and Cloud SQL incur ongoing costs even when no user is browsing. Establish a budget and alerts before rollout. Pausing a worker pool stops progress; accepted outbox work remains pending in PostgreSQL.

Start production with a clean Cloud SQL database unless existing demo history is specifically needed. A SQLite file cannot be copied into Cloud SQL, and copying the local Temporal development volume is not a Temporal Cloud migration. Any real-data cutover requires a separate export/import plan, stopped intake, reconciliation of in-flight workflows and a tested rollback.

Remaining enterprise launch requirements are tracked in [architecture](architecture.md) and [verification](verification.md). These deployment files make the container portable; they do not certify the complete software factory for production.
