# Google sign-in, guest recovery, and browser sessions

This implements two backend slices: Google authorization-code sign-in with PKCE,
and account/session lifecycle controls. Google authentication is **disabled by
default**. No Google application is registered, credentials provisioned,
production migration run, or deployment changed by this implementation.
Games and actual market publication retain their separate launch gates.

## Setup before enabling

Run the reviewed Alembic **head**, currently `0005_google_identity`, as a separate
deployment step. It preserves existing guest cookies, principals, games,
simulator audit, qualified releases, and ingestion/backfill records. Existing
guest sessions do not receive a fabricated Google authentication timestamp.
New identity/claim audit is append-only; destructive downgrade is disabled.

Register a Google OAuth **Web application** and its exact HTTPS redirect URI:
`https://<api-host>/v1/oidc/callback`. Use the applicable Google consent,
test-user, publishing, and verification process. Configuration uses:

| Variable | Value |
| --- | --- |
| `FINANCE_GOOGLE_AUTH_ENABLED` | `true` only after setup and logging review |
| `FINANCE_GOOGLE_CLIENT_ID` | The Web application's `*.apps.googleusercontent.com` client ID |
| `FINANCE_GOOGLE_CLIENT_SECRET` | Server-only secret from the deployment's secret manager |
| `FINANCE_GOOGLE_REDIRECT_URI` | The exact registered API callback, without query or fragment |
| `FINANCE_GOOGLE_COMPLETION_URI` | A fixed HTTPS frontend completion page, without query or fragment |
| `FINANCE_ALLOWED_ORIGINS` | Exact frontend origins, including the completion page's origin |
| `FINANCE_GUEST_RATE_LIMIT_KEY` | At least 32 secret characters; also keys the durable login limiter |

Do not put the client secret in frontend configuration, examples, command
arguments, source control, telemetry, or logs. The API never derives callbacks
from Host/forwarded headers and never accepts client-selected return URLs.
Use same-site HTTPS frontend/API hosts. Cookies remain Secure, HttpOnly,
SameSite=Lax, Path=/, without Domain; cross-site third-party-cookie use is not
supported. HTTP loopback alone is insufficient for these cookies.

Configure trusted ingress addresses for `FORWARDED_ALLOW_IPS`, not `*`.
Anonymous login starts are limited to 20 per client address per ten-minute
fixed window; authenticated starts use the principal instead. Counters are
atomic across replicas and store keyed hashes, not raw addresses.

**Logging is a launch prerequisite.** The API redacts callback query strings
before routing and filters Uvicorn access logs, including copied proxy scopes.
It sets `Cache-Control: no-store` and callback `Referrer-Policy: no-referrer`.
This cannot sanitize an external ingress, APM wrapper, browser recorder, or
collector that sees a request first. Exclude `/v1/oidc/callback` from query/body
capture and upstream access logging, or log its path only. Configure
`OTEL_PYTHON_EXCLUDED_URLS=/v1/oidc/callback` when enabling auto-instrumentation,
and verify the actual deployed instrumentation and ingress settings. Never
record authorization codes, cookies, ID/access tokens, or client secrets.

The server uses fixed Google authorization/token/JWKS endpoints, verified TLS,
ten-second HTTP timeouts, bounded JSON bodies, no redirects, and no
environment-selected HTTP proxy. Plan approved HTTPS egress accordingly.
Signing keys are cached with bounded lifetime and refresh throttling.

## Browser routes

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/v1/authentication` | Whether Google login is enabled |
| POST | `/v1/oidc-authentications` | Start anonymous sign-in, or same-account reauthentication |
| POST | `/v1/guest-claims` | Explicitly start “sign in and save my guest games” |
| GET | `/v1/oidc/callback` | Server-only Google code exchange; fixed frontend redirect |
| GET | `/v1/oidc-authentications/current` | Browser-bound progress, completion CSRF token, guest game count |
| POST | `/v1/oidc-authentications/current/completion` | Explicitly confirm verified login/claim; rotate the session |
| GET | `/v1/me` | Guest or registered identity, capabilities, session expiry and normal CSRF token |
| GET | `/v1/sessions` | Own active sessions, opaque owner-scoped cursor, limit 1–50 |
| DELETE | `/v1/sessions/current` | Sign out this browser; does not delete games or other sessions |
| DELETE | `/v1/sessions/{id}` | Revoke an own active session; foreign/unknown IDs return 404 |
| POST | `/v1/session-revocations` | `{"scope":"other"}` or `{"scope":"all"}` |

Start/completion bodies are exactly `{}`. Principal IDs, issuer, subject,
provider tokens, and arbitrary return URLs are not accepted. All browser writes
require an allowed Origin. When a live session exists, starting sign-in/claim
also requires its normal `/v1/me` CSRF token. Completion instead requires the
flow-specific CSRF token from `/v1/oidc-authentications/current`.

### Frontend sequence

Keep CSRF tokens in memory and use `credentials: "include"`. The browser supplies
Origin; do not manually build Cookie or Origin headers.

1. Read `/v1/authentication` and `/v1/me`. Treat 401 from `/v1/me` as signed out.
2. Offer a clear **“Sign in and save my guest games”** action for a live guest.
   The normal sign-in endpoint returns `409 EXPLICIT_GUEST_CLAIM_REQUIRED`
   rather than discarding or silently merging that guest.
3. POST `{}` to `/v1/guest-claims` for that explicit action, otherwise
   `/v1/oidc-authentications`. Include the current session's CSRF token if any.
4. Navigate to the returned `authorization_url`. Do not log it. Google returns
   to the API, not the frontend. The API exchanges/verifies the code and sends a
   303 to the configured completion page with **no provider credentials**.
5. On that page, GET `/v1/oidc-authentications/current`. Show pending/exchanging,
   failed, or verified status. Failed/expired flows require a new start.
   For a verified guest claim, show its guest-game count and confirmation.
6. On confirmation, POST `{}` with **that flow's** CSRF token:

```javascript
const flow = await fetch(`${api}/v1/oidc-authentications/current`, {
  credentials: "include",
}).then(r => r.json());

// Run in the confirmation button handler after checking flow.status === "verified".
const response = await fetch(`${api}/v1/oidc-authentications/current/completion`, {
  method: "POST",
  credentials: "include",
  headers: { "Content-Type": "application/json", "X-CSRF-Token": flow.csrf_token },
  body: "{}",
});
if (!response.ok) {
  // Display a safe application error using the problem code; never log credentials.
  throw new Error("Authentication could not be completed");
}
const result = await response.json();
const identity = result.identity; // kind, principal_id, csrf_token, capabilities
```

7. Replace in-memory identity/CSRF state. Refetch games and ETags. Discard pending
   guest command retries and generate new idempotency keys: command receipts
   remain scoped to their original principal and are not copied into the account.
   Never blindly resubmit a guest game-creation request after a claim.

Normal game commands keep the same Origin, session CSRF, idempotency, ownership,
and revision checks for both identity kinds. Neither identity grants the
separate legacy ingestion/service credential.

## Identity and recovery rules

- Identity is verified **Google issuer plus subject**, never email. The two
  documented Google issuer spellings normalize to `https://accounts.google.com`.
  Only `openid` is requested. Email, name, picture, and hosted domain are not
  account-linking keys or authorization inputs.
- ID tokens require an allowed RS256 signature from fixed Google keys, issuer,
  audience/authorized party, issued-at/expiry, and the browser flow's nonce.
  Each start has fresh state, a separate HttpOnly browser-binding cookie, and
  S256 PKCE. Raw state, PKCE verifier, authorization code, ID/access tokens, and
  session credentials are not persisted.
- A flow lasts at most ten minutes, is bound to its starting session and exact
  configured client/return context, and consumes its callback before network
  exchange. A crash/uncertain exchange requires a new flow, never code replay.
  Verification alone grants no session and changes no ownership.
- Confirmation creates a new hashed-cookie session, default 30 days.
  A lost confirmation response can be retried only with the same unexpired
  browser binding, flow CSRF, and unchanged original/resulting session context.
  It reissues the same session without repeating claims or extending expiry.
  It cannot resurrect a revoked session.
- An explicit guest claim transfers **all** of that live guest's games into the
  verified account, including archived games. It keeps IDs, checkpoints, market
  revisions, ledger/fill/event audit, and original command receipts unchanged.
  Each game revision increments once to invalidate old ETags. An immutable,
  hash-checked transfer receipt records before/after revisions.
- Claims lock principals in deterministic order and games by ID. A permanent
  claim marker and revocation of every source guest session prevent stale
  in-flight requests from recreating guest-owned resources.
- `409 ADVANCE_IN_PROGRESS` leaves the guest and verified flow intact. Wait for
  the worker to finish, then confirm again before expiry. The transfer never
  interferes with a live worker lease/checkpoint.
- `409 GAME_LIMIT_REACHED` also leaves everything intact. Claims cannot bypass
  the current ten-game ownership quota, including archived games. Choose an
  account with capacity or resolve the product limit before losing the guest
  credential. Archiving does not free a slot.
- A saved account can recover its games by signing in with the same Google
  account from another browser. An **unclaimed** guest whose cookie was lost,
  expired, or revoked cannot be recovered by supplying its principal ID or an
  email address. Prompt guests to save before the seven-day expiry.

## Session lifecycle

Session lists disclose only opaque session IDs, creation/expiry/authentication
timestamps, and `is_current`. They expose no cookies, hashes, provider identity,
IP addresses, or user-agent fingerprints. Expired/revoked sessions are omitted.

Signing out the current session needs its normal CSRF/Origin proof but no fresh
Google verification. Revoking a different session or `other`/`all` requires
Google verification within the last five minutes. On
`403 REAUTHENTICATION_REQUIRED`, start `/v1/oidc-authentications` while signed in,
verify with the **same** Google account, and confirm. This rotates only the
current session and refreshes its authentication time. A different subject
returns `409 ACCOUNT_SWITCH_FORBIDDEN`; sign out explicitly before changing
accounts. Google may use its existing SSO session: this is fresh OIDC proof,
not a guarantee of password entry, MFA, or a particular authentication strength.

Revocations serialize with claims, session issuance, and game writes.
Account-wide revocation also sets an authorization cutoff: pending logins begun
before it cannot subsequently restore a session, including anonymous flows.
New Google proof started afterward remains a recovery path. Revocation receipts
are append-only. Completed identity/claim records are retained; only abandoned
expired flows and rate windows receive bounded opportunistic cleanup.

The old `/v1/guest-sessions/current` logout remains guest-only for compatibility.
Use `/v1/sessions/current` for either kind. Disabling Google login blocks new
flows but does **not** revoke existing account sessions. Logging out here does
not log out Google, revoke a Google grant, delete the account, or delete games.
Unlinking providers, account deletion, email recovery, and ranked play are not
implemented.

## Validation and remaining gates

Offline tests generate synthetic RSA-signed Google responses and never contact
Google. They exercise signature/claims, PKCE/nonce/state/browser binding, code
replay, explicit consent, unchanged trading audit, atomic rollback, quotas,
recovery, session isolation/revocation, and legacy guest migration.
Opt-in PostgreSQL tests cover competing first logins/claims, duplicate
confirmation, stale in-flight guest writes, global-revocation races, durable
rate limits, worker/claim coordination, model parity, and immutability triggers.

Before public launch: configure the real Google application and secrets,
validate consent and callback behavior end-to-end over HTTPS, review upstream
logging/telemetry and trusted proxies, finish the frontend, define retention/
incident procedures, and qualify/publish real market data through the separate
review gate. No real Google login or production rollout has been exercised here.
