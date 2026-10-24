# Finance App Database Service

A Python 3.11 FastAPI database service for the Finance Product.

## Historical trading API foundations

The first `/v1` slice adds secure guest sessions, disclosed immutable rules,
stable instrument mappings, and qualified market-release read contracts.
See [the API guide](docs/trading-api-foundations.md) for routes, browser examples,
configuration, migration/bootstrap commands, tests, and remaining phases.
Game commands are implemented but disabled by default.
[Google sign-in, explicit guest recovery, and session management](docs/google-identity.md)
are implemented but Google login is also disabled until separately configured. The
[offline qualification preflight](docs/market-qualification.md) compares archived
exports with supplied independent evidence, but does not establish evidence
authenticity or make a release playable.
The [operator publisher](docs/market-publication.md) can publish an independently
reviewed candidate with an authenticated approval and exact database-target gate.
It defaults to offline preview. No real release has been approved or published
by these implementation/test steps.

**Deployment prerequisite:** run the reviewed Alembic migration and configure
allowed frontend origins and separate guest-rate-limit/service credentials.
Legacy reads retain their bodies; legacy writes now require a service Bearer
credential. Coordinate scraper/frontend changes before rollout. No migration,
deployment, data qualification, or live backfill change happens automatically.

## Historical trading simulator

The next slice implements a pure deterministic simulator: balanced FIFO books,
next-open long/short fills, splits/dividends, financing, session snapshots, and
margin interruption/resolution. See [the simulator guide](docs/trading-simulator.md).
The [persistent game API](docs/persistent-games.md) adds owned games, orders,
portfolio/audit reads, game-scoped market data, and a durable advance worker.
It requires the current Alembic head (game storage starts at `0003`), an explicitly enabled feature flag, and actual
hash-checked qualified revisions. The live Yahoo backfill does not satisfy that gate.

## Ticker Symbol Normalization

All ticker creation and history lookup endpoints now normalize and validate
ticker symbols **before any database access**.

### Normalization Rules

1. **Trim** leading and trailing whitespace.
2. **Uppercase** the symbol.
3. **Validate** against the structural regex: `^[A-Z][A-Z0-9.\-]{0,9}$`

Valid tickers are 1–10 characters, start with a letter, and contain only
letters, digits, dots (`.`), or hyphens (`-`).

### HTTP 422 Response

If a ticker symbol fails validation, the API returns a stable **HTTP 422**
response with the following JSON body:

```json
{"detail": "Invalid ticker symbol"}
```

This happens for:
- Empty or whitespace-only strings
- Symbols starting with a digit
- Symbols containing invalid characters (e.g. `@`, `_`, `!`)
- Symbols longer than 10 characters

### Affected Endpoints

- `POST /tickers` — validates `ticker` field before insert
- `GET /history` — validates `ticker_name` query parameter before lookup
- `GET /history/last_date` — validates `ticker_name` query parameter before lookup

## Corporate actions and full-history backfill

The new data is separate from `price_history`. Existing frontend/scraper contracts
are unchanged. Additive tables are registered at API startup:
`market_backfill`, `market_backfill_item`, `market_backfill_throttle`,
`market_snapshot`, and `market_history`.

The importer calls the wrapper's `GET /history/full` once per ticker, preserving
split-adjusted OHLCV, adjusted close, cash dividends, split ratios, capital-gain
distributions, source version, retrieval time, and exchange timezone. Financial
values are decimal strings in transit and PostgreSQL NUMERIC in storage.
Duplicate catalog symbols retain their separate legacy ticker IDs.

**These are source snapshots, not simulation-qualified releases.**
`auto_adjust=False` is not an as-traded price guarantee. The original Yahoo basis
is explicitly recorded. `subsequent_split_factor` is the product of splits
strictly after a row's date, including splits on the latest source date.
It is a reconstruction aid, not permission to apply actions twice. Currency,
calendar completeness, delistings, and provider split/dividend semantics still
need qualification before the trading engine uses this data.

Rows from the current exchange-local date remain in the source snapshot for
corporate-action provenance but are excluded from the history read API. Missing
prices remain null, never forward-filled. Loading snapshots are never published.

### Running

Deploy the wrapper change first, then this service. Preview without upstream
requests or writes (using the same PG environment as the service):

```sh
PYTHONPATH=src python -m finance_app_database_service.backfill \
  --name yahoo-full-history-v1
```

Execute/resume the same named run:

```sh
PYTHONPATH=src python -m finance_app_database_service.backfill \
  --execute --name yahoo-full-history-v1 \
  --interval-seconds 60 --batch-size 250 \
  --ticker-batch-size 10 --batch-pause-seconds 300
```

Use one Kubernetes Job with this command, not a laptop/background HTTP request.
API startup creates the additive tables; the worker can also create just those
tables under its singleton advisory lock. No legacy rows are updated/deleted.

- One worker across all named backfills, enforced with a PostgreSQL advisory lock.
- At least 60 seconds between request starts, persisted before each request.
- After every response/error, reserve another interval plus five seconds. This
  avoids client/server arrival-time races even when a request was dispatched
  slowly. It never shortens an existing Retry-After or rate-limit cooldown.
- Extra five-minute pause after every ten terminal ticker results.
- Database inserts commit in 250-row batches, with a 250ms write pause.
- HTTP 429 honors `Retry-After` and pauses globally for at least 15 minutes,
  exponentially increasing to six hours, plus jitter. Rate limits do not exhaust
  a ticker's retry budget.
- Transient failures retry with exponential delays; five failures record a gap.
  Missing/delisted/unavailable Yahoo symbols are recorded as `no_data`, not success.
- Membership, partial snapshots, retries, and cooldown survive pod restarts.
  Reusing a run name requires identical configuration. Completed tickers are skipped.
- `--max-tickers 1` bounds an execution for a pilot; rerun without it to continue.
- A 6GiB total-database safety ceiling stops further requests before another
  ticker when storage is tight. Raise `--max-database-bytes` only with reviewed
  capacity and a new run configuration. An in-flight ticker can exceed the ceiling.

Yahoo does not publish a guaranteed safe quota. Its library can make more than
one underlying HTTP request per history call; existing ingestion also consumes
quota. Backoff is essential. For 5,651 records, allow roughly six days plus errors
and rate-limit pauses, not a promised completion time.

### Monitoring and reading

- `GET /market-data/backfills/{name}`: counts, status, and next allowed request.
- `GET /market-data/backfills/{name}/items?status=failed`: inspect recorded gaps;
  supports `after_ticker_id` and bounded `limit`.
- `GET /market-data/tickers/{ticker_id}/history`: latest completed snapshot;
  supports `from_date`, `to_date`, `after`, `limit` (max 1000), and `snapshot_id`.
  Pin the returned snapshot ID on later pages for stable pagination.

The raw source basis and qualification warning accompany every history page.
Backfill monitoring is read-only; no public import/write endpoint is added.
To stop safely, stop the named Job with explicit operator approval. Keep its
database checkpoints. Do not prune the import tables as a rollback procedure.

## Build and validation

The Dockerfile uses the hashed dependency lock, runs the unit/compile checks in
Python 3.11, and requires the full source commit in its OCI revision label.

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.lock
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q src tests migrations
```
