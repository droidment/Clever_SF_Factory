# Foundry · Automated Software Factory

A runnable control plane for durable, policy-controlled software delivery. Requests become typed execution graphs, specialized agents exchange artifacts, and a supervisor coordinates work through human decisions and acceptance validation.

**Three explicit modes** (who does the work):

- **Demo:** runs locally with SQLite and simulated agents. Shows the full lifecycle without credentials or repository changes. Finished work is marked `simulated`, never `completed`.
- **Anthropic drafts:** calls Claude for real triage, planning and proposed artifacts using your API key. No execution tools are connected; results are marked `drafted`, never verified completion. [Key setup and limitations](docs/anthropic.md).
- **Gateway:** sends agent work to a real HTTPS agent/tool gateway and requires the durable Postgres runtime. The gateway, model access, CI, repository sandboxes and identity provider must be supplied by your environment.

**Two runtimes** (where a running request lives), chosen with `FACTORY_RUNTIME`:

- **inprocess** (default): requests run inside the API process. Fine for a local demo.
- **postgres**: requests are owned by workers through leases in PostgreSQL, survive restarts and can be shared by several workers. No workflow service is needed. See [the Postgres runtime](docs/postgres-runtime.md).

This is an executable enterprise architecture foundation, **not a production-certified autonomous delivery service**. The [architecture and rollout plan](docs/architecture.md) explains implemented capabilities and remaining launch requirements.

## Run on Windows

Python 3.11–3.14 and Node 22+ are required. Verified here with Python 3.13 and Node 24.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
npm --prefix web ci
npm --prefix web run build
.\.venv\Scripts\python.exe -m uvicorn factory.api:app --host 127.0.0.1 --port 8000
```

Open [the console](http://127.0.0.1:8000). Select **Load sample requests** to inspect a portal feature, a release awaiting approval, and an investigation awaiting clarification. Or select **New request**. The application does not contact real repositories in demo mode. SQLite data lives in `data/factory.db`.

For live frontend development, run `npm --prefix web run dev` alongside the API and open [the development console](http://127.0.0.1:5173). The Vite proxy forwards API and event-stream requests.

On Linux/macOS use `python3 -m venv .venv` and `.venv/bin/python` in the corresponding commands.

## What is implemented

- Web/REST intake, authenticated Teams/email/webhook connector envelopes, tracking IDs, idempotent requests and transactional dispatch.
- Anthropic Messages API integration with structured output validation, recorded token usage, estimated costs, explicit draft status and a masked local key launcher.
- Structured triage, graph validation, policy-controlled task assembly, a 17-entry capability registry (16 enabled), dependency-based parallel work and bounded repairs.
- A durable runtime on PostgreSQL alone: leases, replay from saved results, retries with backoff, parked human waits, cancellation and takeover when a worker stops.
- Persistent requests, workflow executions, tasks, agent attempts, artifacts, decisions and audit events.
- A React operations console with live status, graph inspection, execution ledger, artifacts, approvals, searchable requests and registry.
- OIDC JWT verification, RBAC, tenant/repository authorization, release separation of duties and bounded gateway calls.
- Tests, an initial schema bootstrap, a Docker development topology, generated contracts and architecture/runbook documentation.

## Validate

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check factory tests
npm --prefix web run build
```

The runtime tests use SQLite by default. Point them at a dedicated PostgreSQL database named `factory_test` to exercise real row locking:

```powershell
$env:FACTORY_TEST_DATABASE_URL = 'postgresql+psycopg://factory_test:<password>@127.0.0.1:5432/factory_test'
.\.venv\Scripts\python.exe -m pytest -q
```

Browser tests start their own isolated demo API on port 8010 with a separate database. Build the UI first:

```powershell
node web/node_modules/@playwright/test/cli.js install chromium
npm --prefix web run test:e2e
```

## Architecture and interfaces

- [The Postgres runtime: how requests survive restarts](docs/postgres-runtime.md)
- [Architecture, security and enterprise roadmap](docs/architecture.md) (its orchestration sections predate the Postgres runtime)
- [API contracts and intake examples](docs/api.md)
- [Agent gateway contract and trust boundary](docs/gateway.md)
- [Deployment, failure recovery and runbook](docs/operations.md)
- [Google Cloud deployment preparation and migration runbook](docs/google-cloud.md)
- [Validation results](docs/verification.md)
- [Demo accounts and presenter walkthrough](docs/demo-presenter.md)
- Live [OpenAPI UI](http://127.0.0.1:8000/docs) and [OpenAPI JSON](http://127.0.0.1:8000/openapi.json)
- JSON schemas in `schemas/`; regenerate with `python scripts/export_schemas.py`.

## Production configuration

Copy `.env.example` to `.env`, then configure `FACTORY_MODE=gateway`, `FACTORY_RUNTIME=postgres`, PostgreSQL, an HTTPS execution gateway, repository allowlist, and OIDC issuer/audience/JWKS. Initialize the application schema with `python -m factory.migrate`; start API and `python -m factory.worker` independently. Read the runbook before enabling real tool permissions.

The Anthropic draft workspace has a separate [Google Cloud deployment runbook](docs/gcp-draft-deployment.md). The gateway topology is not deployed. No messages were sent through Teams/email, and no source repository credentials are included.

## Container verification

With Docker Desktop's Linux engine running, build the Linux application image and run the backend suite against an isolated PostgreSQL container:

```powershell
docker compose -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from tests
docker compose -f compose.test.yaml down
```

This needs no `.env`, model credentials or identity-provider setup. It publishes no host ports and uses a dedicated ephemeral test database. Agents remain simulated while the database and the runtime are real. The test image builds the production frontend and Python runtime, then adds tests in a separate image stage. The default Dockerfile target remains the application server.

Check the production container's non-root startup, assigned port, built console and graceful shutdown:

```powershell
docker build --target production -t software-factory:local .
.\.venv\Scripts\python.exe scripts/smoke_container.py
```

## Google Cloud preparation

The same image supports a Cloud Run API service, a separate worker pool, and a schema-initialization job. Deployment configuration uses Cloud SQL and Secret Manager. Render reviewable manifests locally with:

```powershell
.\.venv\Scripts\python.exe -m factory.cloud deploy/gcp/config.example.json
```

The example contains placeholders for the future gateway topology. Read [the Google Cloud runbook](docs/google-cloud.md) before supplying real configuration. The current draft deployment uses separate `draft-*.yaml` manifests, Cloud SQL and Secret Manager in `sffactorygpt`; see [access and operations](docs/gcp-draft-deployment.md). Live repository execution and an enterprise browser sign-in flow remain separate work.
