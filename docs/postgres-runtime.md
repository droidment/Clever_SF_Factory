# The Postgres runtime

The factory keeps a running request alive using only its own PostgreSQL database. There is no workflow service to install, license or operate. Select it with `FACTORY_RUNTIME=postgres`; it works with every mode (`demo`, `anthropic`, `gateway`) and is required for `gateway`.

The other runtime, `inprocess`, keeps running requests in the memory of the API process. It is the default for local demos on SQLite and should not be used for work that has to survive a restart.

## How a request stays alive

The supervisor (`factory/engine.py`) is ordinary code that walks a request through triage, planning, the task graph and its human gates. The runtime (`factory/durable.py`) makes that code restartable by **replaying it from the start**:

| The supervisor asks to… | On a replay the runtime… |
|---|---|
| record an event | skips it if an event with that key is already stored |
| call an agent | returns the stored result if that call already completed |
| wait for a human decision | returns the stored decision, or parks the request if there is none |

Whatever is left after the skipped steps is the work that still has to happen. The supervisor's decisions depend only on those stored results, so a replay follows the same path the original run took.

## Leases

`execution_leases` holds one row per unfinished request.

| Column | Meaning |
|---|---|
| `wake_at` | When the request may next be claimed. `NULL` means parked until a command arrives. |
| `owner`, `lease_until` | The worker running it, and when its claim lapses if not renewed. |
| `signals` | Count of commands delivered. A run compares it before parking so a decision that arrived mid-run is not missed. |
| `claims` | Consecutive claims that ended without progress. |
| `cancel_requested` | Set by a cancel command; a running worker sees it on its next renewal. |

Each worker repeats three steps about twice a second:

1. **Pump.** Undelivered outbox commands (`start`, `decision`, `cancel`) are marked delivered and their request is made claimable. This happens in one transaction, in the same database, so a command cannot be lost between "accepted" and "scheduled".
2. **Claim.** Take the oldest claimable request with `FOR UPDATE SKIP LOCKED`, so two workers never take the same one, and start a replay.
3. **Renew.** Extend the leases this worker holds. A worker that finds one of its leases gone cancels that run.

## What happens when things go wrong

| Event | Result |
|---|---|
| Worker process dies | Its leases lapse after `FACTORY_LEASE_SECONDS` (default 60). Another worker claims the request and replays it. The agent call that was in flight is recorded as failed with "Worker stopped before the attempt finished" and is made again. |
| Worker is shut down cleanly | It releases its leases at once; another worker resumes without waiting. |
| Agent call fails | Retried with backoff, up to three attempts per call, each recorded in the execution ledger. `anthropic` mode makes one attempt, so a billed call is never repeated silently. |
| Agent call hangs | Cut off after `FACTORY_AGENT_TIMEOUT_SECONDS` (default 900) and treated as a failed attempt. |
| Database unreachable | The request is neither failed nor lost. It is released and retried after a growing delay, capped at five minutes. |
| A request keeps stopping its workers | After ten consecutive claims without progress it is failed with an explanation, so it cannot stall the pool. |
| Waiting for a person | The request holds no lease and no memory. It can wait indefinitely; the decision command wakes it. |

## Running it

One process for everything (a single VM):

```powershell
$env:FACTORY_RUNTIME = 'postgres'
$env:FACTORY_DATABASE_URL = 'postgresql+psycopg://factory:<password>@localhost:5432/factory'
.\.venv\Scripts\python.exe -m uvicorn factory.api:app --host 127.0.0.1 --port 8000
```

Separate workers: set `FACTORY_DISPATCH_ENABLED=false` on the API and run as many of these as needed.

```powershell
.\.venv\Scripts\python.exe -m factory.worker
```

`FACTORY_WORKER_CONCURRENCY` (default 8) is the number of requests one worker runs at a time. `docker compose up --build` starts PostgreSQL, the API and one worker in this arrangement.

## Limits to know about

- **External effects can repeat.** If a worker dies after an agent finished but before the result was stored, that call is made again. The gateway receives a stable `Idempotency-Key` per call and must honour it for anything that cannot be safely repeated, such as opening a pull request or deploying.
- **Lease timing uses the workers' clocks.** Keep them synchronised (NTP). A worker paused for longer than the lease can briefly overlap with its replacement until its next renewal; stored events and completed calls are not duplicated, but an in-flight agent call can be.
- **The supervisor must stay deterministic.** Its choices may depend only on the request, agent results and human decisions. Changing its logic while requests are in flight can make a replay diverge from what was already recorded; let open requests finish, or accept that they may fail, before deploying such a change.
- **Replay cost grows with the request.** Every resume re-walks the supervisor. Plans are capped at 32 tasks and three revisions, which keeps this to tens of cheap lookups.
- **No schema migrations yet.** `python -m factory.migrate` creates missing tables, including `execution_leases`, but does not alter existing ones.

## Verification

`tests/test_durable.py` covers completion, parking and resuming on a different worker, a worker dying mid-call, a lost lease, cancellation, two workers sharing requests, agent retries, a database outage, a request that keeps failing its workers, and adoption of requests started by the in-process runtime. The tests run on SQLite by default and on PostgreSQL when `FACTORY_TEST_DATABASE_URL` is set; only PostgreSQL exercises real row locking, so the two-worker test runs only there. CI runs both.
