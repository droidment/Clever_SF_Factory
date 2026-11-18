# Anthropic draft workspace on Google Cloud

Deployment target: `sffactorygpt`, `us-central1`. This is the current draft workspace, separate from the future Temporal/execution-gateway topology in [google-cloud.md](google-cloud.md).

Deployed and live-tested on 2026-09-24. [Manage the Cloud Run service](https://console.cloud.google.com/run/detail/us-central1/factory-drafts/metrics?project=sffactorygpt). The [public workspace](https://factory-drafts-910346008115.us-central1.run.app) is viewable without signing in. Requests, drafts and execution history in this workspace are publicly readable. Creating requests, approving or cancelling work, and access to operational metrics require the authorized Google account. The completed smoke request produced six artifacts with estimated Anthropic spend of $0.194282; see [verification](verification.md).

## Authorized operator access

For the presentation, the public site now supports **Sign in / Switch account** with four password-protected demo accounts. See [the presenter guide](demo-presenter.md). The Google CLI proxy remains an alternative operator identity; it is not required for demo-account login.

Run from this project in PowerShell, while signed into Google Cloud CLI with the project owner's account (set `FACTORY_GCLOUD_ACCOUNT` if that is not the CLI's active account):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\open-gcp.ps1
```

Keep that terminal running and open [the authenticated workspace](http://127.0.0.1:8085/). Port 8085 is an authenticated forwarding proxy: the application, background agents and records run in Google Cloud. Ports 8000 and 8001 remain local applications. The public Cloud Run URL provides read-only access; the local proxy supplies your signed Google identity for write access. Browser-based Google sign-in without the local proxy remains future work.

The project launcher uses `scripts/gcp_proxy.py`, which obtains your Google ID token from the CLI in memory and refreshes it periodically. It sends one token header for Cloud Run IAM and a separate signed-token header for application verification. Live testing showed Cloud Run consumes the standard authorization header, so the stock `gcloud run services proxy` alone does not satisfy the application's second identity check. The local proxy binds only to `127.0.0.1`, rejects foreign Host/Origin values, and never sends its credentials to the browser. [Cloud Run authentication headers](https://docs.cloud.google.com/run/docs/authenticating/service-to-service).

## Resources and access

| Resource | Name / configuration |
| --- | --- |
| Cloud Run service | `factory-drafts`; 1 CPU, 1 GiB, CPU always allocated; minimum and maximum 1 instance |
| Cloud SQL | `factory-drafts-db`; PostgreSQL 17; `factory` database; zonal `db-f1-micro` |
| Database protection | Daily backups, seven retained backups, seven-day point-in-time recovery, deletion protection |
| Container registry | `us-central1-docker.pkg.dev/sffactorygpt/factory/software-factory`; deployment pinned to SHA-256 digest |
| Provider credential | Secret Manager `factory-anthropic-key`, version 1 |
| Application database credential | Secret Manager `factory-drafts-database-url`, version 1 |
| Schema owner credential | Secret Manager `factory-drafts-owner-url`, version 1 |
| Initial schema job | `factory-drafts-bootstrap`; no automatic retries |

Cloud Run's invoker IAM check is disabled for public viewing, as requested by the user. `FACTORY_PUBLIC_VIEW=true` grants anonymous visitors only the viewer role in this workspace. Write roles require either the configured Google identity (the project owner's account) or a signed session for an enabled demo account. Google tokens are checked for signature, issuer/audience/expiry and verified email; demo roles come from the fixed server registry. Invalid supplied tokens are rejected rather than treated as anonymous. Tenant and repository permissions come from server configuration. Cross-origin writes are restricted to the application origin and the two configured loopback proxy origins on port 8085.

The runtime service account has Cloud SQL client access and access to only its database URL and Anthropic key. The separate bootstrap service account has access to both database URLs. The `factory_app` PostgreSQL login has table read/write and sequence permissions; schema initialization runs under `factory_owner`. Cloud SQL has no authorized public networks; Cloud Run connects through the built-in authenticated Cloud SQL proxy.

The embedded dispatcher runs while the browser is closed. PostgreSQL advisory locking selects one dispatcher during overlapping Cloud Run revisions. This is still a single-instance draft runtime: cancellation cannot undo billed model calls, and a crash after provider billing but before saving the response can cause a repeated call after restart.

## Release and operation

Non-secret deployment settings are in ignored `deploy/gcp/draft.local.json`. Render with:

```powershell
.\.venv\Scripts\python.exe scripts/render_gcp_drafts.py
gcloud run services replace deploy/gcp/rendered/draft-api.yaml --project=sffactorygpt --region=us-central1
```

Build and push a new production image before changing its digest in the configuration. The bootstrap job only initializes the first schema and grants permissions; future schema changes need reviewed migrations. Do not use the separate placeholder Temporal manifests to update this draft service.

To rotate the provider key, run `.venv\Scripts\python.exe scripts/set_gcp_key.py`, set `api_key_version` to the returned numeric version in `draft.local.json`, render and redeploy. The masked helper sends the key directly to Secret Manager; never put it in source, chat, command arguments or a request description.

Cloud Run's minimum instance and the running Cloud SQL instance incur ongoing charges even when no requests are submitted. Anthropic usage is billed separately. The small zonal database is not an HA enterprise deployment. Backups are enabled, but a restore exercise, load testing and operational alerting remain outstanding.

Claude produces proposed artifacts only. Repository modifications, test execution, security scanners, commits and software releases are not connected. Successful work finishes as **Draft ready**, with acceptance criteria explicitly unverified.
