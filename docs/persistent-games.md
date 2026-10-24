# Persistent single-player games

This slice connects the deterministic simulator to owned storage, player APIs,
and a durable worker. It does not enable games in production, publish market
data, change ingestion, or modify the live Yahoo backfill.

## Launch gates

Game routes default to `503 GAMES_DISABLED`. After a separate approved migration
and qualification/publication step, `FINANCE_GAMES_ENABLED=true` enables the
routes and advertises creation capability through `/v1/me`.

The flag is **not** data qualification. A playable release also requires:

- A published, checksum-valid `simulation_qualified` / USD / `as_traded` manifest.
- Verified USD instrument metadata.
- Immutable `qualified_market_revision` rows containing the actual calendar,
  bars, actions, and validation reports, addressed by canonical SHA-256.
- An immutable `qualified_calendar` binding of calendar ID/version to its
  revision, and reports referencing that exact calendar and manifest revisions.
- Reports covering currency, calendar, price basis, corporate actions, and
  terminal events, with the manifest's source snapshot identities.

The read adapter verifies those references and hashes. It does not establish
their truth from Yahoo data. A separate operator qualification/publication
pipeline must produce the evidence before writing these records. No player or
service publication/import endpoint is added by this slice. Never manufacture
reports or mark source snapshots qualified just to bypass the gate.

The [offline qualification preflight](market-qualification.md) now provides a
comparison stage for archived exports and independent reference evidence. Its
output remains `pending`, requires operator review, and cannot satisfy this gate.
The [operator publisher](market-publication.md) implements a separate, preview-first
publication transaction after authenticated independent review. Migration `0004`
adds its immutable approval receipts. No real release is published automatically.

[Google sign-in, guest claims, and session management](google-identity.md) are
now implemented behind separate configuration, with migration `0005`.
An explicit claim saves a live guest's games into a recoverable account.
Unclaimed guest cookies still expire after seven days and cannot be recovered
by creating another guest. Do not launch publicly without configuring and
testing the browser identity/recovery flow.

## Routes

All paths below start with `/v1/games`. Every read requires a valid session.
Every game lookup and nested lookup enforces ownership, returning 404 for
another principal's resources. Every game response, including errors, is
`Cache-Control: no-store`.

| Method | Suffix | Behavior |
| --- | --- | --- |
| POST | empty | Fund a new owned game and pin its release/rules |
| GET | empty | Own games, status/archived filters and cursor pagination |
| GET | `/{game_id}` | Cursor, lifecycle, revision, active advance, allowed trading actions |
| PATCH | `/{game_id}` | Rename only |
| POST | `/{game_id}/archives` | Freeze/hide without deleting audit data |
| POST | `/{game_id}/replays` | Fresh game with the same initial configuration |
| POST | `/{game_id}/orders` | Queue next-open quantity/notional order |
| GET | `/{game_id}/orders` | Own orders, optional status filter and bounded cursor |
| GET | `/{game_id}/orders/{order_id}` | Intent, status, rejection, and actual fill if executed |
| POST | `/{game_id}/orders/{order_id}/cancellations` | Cancel pending intent |
| POST | `/{game_id}/order-previews` | Non-binding estimate at the visible close |
| GET | `/{game_id}/portfolio` | Current cash, NAV, exposure, margin, and P&L |
| GET | `/{game_id}/positions` | Current marked quantities and FIFO basis |
| GET | `/{game_id}/trades` | Append-only executed fills |
| GET | `/{game_id}/ledger` | Balanced append-only transactions/postings |
| GET | `/{game_id}/events` | Ordered append-only game timeline |
| GET | `/{game_id}/performance` | Bounded daily valuation/P&L curve |
| GET | `/{game_id}/instruments` | Currently eligible release universe, symbol/name search |
| GET | `/{game_id}/quotes` | Visible closing quotes for up to 20 instrument IDs |
| GET | `/{game_id}/history` | One instrument's bounded opening/closing history |
| GET | `/{game_id}/calendar` | Bounded trading dates, no prices |
| POST | `/{game_id}/advances` | Accept asynchronous target/by-date advance |
| POST | `/{game_id}/margin-resolutions` | Accept one-session reduction/liquidation resolution |
| GET | `/{game_id}/advances/{operation_id}` | Durable progress and interruption/block result |

OpenAPI at `/docs` documents the strict command and response schemas.
Financial JSON is decimal strings. Clients cannot supply owners, balances,
fills, execution prices, or cursor updates.

Creation chooses the newest published release when `release_id` is omitted.
It never silently falls back from an invalid/incomplete newest release; provide
an eligible explicit ID if needed. Start must lie within release bounds and
normalizes forward to a session. End normalizes backward, defaults to the
release's final session, and must follow start.

Quotes/history/previews never use prices after the current game cursor.
History/performance ranges beyond the cursor return `FUTURE_DATA_FORBIDDEN`;
calendar dates may extend to the game end, but carry no prices. Missing history
bars remain explicit null opening/closing values inside instrument coverage.
No live-provider fetch or forward-fill occurs.

## Commands, retries, and concurrency

Cookie writes require exact allowed `Origin` and `X-CSRF-Token`. Persisting
commands additionally require `Idempotency-Key`, using 1–128 ASCII letters,
digits, `.`, `_`, `:`, or `-`. Mutations of existing games require the exact
strong ETag from a prior game response in `If-Match`.

Successful/accepted commands atomically store their receipt with the game
changes. Retrying the same principal/key/route/payload returns the original body,
status, ETag, and Location, even with the original stale If-Match or during a
later advance. A changed payload or route returns `409 IDEMPOTENCY_CONFLICT`.
Validation/rejected commands do not reserve their keys.

Missing If-Match returns 428; stale If-Match returns 412 with current ETag.
New commands cannot mutate an advancing game (`409 ADVANCE_IN_PROGRESS`) or an
archived game. Renames, archives, and replays do not rewrite financial history.
Idempotency keys are not authentication or a replacement for revisions.

Admission locks the principal before the game, serializing receipt lookup and
command admission across replicas. Workers lock game before operation and never
lock principals. The database additionally allows only one queued/running
advance per game. Orders, ledger keys, and audit sequences have game-scoped
unique keys. Immutable audit/input triggers reject updates and deletes.

Mutable orders/positions use revision-scoped cursors. Append-only trades,
ledger, and events use game/endpoint-scoped sequence cursors. Audit reads cap
their sequence at the loaded checkpoint, avoiding a mix of old cursor metadata
and newly committed future rows. Operation progress stays attached to that
operation after subsequent advances.

Initial operational limits: ten total games per principal (including archived),
60 successful commands per ten minutes, 1,000 orders per game, initial cash up
to one trillion USD, at most 50 release instruments, 16 MiB per revision,
32 MiB of revision inputs per load, and a 16 MiB checkpoint. Ingress-level
read/preview limits and capacity planning remain deployment prerequisites.

## Browser flow

Use the secure same-site cookie and in-memory CSRF token from the foundation guide.
Origin is supplied by the browser. Keep the same key and payload when retrying:

```javascript
const created = await fetch(`${api}/v1/games`, {
  method: "POST", credentials: "include",
  headers: {
    "Content-Type": "application/json",
    "X-CSRF-Token": csrf,
    "Idempotency-Key": crypto.randomUUID(),
  },
  body: JSON.stringify({
    name: "Historical experiment",
    start_date: "2020-01-02",
    initial_cash: "10000.00",
    ruleset_id: "us-equity-margin-v1",
    release_id: qualifiedReleaseId,
  }),
});
const game = await created.json();
const queued = await fetch(`${api}/v1/games/${game.id}/orders`, {
  method: "POST", credentials: "include",
  headers: {
    "Content-Type": "application/json",
    "X-CSRF-Token": csrf,
    "Idempotency-Key": crypto.randomUUID(),
    "If-Match": created.headers.get("ETag"),
  },
  body: JSON.stringify({instrument_id: instrumentId, action: "buy", notional: "2500.00"}),
});
// Check response.ok before consuming the next ETag.
// The order is still pending. An advance fills it at the next eligible open.
```

POST advances with `{"by":{"count":3,"unit":"months"}}` or `{"to_date":"2021-01-04"}`,
never both. Use the latest game revision. A 202 response supplies a Location
for polling; no in-process FastAPI background task performs simulation.

## Durable worker and storage

Migration `0003_persistent_games` adds qualified-input storage, owned game
checkpoints, current order projections, append-only ledger/postings/fills/
snapshots/events, command receipts, and advance operations. It creates no
qualified release or playable scenario. Run reviewed migrations separately:

```sh
PYTHONPATH=src .venv/bin/python -m alembic -c alembic.ini upgrade head
```

Checkpoints are versioned canonical JSON with a checksum and strict round-trip
validation; financial postings additionally use PostgreSQL `NUMERIC(28,8)`.
Full engine history is retained in the bounded checkpoint for now. This is not
yet a large-universe or unlimited-history reporting architecture.

Preview the queue without claiming:

```sh
PYTHONPATH=src .venv/bin/python -m finance_app_database_service.advance_worker
```

Explicitly process one bounded claim, or run the separate long-lived worker:

```sh
PYTHONPATH=src .venv/bin/python -m finance_app_database_service.advance_worker \
  --execute --once --max-checkpoints 25
PYTHONPATH=src .venv/bin/python -m finance_app_database_service.advance_worker --execute
```

Use the established approved deployment path to run a worker with the same
immutable source/image and database as the API. No worker deployment is added
or applied automatically. `--max-checkpoints` is bounded to 1–1,000.

Each claim gets a fresh fencing token and a 120-second lease. Each transaction
locks its game, rechecks token/expiry, computes one session, appends all audit
deltas, updates the checkpoint/revision and operation progress, and renews or
clears the lease. It checks expiry again before commit. A savepoint rolls back
all partial projection work before committing an expected blocked-data result.
Database/process failures roll back the transaction; lease expiry permits
recovery from the last committed cursor without another fill or financing charge.

Margin calls interrupt at the first breaching close. Resolution processes one
next session, using queued reductions or `{"liquidate_all":true}`. Missing
data/revision failures return `blocked_data`; a new command with a new key can
retry from the retained cursor once the issue is resolved.

Preview is read-only, but `--execute` explicitly authorizes queue processing.
Disabling the API feature flag does not stop an already running worker.
Graceful worker termination retains checkpoints and operations for lease
recovery. Do not delete tables or rewind financial rows as a rollback.
Automatic destructive migration downgrades remain disabled.

## Validation and remaining work

Use the hashed Python 3.11 development environment:

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q src tests migrations
.venv/bin/python -m pip check
```

Set `FINANCE_TEST_DATABASE_URL` to a disposable PostgreSQL database ending in
`_test` to include real PostgreSQL checks. Each uses a private random schema
and cleans it afterward. SQLite cannot prove PostgreSQL decimal/concurrency
behavior. Fixtures containing qualification reports are deliberately synthetic,
not source qualification or release publication.

Remaining launch work: real source qualification/publication for a curated
universe, identity/recovery, frontend integration, ingress limits/observability,
worker capacity validation, and explicit deployment. Benchmark selection,
multi-instrument/aggregated OHLCV charts, derived performance statistics, more
advanced search/filtering, and ranked challenges remain later extensions.
