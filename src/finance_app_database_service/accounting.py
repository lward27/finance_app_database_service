"""Immutable FIFO books and balanced postings for the fixed v1 game rules.

Positive postings are debits. Inventory is carried at cost, including a credit
balance for shorts. Market values belong in snapshots, not in the cost ledger.
No database, clock, provider, or random identifier is consulted here.
"""

from dataclasses import dataclass, replace
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from functools import wraps
from typing import Tuple

from finance_app_database_service.financial import money

ZERO = Decimal("0.00000000")
INITIAL_MARGIN = Decimal("0.50")
MAINTENANCE_MARGIN = Decimal("0.30")


def decimal_calculation(function):
    """Make arithmetic independent of the caller's thread-local Decimal policy."""
    @wraps(function)
    def calculate(*args, **kwargs):
        with localcontext(Context(prec=60, rounding=ROUND_HALF_EVEN)):
            return function(*args, **kwargs)
    return calculate


@decimal_calculation
def positive(value: Decimal) -> Decimal:
    if (
        not isinstance(value, Decimal) or not value.is_finite() or value <= 0
        or value >= Decimal("1e20") or value != money(value)
    ):
        raise ValueError("A positive Decimal with at most eight places is required")
    return money(value)


@dataclass(frozen=True)
class Posting:
    account: str
    amount: Decimal


@dataclass(frozen=True)
class LedgerEntry:
    key: str
    postings: Tuple[Posting, ...]


@dataclass(frozen=True)
class Lot:
    id: str
    instrument_id: str
    side: str
    quantity: Decimal
    basis: Decimal


@dataclass(frozen=True)
class Book:
    lots: Tuple[Lot, ...] = ()
    ledger: Tuple[LedgerEntry, ...] = ()

    @decimal_calculation
    def balance(self, account):
        return money(sum(
            (p.amount for entry in self.ledger for p in entry.postings
             if p.account == account), ZERO,
        ))

    @property
    def cash(self):
        return self.balance("cash")

    @property
    @decimal_calculation
    def restricted_proceeds(self):
        return money(sum((lot.basis for lot in self.lots if lot.side == "short"), ZERO))

    @decimal_calculation
    def quantity(self, instrument_id, side):
        return money(sum(
            (lot.quantity for lot in self.lots
             if lot.instrument_id == instrument_id and lot.side == side), ZERO,
        ))


@decimal_calculation
def post(book: Book, key: str, **amounts: Decimal) -> Book:
    if not key or any(entry.key == key for entry in book.ledger):
        raise ValueError("Ledger keys must be nonempty and unique")
    postings = tuple(Posting(account, money(value)) for account, value in amounts.items())
    if sum((p.amount for p in postings), ZERO) != ZERO:
        raise ValueError("Ledger entry must balance")
    return replace(book, ledger=book.ledger + (LedgerEntry(key, postings),))


@decimal_calculation
def funded_book(initial_cash: Decimal) -> Book:
    amount = positive(initial_cash)
    return post(Book(), "initial_contribution", cash=amount, contributed_capital=-amount)


@decimal_calculation
def fill(book: Book, key: str, instrument_id: str, action: str,
         quantity: Decimal, price: Decimal) -> Book:
    """Apply one already authorized fill; reductions consume cost/proceeds FIFO."""
    quantity, price = positive(quantity), positive(price)
    amount = money(quantity * price)
    if amount == ZERO:
        raise ValueError("A fill must have a nonzero monetary value")
    if action in ("buy", "sell_short"):
        side = "long" if action == "buy" else "short"
        opposite = "short" if side == "long" else "long"
        if book.quantity(instrument_id, opposite):
            raise ValueError("A fill cannot cross an opposing position")
        lot = Lot(key, instrument_id, side, quantity, amount)
        if side == "long":
            result = post(book, key, cash=-amount, long_inventory=amount)
        else:
            result = post(book, key, cash=amount, short_inventory=-amount)
        return replace(result, lots=book.lots + (lot,))
    if action not in ("sell", "buy_to_cover"):
        raise ValueError("Unknown order action")
    side = "long" if action == "sell" else "short"
    if quantity > book.quantity(instrument_id, side):
        raise ValueError("A reduction cannot exceed the position")
    remaining, basis, lots = quantity, ZERO, []
    for lot in book.lots:
        if lot.instrument_id != instrument_id or lot.side != side or remaining == ZERO:
            lots.append(lot)
            continue
        taken = min(remaining, lot.quantity)
        # Taking the remainder releases every last quantum of the original cost.
        used_basis = lot.basis if taken == lot.quantity else money(lot.basis * taken / lot.quantity)
        basis += used_basis
        remaining -= taken
        if taken < lot.quantity:
            lots.append(replace(lot, quantity=lot.quantity - taken, basis=lot.basis - used_basis))
    if action == "sell":
        result = post(book, key, cash=amount, long_inventory=-basis,
                      realized_pnl=-(amount - basis))
    else:
        result = post(book, key, cash=-amount, short_inventory=basis,
                      realized_pnl=-(basis - amount))
    return replace(result, lots=tuple(lots))


@dataclass(frozen=True)
class Valuation:
    cash: Decimal
    restricted_proceeds: Decimal
    free_cash: Decimal
    debit_balance: Decimal
    long_value: Decimal
    short_liability: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    equity: Decimal
    initial_requirement: Decimal
    maintenance_requirement: Decimal
    maintenance_shortfall: Decimal
    buying_power: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    dividends: Decimal
    margin_interest: Decimal
    borrow_fees: Decimal


@decimal_calculation
def value_book(book: Book, marks) -> Valuation:
    """Value every held listing at its exact session mark, never forward-fill."""
    long_value, short_value = ZERO, ZERO
    # Round at the position level, independent of how fills were split into lots.
    instruments = sorted({lot.instrument_id for lot in book.lots})
    for instrument_id in instruments:
        price = positive(marks[instrument_id])
        long_value += money(book.quantity(instrument_id, "long") * price)
        short_value += money(book.quantity(instrument_id, "short") * price)
    cash, restricted = book.cash, book.restricted_proceeds
    available = money(cash - restricted)
    equity = money(cash + long_value - short_value)
    gross = money(long_value + short_value)
    initial, maintenance = money(gross * INITIAL_MARGIN), money(gross * MAINTENANCE_MARGIN)
    long_basis = money(sum((lot.basis for lot in book.lots if lot.side == "long"), ZERO))
    return Valuation(
        cash=cash, restricted_proceeds=restricted, free_cash=max(available, ZERO),
        debit_balance=max(-available, ZERO), long_value=money(long_value),
        short_liability=money(short_value), gross_exposure=gross,
        net_exposure=money(long_value - short_value), equity=equity,
        initial_requirement=initial, maintenance_requirement=maintenance,
        maintenance_shortfall=max(money(maintenance - equity), ZERO),
        buying_power=max(money((equity - initial) / INITIAL_MARGIN), ZERO),
        realized_pnl=-book.balance("realized_pnl"),
        unrealized_pnl=money(long_value - long_basis + restricted - short_value),
        dividends=-book.balance("dividend_income"),
        margin_interest=book.balance("margin_interest_expense"),
        borrow_fees=book.balance("borrow_fee_expense"),
    )


@decimal_calculation
def reconcile(book: Book) -> None:
    """Fail closed if a checkpoint's cost ledger and FIFO inventory disagree."""
    for entry in book.ledger:
        if sum((p.amount for p in entry.postings), ZERO) != ZERO:
            raise ValueError("Unbalanced ledger")
        for posting in entry.postings:
            if posting.amount != money(posting.amount):
                raise ValueError("Unquantized posting")
    if len({entry.key for entry in book.ledger}) != len(book.ledger):
        raise ValueError("Duplicate ledger key")
    for lot in book.lots:
        positive(lot.quantity)
        if lot.side not in ("long", "short") or lot.basis < ZERO or lot.basis != money(lot.basis):
            raise ValueError("Invalid inventory lot")
    longs = sum((lot.basis for lot in book.lots if lot.side == "long"), ZERO)
    if book.balance("long_inventory") != longs:
        raise ValueError("Long inventory does not reconcile")
    if book.balance("short_inventory") != -book.restricted_proceeds:
        raise ValueError("Restricted short proceeds do not reconcile")
    for instrument_id in {lot.instrument_id for lot in book.lots}:
        if book.quantity(instrument_id, "long") and book.quantity(instrument_id, "short"):
            raise ValueError("Opposing positions")
