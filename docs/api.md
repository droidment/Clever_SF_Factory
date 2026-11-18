# API contracts

All operational endpoints require an authorized principal. Demo identity is accepted only over loopback; OIDC mode requires an RS256 access token with the configured issuer/audience, `exp`, `iat`, `sub`, `tid`, `roles`, and `repositories` claims. Tenant and repository privileges come from verified claims, never request fields.

## Endpoint map

| Method | Path | Role | Meaning |
|---|---|---|---|
| POST | `/api/requests` | requester | Normalize, persist and queue work; requires `Idempotency-Key` |
| POST | `/api/intake/{teams,email,webhook}` | requester | Trusted connector envelope; provider event ID supplies deduplication |
| GET | `/api/requests?limit=100&offset=0` | viewer | Most recent visible requests, maximum page size 200 |
| GET | `/api/requests/{id}` | viewer | Request, workflow, tasks, execution attempts, artifacts, events, decisions |
| POST | `/api/requests/{id}/decisions` | approver | Durable human gate decision; returns 202 |
| POST | `/api/requests/{id}/cancel` | operator | Cooperative cancellation command; returns 202 |
| GET | `/api/events?after=42` | viewer | SSE events after a cursor, reauthenticated each connection |
| GET | `/api/agents` | viewer | Capability registry |
| GET | `/api/stats` | viewer | Tenant/repository-scoped agent attempt counts and active utilization |
| GET | `/api/config` | authenticated | Current principal, environment and allowed repositories |
| POST | `/api/demo/seed` | requester | Demo-only idempotent sample requests |
| GET | `/healthz` | public | Process liveness |
| GET | `/readyz` | public | Database check, dispatcher error indicator |
| GET | `/metrics` | operator | Prometheus HTTP metrics |

`admin` implies application roles but does not bypass repository claims. Release self-approval is denied outside demo mode. Live schema constraints and response contracts are described in OpenAPI.

## Request example

```http
POST /api/requests
Authorization: Bearer <access-token>
Idempotency-Key: incident-4281-change-1
Content-Type: application/json

{
  "title": "Prevent duplicate order callbacks",
  "description": "A repeated successful payment callback creates a second order. Make processing idempotent and add regression tests.",
  "requestor": "Order platform team",
  "source": "api",
  "repository": "platform/order-service",
  "priority": "high",
  "business_context": "Repeated callbacks must be safe for customers.",
  "due_date": null,
  "attachments": [],
  "acceptance_criteria": ["A repeated callback creates one order", "Regression tests pass"],
  "security_requirements": ["Do not log payment secrets"],
  "deploy": true,
  "budget_usd": 50
}
```

```json
{"request_id":"<UUID>","created":true,"tracking_url":"/api/requests/<UUID>"}
```

`202` acknowledges the committed request, not a running agent. Temporal may be unavailable; the outbox retains the accepted command. Retry the same payload/key after an ambiguous HTTP result. Reusing the key for a different payload returns `409`.

## Connector envelope

```json
{
  "event_id": "provider-immutable-message-id",
  "subject": "Add a customer activity feed",
  "body": "Add a frontend feed and backend date filtering to the customer portal.",
  "sender": "Display name from the provider",
  "repository": "platform/customer-portal",
  "acceptance_criteria": ["Customers can filter events by date"],
  "priority": "normal"
}
```

Send to `/api/intake/teams` or `/api/intake/email` using a trusted integration identity. This is not a native unauthenticated Bot Framework/Graph webhook endpoint. The external adapter authenticates provider notifications before forwarding this envelope. Attachment ingestion and native subscription setup are separate integration work.

## Human decision

```json
{"gate":"r0:policy-approval","approved":true,"comment":"Reviewed tests, security report and release artifact."}
```

Clarification gates additionally accept `acceptance_criteria`. Release gates reject changing criteria. The gate must be currently open. Duplicate identical decisions are acknowledged; contradictory decisions return `409`. The decision row and signal command commit together. The response precedes workflow resumption.

## Status contract

`queued → triaging → [waiting_for_human] → planning → running → [waiting_for_human] → validating → completed`

Alternative terminal states: `failed`, `rejected`, `cancelled`, `simulated`. Corrections return from verification to `running` with a new plan revision. Frontend labels do not change stored machine states.

SSE frames include monotonically increasing `id` and a JSON event body. Cursor gaps are expected because other tenants/repositories have inaccessible events. Never interpret an ID gap as proof of lost data. Persist a cursor if clients require reconnect continuity across browser reloads.

## Errors

`401` missing/invalid identity; `403` unauthorized role/repository/origin; `404` resource not visible; `409` idempotency conflict, stale gate or terminal cancellation; `422` invalid schema; `429` intake rate limit. The gateway uses separate service authorization. Raw provider error bodies and credentials are not intended for client responses.
