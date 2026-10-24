"""Player command contracts. Owners, balances, fills, and clocks are server-owned."""

from datetime import date
from decimal import Decimal
from typing import Generic, List, Literal, Optional, TypeVar

from pydantic import StrictBool, conint, constr, root_validator, validator
from pydantic.generics import GenericModel

from finance_app_database_service.contracts import Contract, InstrumentID
from finance_app_database_service.financial import MoneyString, QuantityString


class GameCreate(Contract):
    name: constr(strict=True, min_length=1, max_length=100)
    start_date: date
    end_date: Optional[date] = None
    initial_cash: MoneyString
    currency: Literal["USD"] = "USD"
    ruleset_id: Literal["us-equity-margin-v1"] = "us-equity-margin-v1"
    release_id: Optional[constr(strict=True, regex=r"^rel_[0-9a-f]{64}$")] = None

    @validator("initial_cash")
    def positive_capital(cls, value):
        if not Decimal("0") < Decimal(value) <= Decimal("1000000000000"):
            raise ValueError("Initial capital exceeds game limits")
        return value

    @validator("name")
    def clean_name(cls, value):
        value = value.strip()
        if not value or any(ord(character) < 32 for character in value):
            raise ValueError("A printable name is required")
        return value


class GameRename(Contract):
    name: constr(strict=True, min_length=1, max_length=100)
    _clean_name = validator("name", allow_reuse=True)(GameCreate.clean_name.__func__)


class EmptyCommand(Contract):
    pass


class OrderCreate(Contract):
    instrument_id: InstrumentID
    action: Literal["buy", "sell", "sell_short", "buy_to_cover"]
    quantity: Optional[QuantityString] = None
    notional: Optional[QuantityString] = None
    close_all: StrictBool = False
    type: Literal["market"] = "market"
    time_in_force: Literal["next_session"] = "next_session"

    @root_validator
    def valid_size(cls, values):
        quantity, notional = values.get("quantity"), values.get("notional")
        close_all = values.get("close_all", False)
        action = values.get("action")
        if (quantity is not None) + (notional is not None) + close_all != 1:
            raise ValueError("Exactly one order size is required")
        if any(Decimal(value) <= 0 for value in (quantity, notional) if value is not None):
            raise ValueError("Size must be positive")
        if (action in ("buy", "sell_short") and close_all) or (
            action in ("sell", "buy_to_cover") and notional is not None
        ):
            raise ValueError("Invalid size for action")
        return values


class AdvanceBy(Contract):
    count: conint(strict=True, ge=1, le=3660)
    unit: Literal["days", "weeks", "months", "years"]


class AdvanceCreate(Contract):
    to_date: Optional[date] = None
    by: Optional[AdvanceBy] = None

    @root_validator
    def one_target(cls, values):
        if (values.get("to_date") is not None) + (values.get("by") is not None) != 1:
            raise ValueError("Exactly one advance target is required")
        return values


class MarginResolution(Contract):
    liquidate_all: StrictBool = False


class GameView(Contract):
    id: str
    name: str
    revision: int
    status: Literal["ready", "advancing", "margin_call", "completed", "bankrupt"]
    archived: bool
    requested_start: date
    start_date: date
    end_date: date
    cursor: date
    initial_cash: MoneyString
    currency: Literal["USD"]
    ruleset_id: str
    release_id: str
    active_operation_id: Optional[str]
    allowed_actions: List[str]


class OperationView(Contract):
    id: str
    game_id: str
    kind: Literal["advance", "margin_resolution"]
    status: Literal["queued", "running", "completed", "interrupted_margin", "blocked_data", "failed"]
    requested_target: date
    effective_target: date
    actual_cursor: date
    game_status: str
    revision: int
    code: Optional[str]


class FillView(Contract):
    order_id: str
    instrument_id: InstrumentID
    action: str
    date: date
    quantity: QuantityString
    price: MoneyString
    amount: MoneyString


class OrderView(Contract):
    id: str
    instrument_id: InstrumentID
    action: str
    quantity: Optional[QuantityString]
    notional: Optional[QuantityString]
    close_all: bool
    status: Literal["pending", "filled", "rejected", "cancelled"]
    reason: Optional[str]
    fill: Optional[FillView]
    revision: Optional[int] = None


class ValuationView(Contract):
    cash: MoneyString
    restricted_proceeds: MoneyString
    free_cash: MoneyString
    debit_balance: MoneyString
    long_value: MoneyString
    short_liability: MoneyString
    gross_exposure: MoneyString
    net_exposure: MoneyString
    equity: MoneyString
    initial_requirement: MoneyString
    maintenance_requirement: MoneyString
    maintenance_shortfall: MoneyString
    buying_power: MoneyString
    realized_pnl: MoneyString
    unrealized_pnl: MoneyString
    dividends: MoneyString
    margin_interest: MoneyString
    borrow_fees: MoneyString


class PortfolioView(ValuationView):
    as_of: date
    revision: int


class PositionView(Contract):
    instrument_id: InstrumentID
    side: Literal["long", "short"]
    quantity: QuantityString
    basis: MoneyString
    price: MoneyString


class PostingView(Contract):
    account: str
    amount: MoneyString


class LedgerView(Contract):
    key: str
    postings: List[PostingView]


class EventView(Contract):
    sequence: int
    date: date
    kind: str
    reference: str


class PreviewView(Contract):
    non_binding: Literal[True]
    as_of: date
    price: MoneyString
    quantity: QuantityString
    estimated_notional: MoneyString
    execution: Literal["next_session_open"]
    revision: int


class QuoteView(Contract):
    instrument_id: InstrumentID
    price: Optional[MoneyString]
    price_date: date
    as_of: date
    currency: Literal["USD"]
    price_basis: Literal["as_traded"]
    tradable: bool


class QuotesView(Contract):
    items: List[QuoteView]


class HistoryPoint(Contract):
    date: date
    open: Optional[MoneyString]
    close: Optional[MoneyString]


class PerformancePoint(ValuationView):
    date: date


Item = TypeVar("Item")


class Page(GenericModel, Generic[Item]):
    items: List[Item]
    next_cursor: Optional[str]

    class Config:
        extra = "forbid"
