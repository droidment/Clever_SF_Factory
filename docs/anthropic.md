# Use Anthropic with the software factory

The `anthropic` mode makes real Claude API calls for triage, planning and draft artifacts. The user has supplied a key through masked input to Google Secret Manager. Automated tests exercise the actual Anthropic SDK with mocked HTTP responses; live deployment results are recorded in [verification](verification.md).

## Enter the key locally

From a PowerShell terminal in the project directory:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-anthropic.ps1
```

The launcher prompts for your Anthropic API key with masked input. It holds the key in the process environment only, restores any previous settings when it exits, and does not write the key to a file or command history. Do not paste it into chat or a request description.

Open [the Claude draft workspace](http://127.0.0.1:8001). Submit a new request with a clear description and acceptance criteria. Starting the server does not call the model; submitting a request does. The existing demo on port 8000 is separate. The launcher uses `data/anthropic.db` so demo examples do not become paid requests. Stop the server with Ctrl+C; enter the key again next time. Use `-Port 8002` if port 8001 is occupied.

If dependencies have not been updated, run `python -m pip install -r requirements.lock` in the project virtual environment first. Rebuild the console after frontend changes with `npm --prefix web run build`.

The key needs Messages API access to the selected model and an account with API credit/billing. The first request reports credential, model-access or rate-limit errors in its execution history. There is no fallback to simulated success.

## What Claude can do here

| Stage | Current behavior |
| --- | --- |
| Triage | Model-based scope assessment, risk identification and clarification requests |
| Planner | Model-generated task graph, validated against application policy |
| Specialists | Proposed code, designs, documentation and analysis based on supplied text |
| Test/security/review | Draft test plans and reviews of supplied drafts; no tests or scanners run |
| Final result | `Draft ready`; all acceptance criteria remain unverified |
| Repository/deployment tools | Not connected; no file edits, commits, CI runs or releases |

Repository selections are currently labels that constrain requests, not live access to source code. Attachment URLs are not fetched. Include the relevant non-secret context in your description. The server strips generated artifact URLs and tool claims from structured outputs, labels artifacts as unexecuted drafts, and refuses deployment requests. The agent registry reports no connected tools or permissions in this mode.

## Provider configuration

The default is `claude-sonnet-5`. `FACTORY_ANTHROPIC_MODEL` also accepts `claude-sonnet-4-6` and `claude-haiku-4-5-20251001`. `FACTORY_ANTHROPIC_MAX_TOKENS` defaults to 4096 per response (allowed range 256–8192). Model availability depends on your account. [Anthropic model reference](https://platform.claude.com/docs/en/models/overview).

For another local process launcher, set `FACTORY_MODE=anthropic` and inject `ANTHROPIC_API_KEY` or `FACTORY_ANTHROPIC_API_KEY` in its environment. The latter takes precedence. Bind local demo authentication to loopback only. Do not publish it as an unauthenticated cloud service.

Calls use the official SDK, a fixed `https://api.anthropic.com` endpoint, and JSON-schema structured outputs. Schemas are transformed by the SDK, then validated locally against the original Pydantic contracts. Refusals, truncation and malformed results stop the request instead of fabricating a result. [Structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs), [Messages API](https://platform.claude.com/docs/en/api/messages/create).

The provider counts input tokens before generation and checks the configured output-token allowance against the per-task cost ceiling. Request context is capped at 256 KB and 64,000 counted input tokens. Reported tokens come from the API response, not model-generated JSON. Cost is an estimate calculated from recorded tokens and the standard first-party rate table in `factory/anthropic_provider.py`, checked on 2026-09-24; reconcile with your provider invoice and refresh the table when rates change. Token usage from received but invalid/truncated responses is also retained. [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing).

SDK and local activity retries are disabled for this mode to avoid automatically repeating an uncertain paid request. A process crash can still occur after billing but before persistence; restarting an interrupted request can repeat that call. Cancellation takes effect between agent calls and cannot undo provider charges. This local runtime is not a distributed billing ledger or a replacement for the durable Postgres runtime.

## Google Cloud

The draft workspace uses project `sffactorygpt`, Cloud Run, Cloud SQL and a Secret Manager reference for the Anthropic key. Public visitors can browse the workspace; creating paid AI requests and managing work require the configured Google account through an authenticated proxy. Follow [the draft deployment runbook](gcp-draft-deployment.md). The separate gateway manifests describe a future execution topology. A production gateway still needs repository/sandbox tools, independent verification and approval controls. Do not substitute model-generated prose for execution evidence.
