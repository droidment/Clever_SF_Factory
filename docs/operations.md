# Operations runbook

## Local demo

Build the UI, start the loopback API, and seed examples through the console. The demo runtime persists activity results and reconstructs nonterminal requests after an API restart, but this is a convenience for inspection and does **not** carry the guarantees of the [Postgres runtime](postgres-runtime.md). Run only one demo API process. Do not bind the unauthenticated demo to an exposed network.

Data: `data/factory.db` and SQLite WAL files. Stop the API before making an offline copy. Do not delete the data directory as a troubleshooting shortcut; use a different database path for a fresh demo.

## Development topology

For credential-free infrastructure verification first run:

```powershell
docker compose -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from tests
docker compose -f compose.test.yaml down
```

The `software-factory-test` project is separate from the application project. PostgreSQL uses a memory-backed test volume, and no service publishes host ports. Tests create a unique PostgreSQL schema for each test and remove only that schema afterward. The fixture rejects any external database name other than `factory_test`. Tests exercise actual database transactions, leases, human decisions, a worker stopping mid-call and replay with simulated agents. Stopping this stack does not remove application volumes or local demo data.

### Configured application stack

Configure the following in `.env` before using Compose:

```dotenv
FACTORY_GATEWAY_URL=https://your-approved-gateway.example/execute
FACTORY_GATEWAY_TOKEN=<secret>
FACTORY_AUTH_MODE=oidc
FACTORY_OIDC_ISSUER=https://your-identity-provider.example/
FACTORY_OIDC_AUDIENCE=software-factory
FACTORY_OIDC_JWKS_URL=https://your-identity-provider.example/.well-known/jwks.json
POSTGRES_PASSWORD=<development-secret>
```

Run `docker compose up --build`. The schema bootstrap runs before API/worker. Application PostgreSQL has a named persistent volume. API: `localhost:8000`. This topology requires Docker and configured gateway/identity services. It is for development, not HA deployment.

The compose API uses OIDC. Demo auth intentionally rejects non-loopback client addresses, including ordinary Docker bridge addresses. Use the native loopback run instructions for demonstration mode.

## Common incidents

For the managed-cloud topology, use the [Google Cloud runbook](google-cloud.md). Set `FACTORY_DISPATCH_ENABLED=false` on the HTTP service and run one or more `python -m factory.worker` processes. In that configuration, inspect worker logs, outbox age and `execution_leases` independently of API readiness.

| Observation | Check / recovery |
|---|---|
| Request stays queued | Check `/readyz`, worker logs and undelivered outbox rows. Confirm a worker is running with the same `FACTORY_MODE` the request was accepted under. Do not manually fabricate completed status. |
| Task appears running after worker loss | Wait one lease period (`FACTORY_LEASE_SECONDS`); another worker takes over and re-runs the interrupted call. Check `execution_leases` for the owner and `lease_until`. Confirm the gateway reconciles the stable execution key. |
| Human decision recorded but still waiting | Confirm decision outbox delivery and that the lease row has a `wake_at`; API acceptance is asynchronous. Restart a worker if needed. Do not create a conflicting decision. |
| Repeated gateway failures | Check credentials, fixed endpoint, schema, model policy and upstream availability. No raw secrets in logs. Review per-attempt records. |
| Projection lags | Restore database connectivity. The request is released and retried after a delay. Replay does not duplicate stable events. |
| Validation exhausted | Read all revisions and findings. Open a new scoped corrective request; workflow-reset support is not exposed by this MVP. |
| Deployment result unknown | Reconcile the external release receipt before retrying. Do not assume timeout means deployment failed. |
| Cost discrepancy | Compare gateway billing ledger and provider invoices against successful activity usage. The application projection alone omits unresolved failed-call charges. |

## Release discipline

Run backend tests, frontend build, browser tests and the runtime tests against PostgreSQL. The worker image must match the registry/prompt/policy snapshot. A replay follows the supervisor code that is deployed, so let open requests finish before releasing a change to supervisor logic, or accept that they may fail.

Use database migrations for schema changes; `factory.migrate` only creates the initial schema and does not alter existing columns. Before production, introduce a versioned migration history and run it as a separate deployment job. Never run migration DDL concurrently in API replicas.

## Security and capacity launch gates

Provision SSO/BFF, private networking, TLS/mTLS, managed secrets, RLS, constrained tool sandboxes, independent CI attestations, artifact retention/ACLs, budget ledger, exact rate/concurrency quotas, audit export, distributed tracing, alerts, worker version routing and backup/restore procedures. Measure payload/history growth and request throughput. Establish business-approved retention, RPO, RTO and incident ownership. These gates are deployment requirements, not claims of tests already performed.
