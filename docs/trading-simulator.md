# Deterministic historical trading simulator

This is the second implementation slice of the trading-game plan. It adds a
pure Python simulation core. The later [persistent game slice](persistent-games.md)
adds the API and worker around it. Game creation remains disabled by default. No production migrations,
deployments, source imports, or backfill changes are part of this slice.

## Modules and entry points

- `accounting.py`: immutable FIFO lots, balanced ledger postings, cost-basis
  release, restricted short proceeds, valuation, and reconciliation.
- `simulation.py`: immutable orders/fills/events/checkpoints and a `Simulator`
  bound to a release manifest, calendar sessions, and as-traded bars.
- `Simulator.create(start, end, initial_cash)`: one initial contribution at the
  starting session close. Both dates must be sessions, with end after start.
- `queue(...)` / `cancel(...)`: return new states; never fill at the visible close.
- `step(state)`: return exactly one complete next-session checkpoint. A failed
  step leaves its input untouched. This is the durable worker's commit boundary.
- `advance(state, target)`: process every intervening session, returning the
  actual state, requested/effective target, outcome, and optional block code.
- `resolve_margin(state, liquidate_all=False)`: process one next session using
  queued reductions, or player-authorized full liquidation at that open.
- `shifted_date(cursor, count, unit)`: calendar days/weeks/months/years, clamping
  month/year boundaries. Target normalization never reads future prices.

All financial inputs are finite `Decimal` values, not floats. Inputs have at
most eight decimal places and fit `NUMERIC(28,8)`. Calculations use an isolated
60-digit context. Money uses half-even rounding; notional quantities round down.
Oversized/unrepresentable opening orders are rejected without blocking the day.
The API layer must separately validate/serialize strict decimal strings.

## Accounting and session order

Every ledger transaction balances to zero. Positive postings are debits:
cash and long inventory are normally debit balances, short inventory and initial
capital are credits. Gains/dividend income are credits; financing expenses are
debits. Inventory remains at cost; mark-to-market values live in snapshots.
Partial reductions release FIFO basis/proceeds, and final reductions release
the exact remaining amount so rounding cannot strand a restriction.

Each session:

1. Charge 8% annual margin interest on prior-close debit and 3% annual short
   borrow fees on prior-close short value, using elapsed calendar days / 365.
   Restricted short proceeds never offset the debit used for interest.
2. Book prior-close dividend entitlements and apply splits to lots and pending
   quantity orders. Notional orders retain their original cash amount.
3. Execute reductions before openings, with acceptance order preserved within
   each group. Convert notional at the actual open. Recheck 50% initial margin
   at opening prices for each risk-increasing fill, all-or-reject.
4. Mark every remaining position at that session's close, reconcile the ledger
   and FIFO inventory, and append portfolio/position snapshots and ordered events.
5. Stop at the first 30% maintenance breach. Only reductions/cancellations are
   accepted while paused. Resolution processes exactly one next session, even
   if the original advance target was years away.

Sells/covers cannot reserve the same shares twice. Risk-reducing fills do not
require initial-margin compliance, even when a cover increases the debit.
Opposing long/short positions cannot coexist in one instrument. A next-open gap
can reject a previously queued opening order.

Nonpositive closing equity terminates the game as bankrupt, preserving the
deficit and remaining marked positions without fabricated liquidation fills.
At the fixed final session, a surviving game completes at marked value; there
is no extra session beyond its end to cure a final-close margin shortfall.

## Market-data boundary and failure behavior

The constructor accepts only the `simulation_qualified`, USD, `as_traded`
manifest contract. It derives the content-addressed release ID and validates
calendar ordering, bar uniqueness, and declared instrument bounds. It copies
coverage into immutable records and exposes read-only bar/coverage mappings.
Every state pins that release ID and `us-equity-margin-v1`.

**The core itself is not qualification or revision verification.** The new
`game_market.py` reader resolves and hash-checks stored bar/action/calendar
revisions and their qualification reports before constructing a simulator.
The separate producer must still establish calendar completeness, currency,
price basis, corporate-action semantics, and terminal-event coverage.
Merely labeling data `simulation_qualified` is not proof. Synthetic test fixtures
are not a published market release. There is no loader from live Yahoo snapshots.

Every held instrument and pending order needs valid opening/closing bars for
the next session. Missing prices, non-decimal prices, invalid actions, and
unverified terminal events block that checkpoint. No forward-fill, future
substitution, or live provider fetch occurs. Earlier completed sessions remain
in the returned state; none of the failed session's costs/actions/fills commit.

Dividends use `dividend_per_prior_share`, explicitly expressed per prior-close
share. This matters when a split and dividend share an effective date; the
qualification adapter must establish that basis rather than guessing provider
semantics. Reverse splits that cannot preserve every FIFO lot at eight-decimal
quantity precision return `UNSUPPORTED_SPLIT_PRECISION`. Cash-in-lieu and
terminal/delisting settlements are not implemented, never invented.

## Validation

Use the Python 3.11 development environment described in the foundation guide:

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_simulation*.py' -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

Tests cover long/short FIFO gains/losses, notional rounding, next-open gaps,
reduction reservation/ordering, splits, dividend entitlement, weekend/leap-day
financing, margin resolution, bankruptcy, missing-data rollback, deterministic
replay, and daily-step/one-jump equivalence. Ledger balances and portfolio P&L
reconcile, including after fractional partial covers and corporate actions.

PostgreSQL integration is opt-in through `FINANCE_TEST_DATABASE_URL`, which
must name a disposable PostgreSQL database ending in `_test`. Those tests use
private random schemas and a temporary `NUMERIC(28,8)` posting table. They cover
migrations, immutable-record triggers, concurrent quota admission, and exact
quantized posting round-trips. The game PostgreSQL tests additionally exercise
concurrent commands, unique advance admission, fenced workers, and checkpoint rollback.

## Remaining work

Owned persistent models, public contracts, idempotent revision-checked commands,
scoped reads, and a fenced durable worker are now implemented in the game slice.
The pure simulator remains separate from storage and has no concurrency
control by itself. Full histories are retained in memory and in a bounded
checkpoint, with append-only database projections for audit/reporting.

The [Google identity slice](google-identity.md) now implements guest claims and
session recovery. Real source qualification/publication, Google application
setup, frontend integration, production rollout, and ranked challenges remain
separate work.
