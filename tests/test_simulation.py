import unittest
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN, localcontext

from finance_app_database_service.accounting import reconcile
from finance_app_database_service.contracts import ReleaseManifest
from finance_app_database_service.simulation import Bar, SimulationError, Simulator, shifted_date

D = Decimal
A, B = "ins_" + "a" * 32, "ins_" + "b" * 32
DAYS = tuple(date(2020, 1, day) for day in (2, 3, 6, 7, 8, 9, 10))


def market_manifest(dates=DAYS, shortable=True):
    data = {
        "schema_version": 1, "qualification": "simulation_qualified",
        "currency": "USD", "price_basis": "as_traded",
        "calendar_id": "XNYS", "calendar_version": "synthetic-test-calendar",
        "from_date": dates[0], "to_date": dates[-1],
        "instruments": [
            dict(instrument_id=instrument_id, from_date=dates[0], to_date=dates[-1],
                 bar_revision_id="a" * 64, action_revision_id="b" * 64,
                 source_snapshot_ids=["c" * 64], validation_report_sha256="d" * 64,
                 shortable=shortable)
            for instrument_id in (A, B)
        ],
    }
    return ReleaseManifest.parse_obj(data)


def simulator(prices=None, *, dates=DAYS, overrides=(), shortable=True, omit=()):
    bars = {}
    for instrument_id in (A, B):
        for index, day in enumerate(dates):
            price = (prices or ["100"] * len(dates))[index]
            opening, closing = price if isinstance(price, tuple) else (price, price)
            bars[(day, instrument_id)] = Bar(instrument_id, day, D(opening), D(closing))
    for bar in overrides:
        bars[(bar.date, bar.instrument_id)] = bar
    for key in omit:
        bars.pop(key)
    return Simulator(market_manifest(dates, shortable), tuple(dates), tuple(bars.values()))


class TestSessionSimulation(unittest.TestCase):
    def create(self, engine, cash="1000"):
        return engine.create(engine.sessions[0], engine.sessions[-1], D(cash))

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(SimulationError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_orders_execute_at_next_open_not_visible_close(self):
        engine = simulator(["100", ("110", "120"), "120", "120", "120", "120", "120"])
        state = engine.queue(self.create(engine), A, "buy", quantity=D("5"))
        self.assertEqual(state.fills, ())
        self.assertEqual(state.book.cash, D("1000"))
        result = engine.advance(state, DAYS[1])
        execution, = result.state.fills
        self.assertEqual((execution.date, execution.price, execution.quantity),
                         (DAYS[1], D("110"), D("5")))
        self.assertEqual(result.state.snapshots[-1].portfolio.equity, D("1050"))

    def test_gap_rejects_entire_order_without_consuming_cash_or_inventory(self):
        engine = simulator(["100", "110", "110", "110", "110", "110", "110"])
        queued = engine.queue(self.create(engine), A, "buy", quantity=D("20"))
        state = engine.advance(queued, DAYS[1]).state
        self.assertEqual(state.orders[0].reason, "INSUFFICIENT_BUYING_POWER")
        self.assertEqual(state.fills, ())
        self.assertEqual(state.book, queued.book)

    def test_notional_uses_actual_open_and_rounds_down(self):
        engine = simulator(["3"] * len(DAYS))
        state = engine.queue(self.create(engine), A, "buy", notional=D("100"))
        state = engine.advance(state, DAYS[1]).state
        execution, = state.fills
        self.assertEqual(execution.quantity, D("33.33333333"))
        self.assertLessEqual(execution.amount, D("100"))

    def test_subquantum_order_is_rejected_instead_of_zero_fill(self):
        engine = simulator()
        state = engine.queue(self.create(engine), A, "buy", notional=D("0.00000001"))
        state = engine.advance(state, DAYS[1]).state
        self.assertEqual(state.orders[0].reason, "QUANTITY_ROUNDS_TO_ZERO")
        self.assertEqual(state.fills, ())

    def test_oversized_converted_quantity_rejects_order_not_session(self):
        engine = simulator(["0.00000001"] * len(DAYS))
        initial = engine.queue(self.create(engine), A, "buy", notional=D("99999999999999999999"))
        state = engine.step(initial)
        self.assertEqual(state.cursor, DAYS[1])
        self.assertEqual(state.orders[0].reason, "ORDER_LIMIT_EXCEEDED")
        self.assertEqual(state.book, initial.book)

        engine = simulator()
        initial = engine.queue(self.create(engine), A, "buy", quantity=D("99999999999999999999"))
        state = engine.step(initial)
        self.assertEqual(state.orders[0].reason, "ORDER_LIMIT_EXCEEDED")
        self.assertEqual(state.book, initial.book)

    def test_reductions_execute_before_openings_and_acceptance_order_is_stable(self):
        engine = simulator()
        state = engine.queue(self.create(engine), A, "buy", quantity=D("10"))
        state = engine.advance(state, DAYS[1]).state
        state = engine.queue(state, B, "buy", quantity=D("20"))
        state = engine.queue(state, A, "sell", close_all=True)
        state = engine.advance(state, DAYS[2]).state
        self.assertEqual([fill.order_id for fill in state.fills], ["order_1", "order_3", "order_2"])
        self.assertEqual(state.book.quantity(A, "long"), D("0"))
        self.assertEqual(state.book.quantity(B, "long"), D("20"))

        state = engine.queue(self.create(engine), A, "buy", quantity=D("15"))
        state = engine.queue(state, B, "buy", quantity=D("10"))
        state = engine.advance(state, DAYS[1]).state
        self.assertEqual([order.status for order in state.orders], ["filled", "rejected"])

    def test_pending_reductions_reserve_shares_and_cancellation_releases_them(self):
        engine = simulator()
        state = engine.advance(engine.queue(self.create(engine), A, "buy", quantity=D("10")), DAYS[1]).state
        state = engine.queue(state, A, "sell", quantity=D("6"))
        self.assert_code("POSITION_ALREADY_RESERVED", engine.queue, state, A, "sell", quantity=D("5"))
        self.assert_code("POSITION_ALREADY_RESERVED", engine.queue, state, A, "sell", close_all=True)
        state = engine.cancel(state, "order_2")
        state = engine.queue(state, A, "sell", close_all=True)
        self.assertEqual(state.orders[-1].status, "pending")
        self.assert_code("ORDER_NOT_PENDING", engine.cancel, state, "order_2")

    def test_invalid_order_forms_and_non_shortable_listings_fail_closed(self):
        engine = simulator(shortable=False)
        state = self.create(engine)
        self.assert_code("INSTRUMENT_NOT_SHORTABLE", engine.queue, state, A, "sell_short", quantity=D("1"))
        self.assert_code("INSTRUMENT_NOT_ELIGIBLE", engine.queue, state, "unknown", "buy", quantity=D("1"))
        for values in ({}, {"quantity": D("1"), "notional": D("10")},
                       {"close_all": True}, {"quantity": D("1"), "close_all": 1}):
            self.assert_code("INVALID_ORDER", engine.queue, state, A, "buy", **values)
        for amount in (1.0, D("NaN"), D("-1"), D("0"), D("0.123456789")):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                engine.queue(state, A, "buy", quantity=amount)
        self.assert_code("INVALID_ORDER", engine.queue, state, A, "sell", notional=D("10"))

    def test_opposing_open_orders_cannot_cross_a_position(self):
        engine = simulator()
        state = engine.queue(self.create(engine), A, "buy", quantity=D("1"))
        state = engine.queue(state, A, "sell_short", quantity=D("1"))
        state = engine.advance(state, DAYS[1]).state
        self.assertEqual(state.orders[-1].reason, "OPPOSING_POSITION")
        self.assertEqual(len(state.book.lots), 1)

    def test_financing_uses_prior_close_and_actual_weekend_days(self):
        engine = simulator()
        state = engine.advance(engine.queue(self.create(engine), A, "buy", quantity=D("20")), DAYS[1]).state
        self.assertEqual(state.snapshots[-1].portfolio.margin_interest, D("0"))
        state = engine.advance(state, DAYS[2]).state
        self.assertEqual(state.snapshots[-1].portfolio.margin_interest, D("0.65753425"))

        state = engine.advance(engine.queue(self.create(engine), A, "sell_short", quantity=D("10")), DAYS[1]).state
        state = engine.advance(state, DAYS[2]).state
        mark = state.snapshots[-1].portfolio
        self.assertEqual(mark.margin_interest, D("0"))
        self.assertEqual(mark.borrow_fees, D("0.24657534"))

    def test_leap_day_financing_still_uses_fixed_365_denominator(self):
        dates = (date(2020, 2, 27), date(2020, 2, 28), date(2020, 3, 2), date(2020, 3, 3))
        engine = simulator(dates=dates)
        state = engine.advance(engine.queue(self.create(engine), A, "buy", quantity=D("20")), dates[2]).state
        self.assertEqual(state.snapshots[-1].portfolio.margin_interest, D("0.65753425"))

    def test_short_proceeds_do_not_offset_interest_on_long_debit(self):
        engine = simulator()
        state = engine.queue(self.create(engine, "2000"), A, "buy", quantity=D("30"))
        state = engine.queue(state, B, "sell_short", quantity=D("5"))
        state = engine.advance(state, DAYS[2]).state
        mark = state.snapshots[-1].portfolio
        self.assertEqual(mark.margin_interest, D("0.65753425"))
        self.assertEqual(mark.borrow_fees, D("0.12328767"))

    def test_dividend_entitlement_precedes_ex_date_sales_and_excludes_new_buys(self):
        dividend = Bar(A, DAYS[2], D("99"), D("99"), dividend_per_prior_share=D("1"))
        engine = simulator(overrides=(dividend,))
        state = engine.advance(engine.queue(self.create(engine), A, "buy", quantity=D("10")), DAYS[1]).state
        state = engine.queue(state, A, "sell", close_all=True)
        state = engine.advance(state, DAYS[2]).state
        self.assertEqual(state.snapshots[-1].portfolio.dividends, D("10"))
        self.assertEqual(state.book.cash, D("1000"))
        self.assertEqual(state.snapshots[-1].portfolio.realized_pnl, D("-10"))

        state = engine.advance(self.create(engine), DAYS[1]).state
        state = engine.queue(state, A, "buy", quantity=D("10"))
        state = engine.advance(state, DAYS[2]).state
        self.assertEqual(state.snapshots[-1].portfolio.dividends, D("0"))

    def test_short_dividend_is_owed_on_prior_close_shares(self):
        engine = simulator(overrides=(
            Bar(A, DAYS[2], D("99"), D("99"), dividend_per_prior_share=D("1")),
        ))
        state = engine.advance(engine.queue(self.create(engine), A, "sell_short", quantity=D("10")), DAYS[2]).state
        self.assertEqual(state.snapshots[-1].portfolio.dividends, D("-10"))
        self.assertEqual(state.book.restricted_proceeds, D("1000"))

    def test_split_preserves_basis_and_equity_and_adjusts_quantity_orders(self):
        engine = simulator(["100", "100", "50", "50", "50", "50", "50"], overrides=(
            Bar(A, DAYS[2], D("50"), D("50"), split_numerator=D("2")),
        ))
        state = engine.advance(engine.queue(self.create(engine), A, "buy", quantity=D("5")), DAYS[1]).state
        state = engine.queue(state, A, "buy", quantity=D("1"))
        state = engine.advance(state, DAYS[2]).state
        self.assertEqual(state.book.quantity(A, "long"), D("12"))
        self.assertEqual(state.book.balance("long_inventory"), D("600"))
        self.assertEqual(state.snapshots[-1].portfolio.equity, D("1000"))
        self.assertEqual(state.fills[-1].quantity, D("2"))
        self.assertEqual(state.orders[-1].quantity, D("2"))

    def test_split_adjusts_reserved_reductions_but_not_notional_intent(self):
        split = Bar(A, DAYS[2], D("50"), D("50"), split_numerator=D("2"))
        engine = simulator(overrides=(split,))
        state = engine.advance(engine.queue(self.create(engine), A, "buy", quantity=D("5")), DAYS[1]).state
        state = engine.queue(state, A, "sell", quantity=D("3"))
        state = engine.advance(state, DAYS[2]).state
        self.assertEqual(state.fills[-1].quantity, D("6"))
        self.assertEqual(state.book.quantity(A, "long"), D("4"))

        state = engine.advance(self.create(engine), DAYS[1]).state
        state = engine.queue(state, A, "buy", notional=D("100"))
        state = engine.advance(state, DAYS[2]).state
        self.assertEqual(state.fills[-1].quantity, D("2"))
        self.assertEqual(state.fills[-1].amount, D("100"))

    def test_same_day_split_dividend_has_explicit_prior_share_basis(self):
        engine = simulator(overrides=(
            Bar(A, DAYS[2], D("49.5"), D("49.5"), split_numerator=D("2"),
                dividend_per_prior_share=D("1")),
        ))
        state = engine.advance(engine.queue(self.create(engine), A, "buy", quantity=D("10")), DAYS[2]).state
        self.assertEqual(state.book.quantity(A, "long"), D("20"))
        self.assertEqual(state.snapshots[-1].portfolio.dividends, D("10"))
        self.assertEqual(state.snapshots[-1].portfolio.equity, D("1000"))

    def test_short_reverse_split_preserves_proceeds_and_has_no_action_pnl(self):
        engine = simulator(overrides=(
            Bar(A, DAYS[2], D("200"), D("200"), split_denominator=D("2")),
        ))
        state = engine.advance(engine.queue(self.create(engine), A, "sell_short", quantity=D("10")), DAYS[2]).state
        self.assertEqual(state.book.quantity(A, "short"), D("5"))
        self.assertEqual(state.book.restricted_proceeds, D("1000"))
        self.assertEqual(state.snapshots[-1].portfolio.unrealized_pnl, D("0"))
        self.assertEqual(state.snapshots[-1].portfolio.equity, D("999.75342466"))

    def test_unrepresentable_reverse_split_blocks_without_losing_shares(self):
        engine = simulator(overrides=(
            Bar(A, DAYS[2], D("300"), D("300"), split_denominator=D("3"),
                dividend_per_prior_share=D("1")),
        ))
        original = engine.advance(engine.queue(self.create(engine), A, "buy", quantity=D("1")), DAYS[1]).state
        result = engine.advance(original, DAYS[3])
        self.assertEqual(result.outcome, "blocked_data")
        self.assertEqual(result.code, "UNSUPPORTED_SPLIT_PRECISION")
        self.assertEqual(result.state, original)

    def test_advance_stops_at_first_margin_call_and_resolution_is_one_session(self):
        engine = simulator(["100", ("100", "60"), "70", "80", "90", "100", "100"])
        queued = engine.queue(self.create(engine), A, "buy", quantity=D("20"))
        result = engine.advance(queued, DAYS[-1])
        self.assertEqual(result.outcome, "interrupted_margin")
        self.assertEqual(result.requested_target, DAYS[-1])
        self.assertEqual(result.state.cursor, DAYS[1])
        self.assertEqual(result.state.snapshots[-1].portfolio.maintenance_shortfall, D("160"))
        self.assert_code("MARGIN_CALL_ACTIVE", engine.queue, result.state, B, "buy", quantity=D("1"))
        self.assert_code("MARGIN_CALL_ACTIVE", engine.advance, result.state, DAYS[-1])
        resolved = engine.resolve_margin(result.state, liquidate_all=True)
        self.assertEqual(resolved.state.cursor, DAYS[2])
        self.assertEqual(resolved.state.status, "ready")
        self.assertEqual(resolved.state.book.lots, ())
        self.assertEqual(resolved.state.fills[-1].price, D("70"))

    def test_multi_year_target_still_interrupts_at_first_breaching_close(self):
        dates = tuple(
            DAYS[0] + timedelta(days=index) for index in range(735)
            if (DAYS[0] + timedelta(days=index)).weekday() < 5
        )
        engine = simulator(dates=dates, overrides=(
            Bar(A, dates[1], D("100"), D("60")),
        ))
        state = engine.queue(self.create(engine), A, "buy", quantity=D("20"))
        result = engine.advance(state, dates[-1])
        self.assertEqual(result.outcome, "interrupted_margin")
        self.assertEqual(result.state.cursor, dates[1])
        self.assertEqual(len(result.state.snapshots), 2)
        self.assertEqual(result.requested_target, dates[-1])

    def test_unsolved_margin_call_remains_paused_after_one_session(self):
        engine = simulator(["100", ("100", "60"), "60", "60", "60", "60", "60"])
        state = engine.advance(engine.queue(self.create(engine), A, "buy", quantity=D("20")), DAYS[-1]).state
        result = engine.resolve_margin(state)
        self.assertEqual(result.state.cursor, DAYS[2])
        self.assertEqual(result.outcome, "interrupted_margin")

    def test_cover_that_increases_debit_is_allowed_during_margin_stress(self):
        engine = simulator(["100", ("100", "190"), "230", "230", "230", "230", "230"])
        state = engine.advance(engine.queue(self.create(engine), A, "sell_short", quantity=D("10")), DAYS[-1]).state
        state = engine.queue(state, A, "buy_to_cover", quantity=D("9"))
        result = engine.resolve_margin(state)
        self.assertEqual(result.state.fills[-1].action, "buy_to_cover")
        self.assertEqual(result.state.fills[-1].quantity, D("9"))
        self.assertGreater(result.state.snapshots[-1].portfolio.debit_balance, D("0"))
        self.assertEqual(result.state.status, "bankrupt")
        self.assertEqual(result.state.book.quantity(A, "short"), D("1"))

    def test_bankruptcy_records_deficit_without_fabricated_liquidation(self):
        engine = simulator(["100", ("100", "40"), "40", "40", "40", "40", "40"])
        result = engine.advance(engine.queue(self.create(engine), A, "buy", quantity=D("20")), DAYS[-1])
        self.assertEqual(result.state.status, "bankrupt")
        self.assertEqual(result.state.cursor, DAYS[1])
        self.assertEqual(result.state.book.quantity(A, "long"), D("20"))
        self.assertEqual(len(result.state.fills), 1)
        self.assertEqual(result.state.snapshots[-1].portfolio.equity, D("-200"))

    def test_final_session_marks_positions_without_forced_sale(self):
        engine = simulator()
        state = engine.advance(engine.queue(self.create(engine), A, "buy", quantity=D("1")), DAYS[-1]).state
        self.assertEqual(state.status, "completed")
        self.assertEqual(len(state.fills), 1)
        self.assertEqual(state.book.quantity(A, "long"), D("1"))
        self.assert_code("GAME_NOT_TRADABLE", engine.queue, state, A, "sell", close_all=True)

    def test_data_gap_rolls_back_whole_day_but_retains_prior_checkpoints(self):
        engine = simulator(omit=((DAYS[2], A),))
        initial = engine.queue(self.create(engine), A, "buy", quantity=D("20"))
        result = engine.advance(initial, DAYS[-1])
        self.assertEqual((result.outcome, result.code), ("blocked_data", "MARKET_DATA_UNAVAILABLE"))
        self.assertEqual(result.state.cursor, DAYS[1])
        self.assertEqual(result.state.snapshots[-1].portfolio.margin_interest, D("0"))
        self.assertEqual(initial.book.cash, D("1000"))
        self.assertEqual(len(result.state.fills), 1)
        fresh = simulator()
        resumed = fresh.advance(result.state, DAYS[-1]).state
        expected = fresh.advance(fresh.queue(self.create(fresh), A, "buy", quantity=D("20")), DAYS[-1]).state
        self.assertEqual(resumed, expected)

    def test_bad_data_cannot_commit_financing_dividends_or_splits(self):
        for bar, code in (
            (Bar(A, DAYS[2], None, D("100")), "INVALID_MARKET_DATA"),
            (Bar(A, DAYS[2], D("100"), 100.0), "INVALID_MARKET_DATA"),
            (Bar(A, DAYS[2], D("100"), D("100"), dividend_per_prior_share=D("NaN")), "INVALID_MARKET_DATA"),
            (Bar(A, DAYS[2], D("100"), D("100"), unverified_terminal_event=True), "UNVERIFIED_TERMINAL_EVENT"),
        ):
            with self.subTest(bar=bar):
                engine = simulator(overrides=(bar,))
                state = engine.advance(engine.queue(self.create(engine), A, "buy", quantity=D("20")), DAYS[1]).state
                result = engine.advance(state, DAYS[3])
                self.assertEqual(result.state, state)
                self.assertEqual(result.code, code)

    def test_failed_liquidation_resolution_does_not_leave_queued_commands(self):
        engine = simulator(["100", ("100", "60"), "60", "60", "60", "60", "60"],
                           omit=((DAYS[2], A),))
        state = engine.advance(engine.queue(self.create(engine), A, "buy", quantity=D("20")), DAYS[-1]).state
        result = engine.resolve_margin(state, liquidate_all=True)
        self.assertEqual(result.state, state)
        self.assertEqual(result.code, "MARKET_DATA_UNAVAILABLE")

    def test_calendar_and_market_mappings_cannot_be_mutated_in_place(self):
        engine = simulator()
        with self.assertRaises(TypeError):
            engine.bars[(DAYS[1], A)] = Bar(A, DAYS[1], D("1"), D("1"))
        with self.assertRaises(TypeError):
            engine.coverage[A] = engine.coverage[B]
        with self.assertRaises(ValueError):
            Simulator(ReleaseManifest.parse_obj({
                "qualification": "source_only",
            }), DAYS, ())

    def test_invalid_calendars_and_duplicate_or_out_of_coverage_bars_fail_closed(self):
        manifest = market_manifest()
        for sessions in ((), DAYS[::-1], DAYS + (DAYS[-1],),
                         (DAYS[0] - timedelta(days=1),) + DAYS):
            with self.subTest(sessions=sessions), self.assertRaises(ValueError):
                Simulator(manifest, sessions, ())
        bar = Bar(A, DAYS[1], D("100"), D("100"))
        for bars in ((bar, bar),
                     (replace(bar, instrument_id="ins_" + "e" * 32),),
                     (replace(bar, date=DAYS[-1] + timedelta(days=1)),)):
            with self.subTest(bars=bars), self.assertRaises(ValueError):
                Simulator(manifest, DAYS, bars)
        manifest.instruments[0].from_date = DAYS[2]
        with self.assertRaises(ValueError):
            Simulator(manifest, DAYS, (bar,))

    def test_replay_daily_steps_and_one_jump_are_identical_and_reconcile(self):
        engine = simulator(overrides=(
            Bar(A, DAYS[3], D("50"), D("50"), split_numerator=D("2"), dividend_per_prior_share=D("1")),
        ))
        initial = engine.queue(self.create(engine, "2000"), A, "buy", quantity=D("10"))
        initial = engine.queue(initial, B, "sell_short", notional=D("500"))
        jumped = engine.advance(initial, DAYS[-1]).state
        stepped = initial
        for day in DAYS[1:]:
            stepped = engine.advance(stepped, day).state
            reconcile(stepped.book)
            mark = stepped.snapshots[-1].portfolio
            self.assertEqual(
                mark.equity - stepped.initial_cash,
                mark.realized_pnl + mark.unrealized_pnl + mark.dividends - mark.margin_interest - mark.borrow_fees,
            )
        self.assertEqual(jumped, stepped)
        self.assertEqual(engine.advance(initial, DAYS[-1]).state, jumped)
        with localcontext() as context:
            context.prec, context.rounding = 5, ROUND_DOWN
            self.assertEqual(engine.advance(initial, DAYS[-1]).state, jumped)

    def test_pinned_release_and_ruleset_mismatch_are_rejected(self):
        engine = simulator()
        state = self.create(engine)
        for changed in (replace(state, release_id="rel_other"), replace(state, ruleset_id="other")):
            self.assert_code("PINNED_INPUT_MISMATCH", engine.advance, changed, DAYS[1])
            self.assert_code("PINNED_INPUT_MISMATCH", engine.queue, changed, A, "buy", quantity=D("1"))

    def test_target_dates_resolve_backward_without_exceeding_target(self):
        engine = simulator()
        state = self.create(engine)
        result = engine.advance(state, date(2020, 1, 5))
        self.assertEqual(result.effective_target, DAYS[1])
        self.assertEqual(result.state.cursor, DAYS[1])
        for target in (DAYS[0], DAYS[-1] + timedelta(days=1)):
            self.assert_code("INVALID_ADVANCE_TARGET", engine.advance, state, target)
        self.assert_code("INVALID_ADVANCE_TARGET", engine.advance, result.state, date(2020, 1, 5))

    def test_calendar_month_year_clamping_and_advance_limit(self):
        self.assertEqual(shifted_date(date(2020, 1, 31), 1, "months"), date(2020, 2, 29))
        self.assertEqual(shifted_date(date(2020, 2, 29), 1, "years"), date(2021, 2, 28))
        self.assertEqual(shifted_date(DAYS[0], 1, "weeks"), DAYS[5])
        for count, unit in ((0, "days"), (-1, "days"), (True, "days"), (1, "unknown"), (10**10, "years")):
            self.assert_code("INVALID_ADVANCE", shifted_date, DAYS[0], count, unit)
        engine = simulator(dates=(date(2000, 1, 3), date(2000, 1, 4), date(2020, 1, 3)))
        self.assert_code("ADVANCE_LIMIT_EXCEEDED", engine.advance, self.create(engine), date(2020, 1, 3))
