# Verification record

Local environment: Windows, Python 3.13, Node 24.13.1. Results from this implementation session, 2026-09-24.

## Backend

`RUN_TEMPORAL_TESTS=1 python -m pytest -q`: **53 passed** on Windows, including 11 Anthropic integration and boundary tests.

Coverage includes:

- Transactional intake, tracking IDs, duplicate request handling and changed-payload conflicts.
- Tenant and repository visibility, read/write roles, JWT signatures, issuer/audience/expiry validation, and separate-person release approval.
- Teams/email/webhook envelope normalization and idempotent provider-event redelivery.
- Rejection of cyclic/duplicate/unknown graph nodes and planner attempts to grant deployment access.
- Policy-controlled parallel readiness and release prerequisites.
- Acceptance-criterion coverage, missing/invented evidence and rejection of simulated evidence for real completion.
- Local supervisor lifecycle, persistent activity cache, approval pause/resume, corrective work, correction limit, budget admission and cancellation.
- Actual Temporal workflow execution plus history replay.
- Actual Temporal worker shutdown during an approval wait, a signal arriving with no worker, a fresh worker resuming, and successful history replay afterward.
- Outbox signal redelivery after workflow completion is safely retired instead of retried forever.

Temporal test environment: CLI 1.9.1, Server 1.32.0, Python SDK 1.33.0. These tests use a real local Temporal server with simulated agent activities; they do not invoke paid models or mutate repositories.

`python -m ruff check factory tests`: passed.

The updated Anthropic dependency set includes httpx2, which also removes the earlier Starlette TestClient deprecation warning.

## Frontend

TypeScript strict check and Vite production build passed with `npm --prefix web run build`.

Playwright covers dashboard/registry navigation, new intake through simulated completion, artifact inspection, clarification and release decisions through resumed execution, modal keyboard dismissal, and a 390-pixel viewport overflow check. Screenshots are retained in `web/test-results/` after a successful browser run.

All six browser tests pass, including Anthropic mode disclosure and disabled deployment controls. The Anthropic browser check uses a configuration fixture; it does not call a live model. The test configuration starts an isolated demo API on port 8010 with a separate database; it does not need or modify the user's running console.

The browser assertions were refined to scope repeated artifact/permission text to the expanded panel. Initial Temporal testing found and fixed an activity projection argument mismatch before the final successful execution/replay runs.

## Not verified here

- Temporal Cloud and the full execution-gateway deployment. Cloud draft deployment checks are recorded separately below.
- Coding sandboxes, CI attestations, Git merges and software releases still require a production execution gateway.
- Live Teams/email subscriptions, enterprise identity-provider login flow, multi-region availability, load limits, disaster recovery or security certification.
- External costs, distributed budget ledger, exact burst-rate control and independent CI evidence trust.

These are explicit integration and launch gates in the architecture, not implied successes from the demo.

## Anthropic provider checks

The 11 added tests exercise the official SDK through a mocked HTTP transport: structured output, trusted usage accounting, token/cost admission before generation, invalid keys, refusals, truncated/invalid output, usage retention on failed responses, persisted result reuse, secret redaction, provider isolation, draft-only completion and deployment refusal. Those automated tests consume no model credits. The masked PowerShell launcher passes syntax validation. The user subsequently supplied the cloud key through the masked Secret Manager helper.

## Docker and Google Cloud preparation

Docker Desktop 28.3.2 and Compose 2.38.2 were verified after recovering stale Docker runtime sockets. Docker images and user volumes were preserved.

`docker compose -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from tests`: **56 passed**, exit 0, after adding Google-token validation, PostgreSQL dispatcher-leader and authenticated proxy tests. This runs Linux Python 3.13 against PostgreSQL 17.11 and Temporal CLI 1.9.1 / server 1.32.0. The database is dedicated to tests, each test gets an isolated schema, and no host ports are published. Demo/Temporal agents remain simulated; Anthropic tests use mocked provider HTTP responses.

`docker build --target production -t software-factory:local .`: passed, including the production frontend build.

`python scripts/smoke_container.py`: passed. The production image runs as UID 10001, honors `PORT=8080`, returns successful readiness and built-console responses, and shuts down on SIGTERM with exit 0. Its temporary container was removed.

The production image's schema bootstrap, standalone worker and standalone dispatcher also started successfully against the isolated PostgreSQL and Temporal containers. Both long-running processes stopped with SIGTERM and exited 0. This was an idle-process lifecycle check; workflow restart/replay behavior is covered separately by the Temporal integration tests.

Four Cloud Run manifests render locally from `deploy/gcp/config.example.json`. Automated tests cover role-specific secret access, separation of HTTP intake from outbox polling, immutable image references, pinned secret versions, HTTPS endpoints, repository restrictions, service-account separation, port handling and termination signals. These are offline checks, not cloud API validation. See [the deployment runbook](google-cloud.md) for infrastructure prerequisites and remaining launch gates.

## Live Google Cloud draft deployment — 2026-09-24

Public-view update: revision `factory-drafts-00003-ckh`, image index `sha256:c32e234978ba8bbda51312b801d870fbe3f4aa2f5b876ba228ae2925adc46460`. At the user's request, the direct GCP URL now allows anonymous viewing. Live checks returned 200 for the homepage and viewer configuration, 403 for anonymous workflow creation, and 401 for forged application credentials. Authorized operator roles remain available through the proxy. Browser verification showed the public notice, persisted draft request and no New request button. The existing six artifacts and recorded cost survived this deployment without additional model calls. The two private/public authorization test cases passed locally (PostgreSQL-only leader test skipped on SQLite); production TypeScript/Vite Docker build and Ruff passed. The original private rollout below is retained as historical evidence.

Successfully deployed `factory-drafts` to `sffactorygpt/us-central1`, revision `factory-drafts-00002-p7v`. The immutable image index digest is `sha256:91429c495e915d1446e287ad9f56f7ad3c4df1c7019c0e4593a363c9791dc079` (Linux amd64 manifest `sha256:3b7ee8603883e5993858ff6b72de01581fce7856c30bd184d9172d225e960147`). Schema bootstrap execution `factory-drafts-bootstrap-lc7zm` succeeded under a separate service account. Cloud Run startup/readiness succeeded against Cloud SQL, with `dispatch_error: null`.

Verified anonymously requested cloud APIs return 403; valid Cloud Run IAM credentials plus a forged application token return 401; valid signed application credentials identify the project owner's account in tenant `sffactorygpt`. The service IAM policy contains that user's invoker grant and no public principal. The local forwarding process listens only on `127.0.0.1:8085`. The browser loaded the cloud console and displayed the running request and its saved artifacts. These checks used real cloud APIs, not mocks.

Live Anthropic request `c01b1c8b-1b46-49ea-b1f2-52556c24becd` completed with status `drafted`: eight successful Claude Sonnet 5 executions, six artifacts, 44,161 recorded tokens and estimated model spend **$0.194282**. The request asked for operator documentation based only on supplied configuration. It performed no repository edits, scans, tests or releases. This result establishes working provider credentials, background dispatch and record persistence through the deployed application; it is not evidence of software delivery verification.

The local Anthropic SQLite database contained zero requests at migration time, so there was no local work history to transfer. Existing local services were retained. See [access, resource configuration, billing and limitations](gcp-draft-deployment.md). A cloud restart/recovery exercise, load test, database restore exercise and enterprise browser sign-in remain unverified.

## Temporal removal and Postgres runtime, 2026-11-18

Temporal and its SDK were removed; durable execution now runs on PostgreSQL alone ([runtime description](postgres-runtime.md)). The Temporal results above are kept as the record of the earlier implementation and no longer correspond to code in this repository.

Local environment: Windows, Python 3.13 in a fresh virtual environment built from `requirements.lock` (no `temporalio` installed), Node 24, PostgreSQL 18.6.

- `python -m pytest -q` on SQLite: **70 passed, 1 skipped** (the PostgreSQL advisory-lock test).
- The same suite with `FACTORY_TEST_DATABASE_URL` set to PostgreSQL: **71 passed**. The 12 runtime tests were repeated five times on PostgreSQL without a failure.
- `python -m ruff check factory tests`: clean. `npm --prefix web run build`: succeeded. `npm --prefix web run test:e2e`: **7 passed**.
- Live process test on PostgreSQL with a 5-second lease: the API ran with dispatch disabled and a separate `python -m factory.worker` process. The worker was killed while the developer agent call was in flight. A second worker finished the request 8.7 seconds later. The two calls completed before the kill were not repeated; the interrupted call shows attempt 1 failed ("Worker stopped before the attempt finished") and attempt 2 completed; all 28 events were unique.

Not verified: the `compose.yaml` and `compose.test.yaml` stacks (Docker was not running), the Linux CI job with its PostgreSQL service before this was pushed, gateway mode against a real gateway (none exists yet), and behaviour with workers on separate machines.
