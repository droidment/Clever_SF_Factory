# Presenter guide

Open the [cloud workspace](https://factory-drafts-910346008115.us-central1.run.app/) and use **Sign in / Switch account** in the top bar. All four accounts use the shared password delivered privately in the task response. The password is not published in the application or this repository.

| User ID | Role | Permissions |
| --- | --- | --- |
| `admin` | Admin | View, create, approve another account's request, cancel active work |
| `creator` | Ticket Creator | View and create requests |
| `approver` | Approver | View and submit human decisions |
| `viewer` | Viewer | Read-only access |

The accounts share one workspace. They are demo identities, not separately verified people. A request submitted by `admin` cannot be approved by `admin`; switch to `approver`. Public visitors can still read this workspace without a password.

## Suggested demo

1. Open [How it works](https://factory-drafts-910346008115.us-central1.run.app/#how-it-works). Explain the current Anthropic draft runtime and the future Temporal path.
2. Sign in as `creator`, select New request and ask for a short operator document or proposed code change. Supply a clear acceptance criterion and a budget. Requests make real, billable Claude calls.
3. Open the request and follow the execution graph, agent ledger, artifacts and token/cost totals.
4. After agents finish, the request waits for human review. Switch to `approver`, open Human approvals, inspect the artifacts, enter decision notes and choose Approve drafts or Decline.
5. Switch to `viewer` to show the outcome and audit trail. Admin can cancel a separate active request.

Approval accepts a draft; it does not mean code ran, tests passed or a deployment occurred. The previous completed smoke request predates the review gate; use a new request to demonstrate the full handoff. The gate requirement is saved with intake, so restarts preserve the original request policy.

## Session and credential implementation

The shared password is stored as a salted PBKDF2-SHA256 hash (600,000 iterations), with a separate random session-signing key, in `factory-demo-credentials` version 1 in Secret Manager. Only the application runtime account can read this secret through its assigned secret-access grant. The key and password hash are never returned by the API.

Successful login sets an eight-hour, HttpOnly, SameSite=Strict session cookie; HTTPS responses mark it Secure. Roles come from the server's fixed account registry. Login attempts are limited per user ID in the running instance. Sign out removes the browser cookie. Another login replaces it, so tabs on the same origin share the selected identity.

This intentionally shared credential is a demonstration facility: anyone with the password can sign in as Admin and spend model credits. Do not use these accounts as independent production identities. Disabling `demo_login_enabled` in `deploy/gcp/draft.local.json`, rendering and redeploying disables all demo sessions. Rotating the signing key also invalidates existing sessions. A production identity system would need individual credentials, revocation, recovery and centralized rate limiting.

The cloud configuration enables `draft_review_required`; new Anthropic requests append a policy-owned human review task after the AI output stages. Decisions retain the acting user ID and notes. The underlying runtime remains the application dispatcher, not Temporal Cloud.
