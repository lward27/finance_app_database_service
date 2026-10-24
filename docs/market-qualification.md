# Offline market-data qualification preflight

This is a read-only comparison stage, **not a publication or qualification
authority**. It compares a genuine archived Yahoo export with independently
prepared reference evidence and emits a `pending` candidate for operator review.
It never connects to a database, calls a provider, changes backfill records,
verifies instrument metadata, publishes a release, or enables games.

Passing means the supplied documents agree under the supported reconstruction
rules. It does **not** prove that the references are authentic, independent,
complete, licensed, or correctly mapped to the canonical instrument. A made-up
reference can agree with made-up prices. An operator must authenticate the
evidence before a separate approved publication step.

## Inputs and provenance

Prepare one JSON object matching `QualificationInput` in
`finance_app_database_service.market_qualification`. Print its schema offline:

```sh
PYTHONPATH=src .venv/bin/python -c \
  'from finance_app_database_service.market_qualification import QualificationInput; print(QualificationInput.schema_json(indent=2))'
```

It contains:

- `schema_version: 1`.
- `calendar`: a `CalendarDocument` with an explicit ID, immutable version, and
  sorted, unique trading sessions, including both endpoints of each coverage.
- `calendar_reference`: independent `provider` and `reference` strings
  identifying the authoritative calendar artifact.
- `instruments`: at most 50 explicitly selected canonical instruments.

Each instrument supplies its `instrument_id`, exact `symbol`, `from_date`,
`to_date`, explicit boolean `shortable`, `source_snapshot_id`, `source_export`,
and `evidence`. One original source snapshot cannot be mapped to two instruments
in the same candidate. Resolve duplicate legacy mappings before using them.

`source_export` is the original full-history export JSON object, not database
rows, adjusted-close history, or a path. The snapshot ID must match the existing
importer's canonical content hash, which excludes only `fetched_at`. Keep the
original archive and its retrieval timestamp. The preflight recomputes the hash.

**Existing backfill database rows are not an archive of the exact export JSON.**
They do not preserve all original string formatting. Do not manufacture an
export, change a source snapshot ID, or relabel stored rows to make the checksum
pass. This slice adds no archive downloader or backfill export endpoint.

The `evidence` object requires:

- The same canonical `instrument_id`, `symbol`, and exact coverage dates.
- `currency: "USD"`, `price_basis: "as_traded"`, and
  `exchange_timezone: "America/New_York"`.
- `terminal_events: "none_in_coverage"`, backed by independent review of the
  entire proposed interval, not inferred from an absence of Yahoo rows.
- Separate `currency_reference`, `prices_reference`, `actions_reference`, and
  `terminal_events_reference` objects, each with `provider` and `reference`.
- `bars`: one independently sourced opening/closing price for every covered
  session, sorted by date. Financial values are plain decimal strings with at
  most eight decimal places. Null or zero covered prices are rejected.
- `actions`: the complete, sorted, unique list of splits and dividends.
  Splits use positive `split_numerator`/`split_denominator`; dividends use
  `dividend_per_prior_share`. An empty list explicitly asserts no actions.

Each reference should identify an immutable external artifact or retained
evidence document, including its edition/date. No URL is fetched by this tool.
It rejects references openly naming Yahoo/yfinance as their provider, but that
is only an obvious-self-comparison check, not authentication. Do not create
reference prices or actions by copying reconstructed Yahoo output.

## Machine checks

The preflight validates original export identity, source basis and timezone,
completed coverage, calendar membership/completeness, strict financial inputs,
positive sensible OHLC, and agreement with every reference open and close.
Missing sessions are errors, not suspensions invented by the tool.

For each covered date, split-adjusted open/close and dividends are multiplied by
the product of splits **strictly after that date** in the full source export.
Splits after the requested coverage, including a split on the retrieval session,
remain part of that product. A same-day split is not multiplied into that day's
prices. The retrieval session's unfinished prices are never candidate bars.
Adjusted close is never substituted for opening/closing prices.

Reconstructed values use the simulator's eight-place half-even money policy
under an isolated decimal context. Splits must match reference rational ratios;
dividends must match reference prior-close entitlements. A nonzero dividend
cannot disappear by rounding to zero.

The initial conservative policy blocks:

- Unknown/unverified terminal events or intervals without the explicit
  no-terminal-events reference claim.
- Capital-gain distributions within coverage.
- Same-session split plus nonzero dividend, whose entitlement semantics need a
  separately reviewed implementation.
- Missing/zero prices, gaps, extra non-calendar rows, unsupported identity/basis/
  currency/timezone, unmatched or extra actions, overflow, or capacity excess.

Reverse splits can pass data comparison. This does not remove the simulator's
separate refusal to invent cash-in-lieu for unsupported fractional reverse splits.

Limits are 64 MiB input, 100,000 source rows across the candidate, 25,000 sessions
per calendar/instrument document, 16 MiB per revision, and 32 MiB candidate output.
Excess input is rejected rather than truncated. The CLI also rejects duplicate
JSON object keys, nonfinite JSON constants, malformed encoding, and excessive
JSON nesting.

## Run and review

Default operation prints only a compact hash/count/status summary, with no
financial rows and no file writes:

```sh
PYTHONPATH=src .venv/bin/python -m finance_app_database_service.market_qualification \
  --input /absolute/path/to/qualification-input.json
```

Explicitly save a **new** candidate file:

```sh
PYTHONPATH=src .venv/bin/python -m finance_app_database_service.market_qualification \
  --input /absolute/path/to/qualification-input.json \
  --output /absolute/path/to/new-candidate.json
```

The output is created exclusively with mode `0600`. Existing files, including
the input, are never overwritten. A successful summary contains
`status: "ready_for_operator_review"`, `qualification: "pending"`, normalized
`input_sha256`, `candidate_sha256`, counts, and `output_written`. Hashes use
canonical validated JSON, not raw file bytes or the output's final newline.
Failure exits nonzero with a sanitized `blocked` code. Comparison failures
produce no candidate. Filesystem write failures must be resolved before using
any partially written output.

The candidate contains content-addressed calendar/bar/action documents, source
snapshot identities, coverage, reference citations, and hashes of the complete
validated evidence. Retain the original input with it: the candidate does not
duplicate all reference prices. It deliberately contains **no qualification
validation report, published manifest, publication timestamp, or database
write command**. It cannot satisfy the game's qualified-release gate.

Next, an operator must authenticate the independent evidence, verify listing
identity and shortability policy, review any gaps/unsupported actions, and
approve the separate [operator publication transaction](market-publication.md).
That workflow rechecks the original inputs and writes verified metadata, report
bindings, immutable revisions, and an immutable approval receipt atomically.
Production migration, publication, and enabling games require their own approval.

## Validation

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests \
  -p test_market_qualification.py -v
```

Fixtures are synthetic, not evidence for any real listing. Tests cover split/
dividend reconstruction, full-export split factors, reverse splits, half-even
rounding, strict contracts, integrity/gap/action failures, capacity, deterministic
decimal behavior, offline execution without database dependencies, sanitized
errors, and private no-overwrite output. No real source was qualified by adding
or running these tests.
