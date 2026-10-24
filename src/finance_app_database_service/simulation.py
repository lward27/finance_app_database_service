"""Pure, session-checkpointed simulator for ``us-equity-margin-v1``.

Inputs must already be qualified, immutable, as-traded revisions. This module
does not qualify Yahoo data or publish releases. Returned checkpoints are values,
not durable commits; advance_worker persists each checkpoint atomically.
"""

from bisect import bisect_right
from calendar import monthrange
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal, DecimalException
from types import MappingProxyType
from typing import Optional, Tuple

from finance_app_database_service.accounting import (
    ZERO, Book, Valuation, decimal_calculation, fill, funded_book, positive,
    post, reconcile, value_book,
)
from finance_app_database_service.contracts import ReleaseManifest, canonical_json, content_digest
from finance_app_database_service.financial import money, notional_quantity

RULESET_ID = "us-equity-margin-v1"
OPENING = ("buy", "sell_short")
REDUCING = ("sell", "buy_to_cover")
ONE = Decimal("1")


class SimulationError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Bar:
    instrument_id: str
    date: date
    open: Optional[Decimal]
    close: Optional[Decimal]
    split_numerator: Decimal = ONE
    split_denominator: Decimal = ONE
    # Qualification must express this on the *prior-close share* basis, even
    # when a split and ex-dividend date coincide. No provider inference here.
    dividend_per_prior_share: Decimal = ZERO
    unverified_terminal_event: bool = False


@dataclass(frozen=True)
class Coverage:
    from_date: date
    to_date: date
    shortable: bool


@dataclass(frozen=True)
class Order:
    id: str
    instrument_id: str
    action: str
    quantity: Optional[Decimal] = None
    notional: Optional[Decimal] = None
    close_all: bool = False
    status: str = "pending"
    reason: Optional[str] = None


@dataclass(frozen=True)
class Fill:
    order_id: str
    instrument_id: str
    action: str
    date: date
    quantity: Decimal
    price: Decimal
    amount: Decimal


@dataclass(frozen=True)
class PositionSnapshot:
    instrument_id: str
    side: str
    quantity: Decimal
    basis: Decimal
    price: Decimal


@dataclass(frozen=True)
class Snapshot:
    date: date
    portfolio: Valuation
    positions: Tuple[PositionSnapshot, ...]


@dataclass(frozen=True)
class Event:
    sequence: int
    date: date
    kind: str
    reference: str = ""


@dataclass(frozen=True)
class State:
    release_id: str
    ruleset_id: str
    initial_cash: Decimal
    start_date: date
    end_date: date
    cursor: date
    book: Book
    status: str = "ready"
    orders: Tuple[Order, ...] = ()
    fills: Tuple[Fill, ...] = ()
    snapshots: Tuple[Snapshot, ...] = ()
    events: Tuple[Event, ...] = ()


@dataclass(frozen=True)
class AdvanceResult:
    state: State
    requested_target: date
    effective_target: date
    outcome: str
    code: Optional[str] = None


def event(state, kind, reference=""):
    item = Event(len(state.events) + 1, state.cursor, kind, reference)
    return replace(state, events=state.events + (item,))


def shifted_date(cursor: date, count: int, unit: str) -> date:
    """Calendar arithmetic, clamping month/year targets rather than overflowing."""
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        raise SimulationError("INVALID_ADVANCE")
    try:
        if unit in ("days", "weeks"):
            return cursor + timedelta(days=count * (7 if unit == "weeks" else 1))
        if unit not in ("months", "years"):
            raise SimulationError("INVALID_ADVANCE")
        months = cursor.year * 12 + cursor.month - 1 + count * (12 if unit == "years" else 1)
        year, month = divmod(months, 12)
        return date(year, month + 1, min(cursor.day, monthrange(year, month + 1)[1]))
    except (OverflowError, ValueError):
        raise SimulationError("INVALID_ADVANCE") from None


class Simulator:
    def __init__(self, manifest: ReleaseManifest, sessions: Tuple[date, ...],
                 bars: Tuple[Bar, ...]):
        # Copy the mutable contract before deriving its content identity.
        manifest = ReleaseManifest.parse_raw(manifest.json())
        self.release_id = "rel_" + content_digest(canonical_json(manifest))
        self.sessions = tuple(sessions)
        if (
            not self.sessions or tuple(sorted(set(self.sessions))) != self.sessions
            or self.sessions[0] < manifest.from_date or self.sessions[-1] > manifest.to_date
        ):
            raise ValueError("A sorted, unique calendar within the release is required")
        self.coverage = MappingProxyType({
            item.instrument_id: Coverage(item.from_date, item.to_date, item.shortable)
            for item in manifest.instruments
        })
        indexed_bars = {}
        for bar in bars:
            key = (bar.date, bar.instrument_id)
            if key in indexed_bars or bar.date not in self.sessions or bar.instrument_id not in self.coverage:
                raise ValueError("Duplicate or out-of-release bar")
            coverage = self.coverage[bar.instrument_id]
            if not coverage.from_date <= bar.date <= coverage.to_date:
                raise ValueError("Bar exceeds instrument coverage")
            indexed_bars[key] = bar
        self.bars = MappingProxyType(indexed_bars)

    def _check_state(self, state):
        if state.release_id != self.release_id or state.ruleset_id != RULESET_ID:
            raise SimulationError("PINNED_INPUT_MISMATCH")

    @decimal_calculation
    def create(self, start: date, end: date, initial_cash: Decimal) -> State:
        # API date normalization is deliberately outside the accounting core.
        if start not in self.sessions or end not in self.sessions or start >= end:
            raise SimulationError("INVALID_GAME_DATES")
        amount = positive(initial_cash)
        book = funded_book(amount)
        initial = Snapshot(start, value_book(book, {}), ())
        state = State(self.release_id, RULESET_ID, amount, start, end, start, book,
                      snapshots=(initial,))
        return event(state, "game_created")

    @decimal_calculation
    def queue(self, state: State, instrument_id: str, action: str, *,
              quantity: Optional[Decimal] = None, notional: Optional[Decimal] = None,
              close_all: bool = False) -> State:
        self._check_state(state)
        if state.status not in ("ready", "margin_call"):
            raise SimulationError("GAME_NOT_TRADABLE")
        if action not in OPENING + REDUCING or not isinstance(close_all, bool):
            raise SimulationError("INVALID_ORDER")
        if state.status == "margin_call" and action in OPENING:
            raise SimulationError("MARGIN_CALL_ACTIVE")
        coverage = self.coverage.get(instrument_id)
        if not coverage or not coverage.from_date <= state.cursor <= coverage.to_date:
            raise SimulationError("INSTRUMENT_NOT_ELIGIBLE")
        if action == "sell_short" and not coverage.shortable:
            raise SimulationError("INSTRUMENT_NOT_SHORTABLE")
        if (quantity is not None) + (notional is not None) + close_all != 1:
            raise SimulationError("INVALID_ORDER")
        if (action in OPENING and close_all) or (action in REDUCING and notional is not None):
            raise SimulationError("INVALID_ORDER")
        if quantity is not None:
            positive(quantity)
        if notional is not None:
            positive(notional)
        if action in REDUCING:
            side = "long" if action == "sell" else "short"
            available = state.book.quantity(instrument_id, side)
            reserved = ZERO
            for order in state.orders:
                if order.status == "pending" and order.instrument_id == instrument_id and order.action == action:
                    reserved += available if order.close_all else order.quantity
            if available == ZERO or reserved + (available if close_all else quantity) > available:
                raise SimulationError("POSITION_ALREADY_RESERVED")
        order = Order(
            id=f"order_{len(state.orders) + 1}", instrument_id=instrument_id,
            action=action, quantity=quantity, notional=notional, close_all=close_all,
        )
        return event(replace(state, orders=state.orders + (order,)), "order_queued", order.id)

    def cancel(self, state: State, order_id: str) -> State:
        self._check_state(state)
        if state.status not in ("ready", "margin_call"):
            raise SimulationError("GAME_NOT_TRADABLE")
        order = next((item for item in state.orders if item.id == order_id), None)
        if not order or order.status != "pending":
            raise SimulationError("ORDER_NOT_PENDING")
        orders = tuple(replace(item, status="cancelled") if item.id == order_id else item
                       for item in state.orders)
        return event(replace(state, orders=orders), "order_cancelled", order_id)

    def _session_bars(self, state, day):
        required = {lot.instrument_id for lot in state.book.lots}
        required.update(order.instrument_id for order in state.orders if order.status == "pending")
        result = {}
        for instrument_id in sorted(required):
            bar = self.bars.get((day, instrument_id))
            if bar is None:
                raise SimulationError("MARKET_DATA_UNAVAILABLE")
            if bar.unverified_terminal_event:
                raise SimulationError("UNVERIFIED_TERMINAL_EVENT")
            try:
                positive(bar.open)
                positive(bar.close)
                positive(bar.split_numerator)
                positive(bar.split_denominator)
                dividend = bar.dividend_per_prior_share
                if not isinstance(dividend, Decimal) or dividend < ZERO or dividend != money(dividend):
                    raise ValueError()
            except (ValueError, DecimalException):
                raise SimulationError("INVALID_MARKET_DATA") from None
            result[instrument_id] = bar
        return result

    @decimal_calculation
    def _financing(self, state, day):
        previous = state.snapshots[-1].portfolio
        days = Decimal((day - state.cursor).days)
        interest = money(previous.debit_balance * Decimal("0.08") * days / Decimal(365))
        borrow = money(previous.short_liability * Decimal("0.03") * days / Decimal(365))
        book = state.book
        if interest:
            book = post(book, f"{day}:interest", cash=-interest, margin_interest_expense=interest)
        if borrow:
            book = post(book, f"{day}:borrow", cash=-borrow, borrow_fee_expense=borrow)
        return replace(state, book=book)

    @decimal_calculation
    def _actions(self, state, bars):
        book, orders = state.book, state.orders
        # All entitlements come from prior-close positions, before any open fill.
        for instrument_id, bar in bars.items():
            entitlement = book.quantity(instrument_id, "long") - book.quantity(instrument_id, "short")
            dividend = money(entitlement * bar.dividend_per_prior_share)
            if dividend:
                book = post(book, f"{bar.date}:dividend:{instrument_id}",
                            cash=dividend, dividend_income=-dividend)
                state = event(state, "dividend", instrument_id)
            if bar.split_numerator == bar.split_denominator:
                continue

            def scaled(quantity):
                result = quantity * bar.split_numerator / bar.split_denominator
                if result <= ZERO or result != money(result):
                    # No silent loss of shares, and no invented cash-in-lieu.
                    raise SimulationError("UNSUPPORTED_SPLIT_PRECISION")
                return positive(result)

            book = replace(book, lots=tuple(
                replace(lot, quantity=scaled(lot.quantity)) if lot.instrument_id == instrument_id else lot
                for lot in book.lots
            ))
            orders = tuple(
                replace(order, quantity=scaled(order.quantity))
                if order.status == "pending" and order.instrument_id == instrument_id and order.quantity is not None
                else order for order in orders
            )
            state = event(state, "split", instrument_id)
        return replace(state, book=book, orders=orders)

    @decimal_calculation
    def _orders(self, state, bars):
        pending = [order for order in state.orders if order.status == "pending"]
        # Python's stable sort preserves acceptance order within both groups.
        pending.sort(key=lambda order: order.action in OPENING)
        for order in pending:
            bar, reason = bars[order.instrument_id], None
            side = "long" if order.action in ("buy", "sell") else "short"
            quantity = order.quantity
            candidate = None
            if order.close_all:
                quantity = state.book.quantity(order.instrument_id, side)
            elif order.notional is not None:
                try:
                    quantity = notional_quantity(order.notional, bar.open)
                except ValueError:
                    reason = "ORDER_LIMIT_EXCEEDED"
            if reason is None:
                try:
                    if quantity == ZERO or money(quantity * bar.open) == ZERO:
                        reason = "QUANTITY_ROUNDS_TO_ZERO"
                    elif order.action in REDUCING and quantity > state.book.quantity(order.instrument_id, side):
                        reason = "POSITION_UNAVAILABLE"
                    elif order.action in OPENING and state.book.quantity(
                        order.instrument_id, "short" if side == "long" else "long",
                    ):
                        reason = "OPPOSING_POSITION"
                    if reason is None:
                        candidate = fill(state.book, f"fill:{order.id}", order.instrument_id,
                                         order.action, quantity, bar.open)
                        if order.action in OPENING:
                            opening = value_book(candidate, {key: item.open for key, item in bars.items()})
                            if opening.equity <= ZERO or opening.equity < opening.initial_requirement:
                                reason = "INSUFFICIENT_BUYING_POWER"
                except (DecimalException, ValueError):
                    reason = "ORDER_LIMIT_EXCEEDED"
            if reason:
                updated = replace(order, status="rejected", reason=reason)
                state = event(state, "order_rejected", order.id)
            else:
                updated = replace(order, status="filled")
                execution = Fill(order.id, order.instrument_id, order.action, state.cursor,
                                 quantity, bar.open, money(quantity * bar.open))
                state = replace(state, book=candidate, fills=state.fills + (execution,))
                state = event(state, "order_filled", order.id)
            state = replace(state, orders=tuple(
                updated if item.id == order.id else item for item in state.orders
            ))
        return state

    @decimal_calculation
    def _step(self, state, day):
        """Construct an entire checkpoint before making any result observable."""
        bars = self._session_bars(state, day)
        result = self._financing(state, day)
        result = replace(result, cursor=day)
        result = self._actions(result, bars)
        result = self._orders(result, bars)
        valuation = value_book(result.book, {key: bar.close for key, bar in bars.items()})
        positions = []
        for instrument_id in sorted({lot.instrument_id for lot in result.book.lots}):
            for side in ("long", "short"):
                lots = [lot for lot in result.book.lots if lot.instrument_id == instrument_id and lot.side == side]
                if lots:
                    positions.append(PositionSnapshot(
                        instrument_id, side, money(sum((lot.quantity for lot in lots), ZERO)),
                        money(sum((lot.basis for lot in lots), ZERO)), bars[instrument_id].close,
                    ))
        snapshot = Snapshot(day, valuation, tuple(positions))
        status = "ready"
        if valuation.equity <= ZERO:
            status = "bankrupt"
        elif day == result.end_date:
            status = "completed"
        elif valuation.maintenance_shortfall > ZERO:
            status = "margin_call"
        result = replace(result, status=status, snapshots=result.snapshots + (snapshot,))
        reconcile(result.book)
        return event(result, "session_closed" if status == "ready" else status)

    def step(self, state: State) -> State:
        """Return one whole session for a future worker's transaction boundary."""
        self._check_state(state)
        if state.status != "ready":
            raise SimulationError("MARGIN_CALL_ACTIVE" if state.status == "margin_call" else "GAME_NOT_TRADABLE")
        index = bisect_right(self.sessions, state.cursor)
        if index >= len(self.sessions) or self.sessions[index] > state.end_date:
            raise SimulationError("INVALID_ADVANCE_TARGET")
        return self._step(state, self.sessions[index])

    def _target(self, state, target):
        if target <= state.cursor or target > state.end_date:
            raise SimulationError("INVALID_ADVANCE_TARGET")
        if target > shifted_date(state.cursor, 10, "years"):
            raise SimulationError("ADVANCE_LIMIT_EXCEEDED")
        effective = self.sessions[bisect_right(self.sessions, target) - 1]
        if effective <= state.cursor:
            raise SimulationError("INVALID_ADVANCE_TARGET")
        return effective

    def advance(self, state: State, target: date) -> AdvanceResult:
        self._check_state(state)
        if state.status != "ready":
            raise SimulationError("MARGIN_CALL_ACTIVE" if state.status == "margin_call" else "GAME_NOT_TRADABLE")
        effective = self._target(state, target)
        current = state
        for day in self.sessions[bisect_right(self.sessions, state.cursor):]:
            if day > effective:
                break
            try:
                current = self.step(current)
            except SimulationError as exc:
                return AdvanceResult(current, target, effective, "blocked_data", exc.code)
            except (DecimalException, ValueError):
                return AdvanceResult(current, target, effective, "blocked_data", "INVALID_MARKET_DATA")
            if current.status != "ready":
                outcome = "interrupted_margin" if current.status == "margin_call" else current.status
                return AdvanceResult(current, target, effective, outcome)
        return AdvanceResult(current, target, effective, "completed")

    def resolve_margin(self, state: State, *, liquidate_all: bool = False) -> AdvanceResult:
        self._check_state(state)
        if state.status != "margin_call" or not isinstance(liquidate_all, bool):
            raise SimulationError("NO_MARGIN_CALL")
        candidate = state
        if liquidate_all:
            for order in candidate.orders:
                if order.status == "pending":
                    candidate = self.cancel(candidate, order.id)
            for instrument_id in sorted({lot.instrument_id for lot in candidate.book.lots}):
                action = "sell" if candidate.book.quantity(instrument_id, "long") else "buy_to_cover"
                candidate = self.queue(candidate, instrument_id, action, close_all=True)
        next_day = self.sessions[bisect_right(self.sessions, state.cursor)]
        result = self.advance(replace(candidate, status="ready"), next_day)
        if result.outcome == "blocked_data":
            # Resolution command changes are also uncommitted if the day fails.
            return replace(result, state=state)
        return result
