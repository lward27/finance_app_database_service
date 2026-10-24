import unittest
from decimal import Decimal, ROUND_UP, localcontext

from finance_app_database_service.accounting import (
    Posting, decimal_calculation, fill, funded_book, post, positive, reconcile, value_book,
)

D = Decimal


class TestSimulationAccounting(unittest.TestCase):
    def test_initial_contribution_and_every_entry_balance(self):
        book = funded_book(D("1000"))
        self.assertEqual(book.cash, D("1000.00000000"))
        self.assertEqual(book.balance("contributed_capital"), D("-1000"))
        self.assertEqual(sum(p.amount for p in book.ledger[0].postings), D("0"))
        reconcile(book)

    def test_long_gains_losses_and_fifo_partial_cost_release(self):
        book = funded_book(D("1000"))
        book = fill(book, "buy1", "A", "buy", D("3"), D("10"))
        book = fill(book, "buy2", "A", "buy", D("2"), D("20"))
        book = fill(book, "sell1", "A", "sell", D("4"), D("15"))
        self.assertEqual(book.cash, D("990"))
        self.assertEqual(book.balance("realized_pnl"), D("-10"))
        lot, = book.lots
        self.assertEqual((lot.id, lot.quantity, lot.basis), ("buy2", D("1"), D("20")))
        valuation = value_book(book, {"A": D("12")})
        self.assertEqual(valuation.realized_pnl, D("10"))
        self.assertEqual(valuation.unrealized_pnl, D("-8"))
        self.assertEqual(valuation.equity, D("1002"))
        book = fill(book, "sell2", "A", "sell", D("1"), D("5"))
        self.assertEqual(book.lots, ())
        self.assertEqual(value_book(book, {}).realized_pnl, D("-5"))
        self.assertEqual(book.cash, D("995"))
        reconcile(book)

    def test_short_fifo_proceeds_and_cover_gains_losses(self):
        book = fill(funded_book(D("1000")), "short", "A", "sell_short", D("10"), D("10"))
        self.assertEqual(book.cash, D("1100"))
        self.assertEqual(book.restricted_proceeds, D("100"))
        book = fill(book, "cover1", "A", "buy_to_cover", D("4"), D("5"))
        self.assertEqual(book.restricted_proceeds, D("60"))
        mark = value_book(book, {"A": D("5")})
        self.assertEqual(mark.realized_pnl, D("20"))
        self.assertEqual(mark.unrealized_pnl, D("30"))
        book = fill(book, "cover2", "A", "buy_to_cover", D("6"), D("20"))
        self.assertEqual(book.restricted_proceeds, D("0"))
        self.assertEqual(book.cash, D("960"))
        self.assertEqual(value_book(book, {}).realized_pnl, D("-40"))
        reconcile(book)

    def test_restricted_short_proceeds_never_hide_margin_debit(self):
        book = fill(funded_book(D("2000")), "buy", "A", "buy", D("30"), D("100"))
        book = fill(book, "short", "B", "sell_short", D("5"), D("100"))
        mark = value_book(book, {"A": D("100"), "B": D("100")})
        self.assertEqual(mark.cash, D("-500"))
        self.assertEqual(mark.restricted_proceeds, D("500"))
        self.assertEqual(mark.debit_balance, D("1000"))
        self.assertEqual(mark.equity, D("2000"))
        self.assertEqual(mark.gross_exposure, D("3500"))

    def test_short_cover_crosses_fifo_lots_at_their_original_proceeds(self):
        book = funded_book(D("1000"))
        book = fill(book, "first", "A", "sell_short", D("3"), D("10"))
        book = fill(book, "second", "A", "sell_short", D("2"), D("20"))
        book = fill(book, "cover", "A", "buy_to_cover", D("4"), D("15"))
        lot, = book.lots
        self.assertEqual((lot.id, lot.quantity, lot.basis), ("second", D("1"), D("20")))
        self.assertEqual(book.restricted_proceeds, D("20"))
        self.assertEqual(value_book(book, {"A": D("12")}).realized_pnl, D("-10"))
        reconcile(book)

    def test_fractional_basis_remainder_is_released_on_final_cover(self):
        book = fill(funded_book(D("100")), "short", "A", "sell_short", D("3"), D("0.33333333"))
        for index in range(3):
            book = fill(book, f"cover{index}", "A", "buy_to_cover", D("1"), D("0.2"))
            reconcile(book)
        self.assertEqual(book.lots, ())
        self.assertEqual(book.restricted_proceeds, D("0"))
        self.assertEqual(book.balance("short_inventory"), D("0"))
        self.assertEqual(book.cash, D("100.39999999"))

    def test_valuation_rounds_per_position_not_per_fill(self):
        one = fill(funded_book(D("10")), "one", "A", "buy", D("2"), D("1"))
        two = fill(funded_book(D("10")), "first", "A", "buy", D("1"), D("1"))
        two = fill(two, "second", "A", "buy", D("1"), D("1"))
        self.assertEqual(value_book(one, {"A": D("1.00000001")}),
                         value_book(two, {"A": D("1.00000001")}))

    def test_postings_reject_unbalanced_or_duplicate_transactions(self):
        book = funded_book(D("100"))
        for key, amounts in (
            ("bad", dict(cash=D("1"), contributed_capital=D("-2"))),
            ("initial_contribution", dict(cash=D("1"), contributed_capital=D("-1"))),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                post(book, key, **amounts)
        self.assertEqual(book.cash, D("100"))

    def test_invalid_fill_inputs_and_crossing_are_rejected(self):
        book = fill(funded_book(D("100")), "buy", "A", "buy", D("1"), D("10"))
        for action, quantity, price in (
            ("unknown", D("1"), D("1")), ("sell", D("2"), D("1")),
            ("sell_short", D("1"), D("1")), ("buy", 1.0, D("1")),
            ("buy", D("1"), D("NaN")), ("buy", D("0.00000001"), D("0.00000001")),
        ):
            with self.subTest(action=action, quantity=quantity), self.assertRaises(ValueError):
                fill(book, "invalid", "A", action, quantity, price)
        for amount in (D("0"), D("-1"), D("1e20"), D("1.000000001"), D("Infinity"), 1.0):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                positive(amount)

    def test_reconciliation_detects_tampered_inventory_and_postings(self):
        from dataclasses import replace
        book = fill(funded_book(D("100")), "buy", "A", "buy", D("1"), D("10"))
        bad_lot = replace(book.lots[0], basis=D("9"))
        with self.assertRaises(ValueError):
            reconcile(replace(book, lots=(bad_lot,)))
        entry = replace(book.ledger[0], postings=(Posting("cash", D("99")),))
        with self.assertRaises(ValueError):
            reconcile(replace(book, ledger=(entry,) + book.ledger[1:]))

    def test_caller_decimal_context_does_not_change_books_or_valuation(self):
        @decimal_calculation
        def calculate():
            book = fill(funded_book(D("10000")), "buy", "A", "buy", D("12.12345678"), D("13.87654321"))
            return book, value_book(book, {"A": D("14.12345678")})

        expected = calculate()
        with localcontext() as context:
            context.prec, context.rounding = 4, ROUND_UP
            self.assertEqual(calculate(), expected)
