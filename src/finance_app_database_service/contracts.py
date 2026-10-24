"""Versioned public contracts. Financial values are strict decimal strings."""

import hashlib
import json
from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, StrictBool, conlist, constr, root_validator

from finance_app_database_service.financial import RateString

Digest = constr(strict=True, regex=r"^[0-9a-f]{64}$")
InstrumentID = constr(strict=True, regex=r"^ins_[0-9a-f]{32}$")


class Contract(BaseModel):
    class Config:
        extra = "forbid"


class GuestCreate(Contract):
    pass


class Capabilities(Contract):
    create_games: bool = False
    join_ranked: bool = False
    claim_guest: bool = False


class GuestView(Contract):
    principal_id: str
    kind: Literal["guest"] = "guest"
    created_at: datetime
    session_expires_at: datetime
    csrf_token: str
    capabilities: Capabilities = Capabilities()


class RulesDefinition(Contract):
    id: Literal["us-equity-margin-v1"]
    currency: Literal["USD"]
    execution: Literal["next_session_open"]
    order_type: Literal["market"]
    time_in_force: Literal["next_session"]
    fill_policy: Literal["all_or_reject"]
    long_positions: Literal[True]
    short_positions: Literal[True]
    fractional_shares: Literal[True]
    initial_margin_rate: RateString
    maintenance_margin_rate: RateString
    annual_margin_interest_rate: RateString
    annual_short_borrow_rate: RateString
    accrual: Literal["actual_calendar_days/365"]
    short_proceeds: Literal["restricted_by_fifo_lot"]
    cash_interest_rate: Literal["0"]
    commission: Literal["0"]
    slippage: Literal["0"]
    settlement: Literal["immediate_simulated"]
    lot_matching: Literal["FIFO"]
    money_decimal_places: Literal[8]
    quantity_decimal_places: Literal[8]
    money_rounding: Literal["ROUND_HALF_EVEN"]
    notional_quantity_rounding: Literal["ROUND_DOWN"]
    margin_call: Literal["pause_at_first_breaching_close"]
    dividends: Literal["prior_close_entitlement_booked_on_ex_date"]
    additional_contributions: Literal[False]
    max_advance_calendar_years: Literal[10]
    disclosures: List[str]

    @root_validator
    def fixed_v1_rates(cls, values):
        expected = {
            "initial_margin_rate": "0.50", "maintenance_margin_rate": "0.30",
            "annual_margin_interest_rate": "0.08", "annual_short_borrow_rate": "0.03",
        }
        if any(key in values and values[key] != value for key, value in expected.items()):
            raise ValueError("An immutable ruleset cannot change its financial rules")
        return values


class RulesetView(Contract):
    id: str
    sha256: Digest
    definition: RulesDefinition


class RulesetPage(Contract):
    items: List[RulesetView]
    next_cursor: Optional[str] = None


class InstrumentView(Contract):
    id: InstrumentID
    symbol: str
    name: str
    exchange: str
    currency: Optional[str]
    metadata_status: str


class InstrumentDetail(InstrumentView):
    aliases: List[str]
    legacy_ticker_ids: List[int]


class InstrumentPage(Contract):
    items: List[InstrumentView]
    next_cursor: Optional[str]


class QualifiedCoverage(Contract):
    instrument_id: InstrumentID
    from_date: date
    to_date: date
    bar_revision_id: Digest
    action_revision_id: Digest
    source_snapshot_ids: conlist(Digest, min_items=1, max_items=20)
    validation_report_sha256: Digest
    shortable: StrictBool

    @root_validator
    def valid_dates(cls, values):
        if values.get("from_date") and values.get("to_date"):
            if values["from_date"] > values["to_date"]:
                raise ValueError("Coverage start must not follow its end")
        return values


class ReleaseManifest(Contract):
    schema_version: Literal[1]
    qualification: Literal["simulation_qualified"]
    currency: Literal["USD"]
    price_basis: Literal["as_traded"]
    calendar_id: constr(strict=True, min_length=1, max_length=64)
    calendar_version: constr(strict=True, min_length=1, max_length=128)
    from_date: date
    to_date: date
    instruments: conlist(QualifiedCoverage, min_items=1, max_items=10000)

    @root_validator
    def valid_coverage(cls, values):
        coverage = values.get("instruments") or []
        if coverage:
            if len({item.instrument_id for item in coverage}) != len(coverage):
                raise ValueError("Duplicate instrument coverage")
            if (
                values.get("from_date") != min(item.from_date for item in coverage)
                or values.get("to_date") != max(item.to_date for item in coverage)
            ):
                raise ValueError("Release bounds must describe its instrument coverage")
        return values


class ReleaseSummary(Contract):
    id: str
    sha256: Digest
    published_at: datetime
    qualification: Literal["simulation_qualified"]
    currency: Literal["USD"]
    price_basis: Literal["as_traded"]
    calendar_id: str
    calendar_version: str
    from_date: date
    to_date: date
    instrument_count: int


class ReleaseDetail(ReleaseSummary):
    manifest: ReleaseManifest


class ReleasePage(Contract):
    items: List[ReleaseSummary]
    next_cursor: Optional[str]


def canonical_json(value) -> str:
    if isinstance(value, BaseModel):
        value = json.loads(value.json())
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def content_digest(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
