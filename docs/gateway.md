# Execution gateway

The gateway is the only component that touches a repository, gives a model tools, runs commands or pushes anything. The factory (`factory/`) decides *what* happens and in which order; the gateway (`gateway/`) does it and reports evidence. They talk over one HTTPS endpoint and share no database.

This is a first version. It carries a request from intake to a pushed, tested branch and a pull request. What it does not do yet is listed under [Limits](#limits).

## What each agent does

| Agent | Who does the work | Result |
|---|---|---|
| `triage`, `planner` | Claude, one structured call, no tools | Classification; a proposed task list |
| `requirements`, `architecture`, `repository` | Claude with read-only file tools | A Markdown report |
| `developer`, `frontend`, `backend`, `database` | Claude with read and write file tools | A commit on the request's branch; the patch as the artifact |
| `documentation` | Claude with write access to documentation paths only | A commit; the patch as the artifact |
| `review` | Claude with read-only file tools and the full diff | A report; can send the work back |
| `test` | The gateway. No model | Exit code and output of the repository's configured test command |
| `security` | The gateway. No model | Secret scan of the change, plus an optional configured scan command |
| `validation` | Claude judges each criterion against the evidence; the gateway checks the answer | Per-criterion verdict |
| `deployment` | The gateway. No model | Branch pushed to the origin; pull request opened when GitHub is configured |

`performance` and `sap` are not implemented and are refused.

Three rules follow from this split:

- **A model never reports whether tests passed.** The test step runs a command an administrator configured and records what happened. Validation passes only if this gateway holds its own record of a passing test run for the current plan revision, and nothing but documentation has changed since the tested commit.
- **A model never runs a command.** It can list, read, search, write, edit and delete files inside one checkout. It cannot reach `.git`, anything outside the checkout, or protected paths (`.github/*` by default, so an agent cannot rewrite CI).
- **Nothing leaves the gateway before a person approves.** Work happens in a local clone. The push and the pull request happen in the `deployment` step, which the factory only schedules after the release decision. Merging is left to people.

## Workspaces

Each request gets one bare clone and one integration branch, `factory/<request id>`. Every agent call works in its own git worktree made from the tip of that branch. A writing agent's edits are committed there and merged into the integration branch under a per-request lock, so agents the planner runs in parallel do not overwrite each other. A merge conflict is reported as a failed task with the conflict as its finding, which sends the request into the factory's corrective round.

## Wire contract

`POST /execute` with `Authorization: Bearer <GATEWAY_TOKEN>`, `Idempotency-Key: <request id>:<execution key>` and the body the factory already sends (`schema_version`, `request_id`, `tenant_id`, `agent`, `key`, `work`, `dependencies`, `repair_context`, `max_cost_usd`, `release_gate`). The `definition` in the body is ignored: tools and permissions come from the gateway's own copy of the registry.

| Status | Meaning | What the factory does |
|---|---|---|
| `200` | Finished. Body is `Triage`, `Plan` or `AgentOutput` | Stores the result |
| `202` | Still working (`Retry-After` header) | Sends the identical request again |
| `401`, `403`, `422` | Refused; the body's `error` says why | Fails the call permanently |
| `502` | This attempt failed | Retries, up to three attempts |

An agent call can take many minutes, longer than one HTTP request should stay open. The gateway holds a request for up to `GATEWAY_WAIT_SECONDS` (default 100) and then answers `202`. Sending the same request again rejoins the running work; it never starts a second copy. The factory keeps asking until `FACTORY_AGENT_TIMEOUT_SECONDS`, and a retry after that rejoins as well.

## Repeats and interruptions

A receipt (`gateway_operations`) is written before any work starts and is keyed by tenant, request and execution key.

| Situation | Result |
|---|---|
| Same key again after success | The stored result. No model call, no command, no commit |
| Same key while running | Rejoins the running work |
| Same key with a different body | Refused |
| Earlier attempt failed | A new attempt, in a fresh worktree |
| Gateway stopped mid-call | At start-up the receipt is marked failed; the next request starts a new attempt |
| Gateway stopped after merging a commit but before saving the result | The commit is found by its `Factory-Key` trailer and returned. The model is not called again |
| Push or pull request repeated | The push is the same commit to the same branch; an open pull request for the branch is reused |

## Spend

Cost is computed from the `usage` block of every Claude response, at the rates in `gateway/config.py`. Before each model turn the gateway stops the call if the turn would probably cross `max_cost_usd`; spend from failed and stopped attempts stays on the receipt. A new call is refused once the request's receipts add up to its `budget_usd`. A model that is not in the rate table cannot be selected.

## Running it

```powershell
$env:GATEWAY_TOKEN = '<random secret, 32+ characters>'      # the same value as FACTORY_GATEWAY_TOKEN
$env:ANTHROPIC_API_KEY = '<key>'
$env:GATEWAY_SANDBOX = 'docker'
.\.venv\Scripts\python.exe -m gateway                         # 127.0.0.1:8100
```

Repositories are listed in `gateway.repositories.json` (copy `gateway.repositories.example.json`). The key is the repository name used in factory requests and must also appear in `FACTORY_ALLOWED_REPOSITORIES`.

| Field | Meaning |
|---|---|
| `url` | Clone URL or local path |
| `base` | Branch that work starts from and pull requests target. Default `main` |
| `test_command` | Run in the sandbox at the integrated commit. Empty means the test step fails |
| `scan_command` | Optional extra security scan; a non-zero exit is a finding |
| `image` | Container image for the Docker sandbox |
| `github` | `owner/name`. With `GATEWAY_GITHUB_TOKEN`, a release opens a pull request |
| `protected_paths`, `documentation_paths` | Path patterns agents may never write; patterns the documentation agent may write |

Point the factory at it with `FACTORY_MODE=gateway`, `FACTORY_GATEWAY_URL=http://127.0.0.1:8100/execute` and the token. Plain HTTP is accepted only for a loopback address; anything else must be HTTPS.

Other settings: `GATEWAY_MODEL` (`claude-opus-5-5` default, or `claude-sonnet-5-5`), `GATEWAY_EFFORT`, `GATEWAY_MAX_TURNS`, `GATEWAY_DATABASE_URL`, `GATEWAY_WORKSPACE_ROOT`, `GATEWAY_COMMAND_TIMEOUT_SECONDS`, `GATEWAY_OPERATION_TIMEOUT_SECONDS`.

### Sandbox

Test and scan commands execute code that an agent may have just written.

- `docker` (default): `docker run --rm --network none` with memory, CPU and process limits and the worktree mounted. There is no network, so the image must already contain the project's dependencies.
- `process`: the command runs on the gateway's host with a reduced environment. This is not isolation. Use it only on a machine and repository where running that code directly is acceptable.

### Credentials

The GitHub token needs contents and pull-request write access on the configured repositories only. It is passed to git through the process environment for the clone and the push, and is never written to disk. Git's credential helpers are disabled, so the gateway cannot act with the personal credentials of whoever starts it.

## Limits

- **One gateway process.** Running calls and per-request locks live in memory. Two instances on the same workspace directory would conflict.
- **Cancelling a request does not stop a call already running in the gateway.** The factory stops asking; the call finishes and its spend is recorded.
- **Approval is checked by the factory, not the gateway.** The gateway trusts that a `deployment` call means the release was approved. It does verify its own test record first.
- **CI results are not read back.** The pull request is opened and the request completes; whether GitHub's checks pass is not yet fed into the factory.
- **A model call that times out after the provider billed it is not metered.** Only responses that arrive are counted.
- **No refusal fallback.** If the model declines a request the call fails; it is not rerouted to another model, because spend is metered against one model's rates.
- **Workspaces are not cleaned up** when a request ends.
- **No container image or cloud manifest for the gateway yet.**

## Verification

`tests/test_gateway.py` runs the gateway against a real git repository and a real test command, with Claude replaced by a scripted HTTP transport. It covers credentials, commit and repeat, `202` polling, failing tests, recovery of a committed change, the cost stop, validation without a test record, path confinement, the secret scan, and one request driven by the factory's durable runtime from intake through approval to a pushed branch. See [verification](verification.md) for what has and has not been run.
