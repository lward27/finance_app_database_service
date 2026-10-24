# Historical trading API: foundation slice

This implements the first slice of the approved trading-game plan. It does not
enable trading or treat the Yahoo backfill as simulation-qualified data.
Existing exploration reads and the backfill worker retain their contracts.
Nothing here automatically migrates production, publishes data, or starts a game.
For the later, explicitly gated game routes, see [persistent games](persistent-games.md).

## Routes

| Method | Path | Behavior |
| --- | --- | --- |
| POST | `/v1/guest-sessions` | Create an anonymous principal and seven-day session |
| GET | `/v1/me` | Own principal, session expiry, CSRF token, and capabilities |
| DELETE | `/v1/guest-sessions/current` | Revoke the session without deleting its principal |
| GET | `/v1/rulesets` | Published immutable rule definitions |
| GET | `/v1/rulesets/{id}` | Definition and SHA-256 |
| GET | `/v1/instruments` | Bounded search by symbol, alias, name, or exchange |
| GET | `/v1/instruments/{id}` | Listing metadata, aliases, and legacy ticker mappings |
| GET | `/v1/market-data/releases` | Published, qualified coverage summaries |
| GET | `/v1/market-data/releases/{id}` | Pinned calendar, coverage, revision IDs, validation hashes |

Interactive OpenAPI remains at `/docs`. New failures use
`application/problem+json` with `code` and `request_id`, also returned in
`X-Request-ID`. Legacy ticker-validation error bodies remain unchanged.
Instrument cursors are scoped to normalized filters; reusing a cursor with a
different query or endpoint returns `422 INVALID_CURSOR`. Limits are 100
instruments and 20 release summaries per page.

## Identity and configuration

Before enabling these routes, configure:

- `FINANCE_ALLOWED_ORIGINS`: comma-separated, exact HTTPS frontend origins, with
  no trailing slash or wildcard. HTTP is allowed only for loopback development,
  but the session cookie still requires HTTPS.
- `FINANCE_GUEST_RATE_LIMIT_KEY`: a secret of at least 32 characters from the
  deployment's secret manager. Missing/short configuration disables guest creation.
- `FINANCE_SERVICE_TOKEN`: a separate secret of at least 32 characters for
  ingestion/admin writes. Missing/short configuration disables those writes.
- If behind an ingress, configure Uvicorn's `FORWARDED_ALLOW_IPS` with the actual
  trusted proxy addresses and appropriate network restrictions. Never trust
  arbitrary forwarded headers or `*`: otherwise the limiter can be bypassed, or
  all users can appear to share the ingress's address.

Cookies use `__Host-finance_session`, Secure, HttpOnly, SameSite=Lax, Path=/,
and no Domain. Use same-site HTTPS frontend/API hosts; cross-site third-party
cookie sessions are not supported by this slice. Session tokens have 256 bits
of randomness; only their SHA-256 hashes are stored. Principals have opaque IDs.
[Google sign-in and explicit guest recovery](google-identity.md) now use those
verified issuer/subject identities and add session management in migration
`0005`. Google login remains disabled until configured. Ranked play remains disabled.
Game creation capability follows `FINANCE_GAMES_ENABLED` (false by default);
an enabled flag alone never substitutes for actual qualified market inputs.

Every guest write requires an exact allowed `Origin`. Revocation additionally
requires `X-CSRF-Token` from creation or `/v1/me`. CSRF tokens are derived from
the HttpOnly session secret with domain-separated HMAC, never from a principal
ID supplied by a client. Session responses use `Cache-Control: no-store`.

Guest creation allows ten successful creations per client address per
ten-minute fixed window, with an atomic PostgreSQL counter shared by replicas.
Only keyed, window-specific address hashes are stored; expired windows receive
bounded cleanup. Invalid origin/body requests cannot create principals.
`429` includes `Retry-After`. An active cookie returns `409 SESSION_ALREADY_ACTIVE`
rather than silently replacing the guest and losing access to future games.

Anonymous session bootstrap does not replay cookie credentials using an
`Idempotency-Key`, which is not authentication. A lost bootstrap response can
leave an unused principal; a retained cookie can recover its own identity via
`/v1/me`. Game commands now have their own idempotency/revision protocol,
described in the game guide; this identity endpoint does not replay credentials.

Browser example, with Origin supplied automatically by the browser:

```javascript
const session = await fetch(`${api}/v1/guest-sessions`, {
  method: "POST",
  credentials: "include",
  headers: { "Content-Type": "application/json" },
  body: "{}",
}).then(r => r.json());

await fetch(`${api}/v1/guest-sessions/current`, {
  method: "DELETE",
  credentials: "include",
  headers: { "X-CSRF-Token": session.csrf_token },
});
```

Keep the CSRF token in memory and recover it via `/v1/me` after reload. Do not
copy session cookies into local storage. Revocation retains the principal and
future owned records; an expired/revoked cookie returns 401.

## Compatibility and deployment prerequisites

Anonymous legacy GETs remain available, including wildcard **non-credentialed**
CORS. `/v1` and browser writes use only the configured credentialed origin list.
All legacy ticker/history mutations and scraper-run commands now require
`Authorization: Bearer <service credential>`. A guest cookie does not grant
ingestion privileges. Coordinate scraper credentials and removal of frontend
destructive controls before deploying this change. This is an intentional
write-security change, not a silent compatibility promise for old write clients.

Run migrations as a separate, reviewed deployment step, using the same PG
configuration as the service:

```sh
PYTHONPATH=src alembic -c alembic.ini upgrade head
PYTHONPATH=src python -m finance_app_database_service.catalog
PYTHONPATH=src python -m finance_app_database_service.catalog --execute --batch-size 250
```

The catalog command defaults to a read-only preview. Apply it after the legacy
ticker catalog exists. It commits small, resumable batches; concurrent bootstraps
use conflict-safe inserts. IDs are deterministic opaque UUIDs derived from legacy
IDs, not symbols. Duplicate symbols remain separate until a reviewed canonical
mapping exists. No currency is inferred from an exchange or marked verified.
Existing ticker/history rows are not rewritten.

Migration `0001` creates absent legacy/import tables on a new database and adopts
existing ones without rewriting them. `0002` adds only foundation tables and the
fixed ruleset. API startup is restricted to the old bootstrap tables; importing
a router cannot implicitly create the foundation schema. Unmigrated `/v1` routes
fail with a sanitized 503 instead of attempting schema changes.

PostgreSQL migrations use a separate transaction advisory lock and a five-second
lock timeout, never the running backfill's singleton lock. As with any schema
change, validate against a representative test database before a production
gate. Automatic destructive downgrades are deliberately disabled. An approved
code rollback retains all data, sessions, and backfill checkpoints.

## Rules and immutable market inputs

The frozen `us-equity-margin-v1` definition discloses next-open all-or-reject
orders, long/short/fractional shares, 50% initial and 30% maintenance margin,
8% annual debit interest, 3% short borrow fees, and actual calendar days / 365.
Short proceeds remain restricted by FIFO lot. Dividends book on the ex-date
using prior-close entitlement; commissions, slippage, and cash interest are zero.
These are educational rules, not a historical broker simulation.

All financial JSON is strict decimal strings. Shared helpers quantize money to
eight decimal places with half-even rounding and notional quantities downward
to eight decimal places. Binary floats, nonfinite values, and overflow are
rejected. Rules are checksum-verified and database triggers reject their updates
and deletes; changes require a new ID/migration.

Release IDs are `rel_` plus SHA-256 of canonical, validated manifest JSON.
Manifests pin USD/as-traded prices, calendar version, instrument coverage,
separate bar/action revision hashes, source snapshots, and validation reports.
Published releases cannot be updated or deleted. Draft and unqualified rows are
not public; invalid published manifests/checksums fail closed.

**There is no release-publication endpoint yet.** This contract is not a
qualification pipeline. Yahoo `yahoo_split_adjusted` / `source_only` snapshots
remain in their existing tables and never automatically appear as releases.
An [offline preflight](market-qualification.md) can now compare archived exports
with independent reference evidence, producing only a pending review candidate.
The [operator publisher](market-publication.md) rechecks the original inputs,
requires a separately authenticated review, and atomically writes reports,
verified metadata, immutable revisions, a release, and its approval receipt.
It defaults to offline preview and never enables games. Real evidence review
and separately authorized publication are still required before that gate.

## Validation and next slices

Install the hashed development lock in Python 3.11:

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.lock
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q src tests migrations
```

Default tests use isolated SQLite, including concurrent quota admission,
migration adoption preserving backfill checkpoints/cooldowns, immutable-record
guards, security boundaries, and legacy read compatibility. SQLite is not proof
of PostgreSQL NUMERIC behavior or PostgreSQL trigger/concurrency semantics.
To exercise those paths, set `FINANCE_TEST_DATABASE_URL` to a disposable
PostgreSQL database whose name ends in `_test` and rerun the suite. These opt-in
tests create/drop only their own randomly named schemas, never production
tables. Without that setting, they are explicitly skipped.

The [deterministic simulator core](trading-simulator.md) now implements FIFO/ledger
accounting, next-open fills, splits/dividends, financing, session snapshots, and
margin interruption/resolution. The [persistent game slice](persistent-games.md)
now adds owned storage, idempotent revision-checked commands, scoped market reads,
and durable advance processing around those checkpoints.

Google identity and guest-claim APIs are now implemented separately.
Real source evidence review/publication, frontend integration,
Google application setup, and production deployment remain unfinished work.
Ranked challenges follow the single-player launch.
