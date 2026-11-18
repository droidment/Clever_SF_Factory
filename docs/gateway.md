# Agent execution gateway

The factory calls one admin-configured HTTPS endpoint. This repository does not embed a model SDK or expose an unrestricted terminal to model-generated instructions. An enterprise gateway is the explicit extension point for real implementation agents, repository sandboxes, CI and deployment.

## Wire contract

`POST ${FACTORY_GATEWAY_URL}`

Headers: `Authorization: Bearer <service secret>`, `Idempotency-Key: <request UUID>:<execution key>`, `Content-Type: application/json`.

```json
{
  "schema_version": 1,
  "request_id": "<UUID>",
  "tenant_id": "<verified tenant>",
  "agent": "backend",
  "key": "r0:backend",
  "work": {"title":"...","repository":"platform/customer-portal","acceptance_criteria":["..."]},
  "dependencies": {"architecture":{"summary":"...","artifacts":[]}},
  "repair_context": [],
  "max_cost_usd": 2,
  "release_gate": "r0:policy-approval",
  "definition": {"name":"backend","tools":["repository.read","workspace.patch"],"permissions":["repo:read","branch:write"],"prompt_version":"1.0.0"},
  "output_schema": {"type":"object"}
}
```

The illustrative `work`, `definition` and `output_schema` are abbreviated above; the running activity sends the complete versioned contracts. Triage returns `Triage`; planner returns `Plan` containing implementation tasks only; other agents return `AgentOutput`:

```json
{
  "summary": "Implemented the callback idempotency change in an isolated branch.",
  "artifacts": [{"name":"change.patch","type":"patch","content":"<bounded summary or patch>","location":"https://artifacts.example/tenant/request/digest","sha256":null}],
  "usage": {"cost_usd":0.37,"tokens":1200,"model":"approved-model-version"},
  "simulated": false,
  "passed": true,
  "criteria": [],
  "findings": [],
  "decisions": ["Used a unique callback identifier to prevent duplicate writes."],
  "tool_calls": [{"tool":"workspace.patch","receipt_id":"<gateway receipt>","commit":"<commit SHA>"}]
}
```

Validation outputs enumerate each acceptance criterion exactly once with `passed` and evidence references like `policy-test/test-report.json`. Evidence must reference an upstream artifact name, not a self-authored final-validation artifact. The gateway must independently validate CI attestations before reporting success. Local shape validation is not sufficient to detect fabricated claims from a model.

## Side-effect and budget rules

1. Store a durable operation receipt keyed by tenant, request and execution key **before** initiating an external operation. Repeated keys return the same receipt/result.
2. Distinguish definitely-not-started, running, succeeded, failed and unknown effects. Reconcile unknown effects before repeating them.
3. Apply request-wide budget reservations and a cumulative per-key ceiling, including failed/aborted provider calls. The `max_cost_usd` field is an authorization limit, not a suggested estimate.
4. Report usage from provider/tool metering, never model text. Reconcile interrupted calls. A stale local activity cache cannot serve as the billing source.
5. Resolve allowed tools from a server-side registered policy. Treat the definition supplied by the activity as a pinned version hint, not independent authority.
6. Release only an immutable reviewed digest to an approved environment. Fetch and verify the decision through an authorized approval service. The current factory prechecks the database decision; digest binding belongs in the integrated release gateway.
7. Use short-lived credentials, isolated processes, ephemeral disks, outbound allowlists, malware/secret scanning and tool audit logs.

## Timeout/error behavior

Response size limit: 256 KB on the wire, 128 KB validated output. Store large outputs in controlled object storage. HTTP timeout is 165 seconds; Temporal activity deadline is 180 seconds with a 30-second heartbeat timeout. Long builds should become gateway operations with resumable IDs and a polling activity in a future contract version.

HTTP `400/401/403/404/422`, invalid schemas, unregistered agents and illegal repositories fail permanently. Network errors, rate limits and 5xx responses retry up to three attempts. Passing output must have at least one typed artifact. Never put access tokens, private keys or repository credentials in outputs, errors or artifacts.

## Pilot adapter checklist

Implement triage/planner structured model calls; repository base commit resolution; tool permission enforcement; branch/worktree isolation; actual build/test/security commands with receipts; patch integration/conflict reports; documentation artifacts; independent acceptance verification; approved release execution; spend reconciliation. Test each with crash-after-effect and retry-before-response injection before enabling autonomous mutations.
