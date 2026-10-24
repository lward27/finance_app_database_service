"""Pure market-revision contracts, shared by offline checks and game readers."""

from datetime import date
from typing import List, Literal, Optional

from pydantic import StrictBool, conlist, root_validator

from finance_app_database_service.contracts import Contract, Digest, InstrumentID
from finance_app_database_service.financial import QuantityString

MAX_REVISION_BYTES = 16 * 1024 * 1024
MAX_RELEASE_BYTES = 32 * 1024 * 1024
MAX_INSTRUMENTS = 50


class CalendarDocument(Contract):
    schema_version: Literal[1]
    calendar_id: str
    calendar_version: str
    sessions: conlist(date, min_items=2, max_items=25000)


class BarValue(Contract):
    date: date
    open: Optional[QuantityString]
    close: Optional[QuantityString]


class BarsDocument(Contract):
    schema_version: Literal[1]
    instrument_id: InstrumentID
    currency: Literal["USD"]
    price_basis: Literal["as_traded"]
    bars: conlist(BarValue, min_items=1, max_items=25000)


class ActionValue(Contract):
    date: date
    split_numerator: QuantityString = "1"
    split_denominator: QuantityString = "1"
    dividend_per_prior_share: QuantityString = "0"
    unverified_terminal_event: StrictBool = False


class ActionsDocument(Contract):
    schema_version: Literal[1]
    instrument_id: InstrumentID
    actions: conlist(ActionValue, max_items=25000)


class ValidationDocument(Contract):
    schema_version: Literal[1]
    instrument_id: InstrumentID
    qualification: Literal["simulation_qualified"]
    currency: Literal["USD"]
    price_basis: Literal["as_traded"]
    calendar_revision_id: Digest
    bar_revision_id: Digest
    action_revision_id: Digest
    source_snapshot_ids: conlist(Digest, min_items=1, max_items=20)
    checks: List[Literal["currency", "calendar", "price_basis", "corporate_actions", "terminal_events"]]

    @root_validator
    def complete_checks(cls, values):
        checks = values.get("checks") or []
        if sorted(checks) != sorted(("currency", "calendar", "price_basis", "corporate_actions", "terminal_events")):
            raise ValueError("Qualification checks are incomplete")
        return values
