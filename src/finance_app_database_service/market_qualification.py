"""Offline qualification preflight. Never publish, connect to a DB, or fetch data."""

import argparse
import json
import os
from datetime import date
from decimal import Context, Decimal, DecimalException, ROUND_HALF_EVEN, localcontext
from pathlib import Path
from typing import Any, Dict, Literal

from pydantic import StrictBool, ValidationError, conlist, constr

from finance_app_database_service.contracts import (
    Contract, Digest, InstrumentID, canonical_json, content_digest,
)
from finance_app_database_service.financial import money
from finance_app_database_service.market_documents import (
    MAX_INSTRUMENTS, MAX_RELEASE_BYTES, MAX_REVISION_BYTES,
    ActionsDocument, ActionValue, BarsDocument, BarValue, CalendarDocument,
)
from finance_app_database_service.market_import import validate_export

MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_SOURCE_ROWS = 100000
Text = constr(strict=True, strip_whitespace=True, min_length=1, max_length=500)


class EvidenceReference(Contract):
    provider: Text
    reference: Text


class ReferenceEvidence(Contract):
    instrument_id: InstrumentID
    symbol: Text
    currency: Literal["USD"]
    price_basis: Literal["as_traded"]
    exchange_timezone: Literal["America/New_York"]
    from_date: date
    to_date: date
    terminal_events: Literal["none_in_coverage"]
    currency_reference: EvidenceReference
    prices_reference: EvidenceReference
    actions_reference: EvidenceReference
    terminal_events_reference: EvidenceReference
    bars: conlist(BarValue, min_items=2, max_items=25000)
    actions: conlist(ActionValue, max_items=25000)


class QualificationInstrument(Contract):
    instrument_id: InstrumentID
    symbol: Text
    from_date: date
    to_date: date
    shortable: StrictBool
    source_snapshot_id: Digest
    source_export: Dict[str, Any]
    evidence: ReferenceEvidence


class QualificationInput(Contract):
    schema_version: Literal[1]
    calendar: CalendarDocument
    calendar_reference: EvidenceReference
    instruments: conlist(QualificationInstrument, min_items=1, max_items=MAX_INSTRUMENTS)


class QualificationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise QualificationError(code)


def independent(reference):
    # This catches obvious self-comparisons, not forged provider identities.
    name = reference.provider.casefold()
    require("yahoo" not in name and "yfinance" not in name, "NON_INDEPENDENT_EVIDENCE")


def source_rows(item):
    payload = item.source_export
    try:
        require(type(payload.get("schema_version")) is int, "INVALID_SOURCE_EXPORT")
        rows = payload["rows"]
        require(isinstance(rows, list) and 0 < len(rows) <= MAX_SOURCE_ROWS, "SOURCE_CAPACITY_EXCEEDED")
        for row in rows:
            for field in ("open", "high", "low", "close", "adjusted_close",
                          "dividend", "split_ratio", "capital_gain"):
                value = row[field]
                if value is not None:
                    require(isinstance(value, str) and len(value) <= 128, "INVALID_SOURCE_EXPORT")
                    number = Decimal(value)
                    require(number.is_finite() and 0 <= number < Decimal("1e20"), "INVALID_SOURCE_EXPORT")
        metadata, normalized = validate_export(payload, item.symbol)
    except QualificationError:
        raise
    except (ValueError, KeyError, TypeError, OverflowError, DecimalException):
        raise QualificationError("INVALID_SOURCE_EXPORT") from None
    require(metadata["content_hash"] == item.source_snapshot_id, "SOURCE_SNAPSHOT_MISMATCH")
    require(metadata["exchange_timezone"] == item.evidence.exchange_timezone, "SOURCE_TIMEZONE_MISMATCH")
    require(metadata["complete_through"] >= item.to_date, "SOURCE_NOT_COMPLETE")
    return normalized


def check_instrument(item, sessions):
    evidence = item.evidence
    require(
        (item.instrument_id, item.symbol, item.from_date, item.to_date)
        == (evidence.instrument_id, evidence.symbol, evidence.from_date, evidence.to_date),
        "EVIDENCE_IDENTITY_MISMATCH",
    )
    require(item.from_date < item.to_date, "INVALID_COVERAGE")
    require(item.from_date in sessions and item.to_date in sessions, "COVERAGE_OFF_CALENDAR")
    expected = [day for day in sessions if item.from_date <= day <= item.to_date]
    for name in ("currency_reference", "prices_reference", "actions_reference", "terminal_events_reference"):
        independent(getattr(evidence, name))
    require([bar.date for bar in evidence.bars] == expected, "REFERENCE_CALENDAR_MISMATCH")
    action_dates = [action.date for action in evidence.actions]
    require(action_dates == sorted(set(action_dates)), "INVALID_REFERENCE_ACTIONS")
    require(set(action_dates) <= set(expected), "ACTION_OFF_CALENDAR")
    require(not any(action.unverified_terminal_event for action in evidence.actions), "UNSUPPORTED_TERMINAL_EVENT")
    actions = {action.date: action for action in evidence.actions}
    rows = [row for row in source_rows(item) if item.from_date <= row["ts"] <= item.to_date]
    require([row["ts"] for row in rows] == expected, "SOURCE_CALENDAR_MISMATCH")
    prices = []
    checked_actions = []
    for row, reference in zip(rows, evidence.bars):
        require(all(row[key] is not None and row[key] > 0 for key in ("open", "high", "low", "close")),
                "MISSING_OR_ZERO_PRICE")
        require(row["low"] <= min(row["open"], row["close"])
                <= max(row["open"], row["close"]) <= row["high"], "INVALID_SOURCE_OHLC")
        require(row["capital_gain"] == 0, "UNSUPPORTED_CAPITAL_GAIN")
        factor = row["subsequent_split_factor"]
        require(factor.is_finite() and 0 < factor < Decimal("1e20"), "INVALID_SPLIT_FACTOR")
        opening, closing = money(row["open"] * factor), money(row["close"] * factor)
        require(reference.open is not None and reference.close is not None
                and opening > 0 and closing > 0, "MISSING_OR_ZERO_PRICE")
        require(opening == Decimal(reference.open) and closing == Decimal(reference.close), "PRICE_BASIS_MISMATCH")
        prices.append(BarValue(date=row["ts"], open=format(opening, "f"), close=format(closing, "f")))
        ratio = row["split_ratio"] or Decimal(1)
        dividend = money(row["dividend"] * factor)
        # Do not guess entitlement semantics for simultaneous split/dividend rows.
        require(not (ratio != 1 and row["dividend"] != 0), "SIMULTANEOUS_ACTIONS_UNSUPPORTED")
        action = actions.get(row["ts"])
        if ratio != 1 or row["dividend"] != 0:
            require(action is not None, "CORPORATE_ACTION_MISMATCH")
            numerator, denominator = Decimal(action.split_numerator), Decimal(action.split_denominator)
            require(numerator > 0 and denominator > 0
                    and numerator == ratio * denominator, "CORPORATE_ACTION_MISMATCH")
            require(row["dividend"] == 0 or dividend > 0, "CORPORATE_ACTION_MISMATCH")
            require(dividend == Decimal(action.dividend_per_prior_share), "CORPORATE_ACTION_MISMATCH")
            checked_actions.append(action)
        else:
            require(action is None, "CORPORATE_ACTION_MISMATCH")
    return (
        BarsDocument(schema_version=1, instrument_id=item.instrument_id, currency="USD",
                     price_basis="as_traded", bars=prices),
        ActionsDocument(schema_version=1, instrument_id=item.instrument_id, actions=checked_actions),
    )


def preflight(value):
    """Compare supplied evidence; passing never establishes its authenticity."""
    try:
        bundle = QualificationInput.parse_obj(value)
        canonical_input = canonical_json(bundle)
    except (ValidationError, TypeError, ValueError, RecursionError):
        raise QualificationError("INVALID_QUALIFICATION_INPUT") from None
    require(len(canonical_input.encode("utf-8")) <= MAX_INPUT_BYTES, "INPUT_CAPACITY_EXCEEDED")
    require(len({item.instrument_id for item in bundle.instruments}) == len(bundle.instruments),
            "DUPLICATE_INSTRUMENT")
    require(len({item.source_snapshot_id for item in bundle.instruments}) == len(bundle.instruments),
            "DUPLICATE_SOURCE_SNAPSHOT")
    require(sum(len(item.source_export.get("rows", []))
                for item in bundle.instruments if isinstance(item.source_export.get("rows"), list))
            <= MAX_SOURCE_ROWS, "SOURCE_CAPACITY_EXCEEDED")
    sessions = bundle.calendar.sessions
    require(sessions == sorted(set(sessions)), "INVALID_CALENDAR")
    require(0 < len(bundle.calendar.calendar_id) <= 64
            and 0 < len(bundle.calendar.calendar_version) <= 128, "INVALID_CALENDAR")
    independent(bundle.calendar_reference)
    revisions = {}

    def revision(kind, document):
        body = canonical_json(document)
        require(len(body.encode("utf-8")) <= MAX_REVISION_BYTES, "REVISION_CAPACITY_EXCEEDED")
        digest = content_digest(body)
        revisions[digest] = dict(id=digest, kind=kind, document=json.loads(body))
        return digest

    calendar_id = revision("calendar", bundle.calendar)
    coverage = []
    try:
        with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
            for item in sorted(bundle.instruments, key=lambda item: item.instrument_id):
                bars, actions = check_instrument(item, sessions)
                evidence = item.evidence
                coverage.append(dict(
                    instrument_id=item.instrument_id, symbol=item.symbol,
                    from_date=item.from_date.isoformat(), to_date=item.to_date.isoformat(),
                    shortable=item.shortable, source_snapshot_ids=[item.source_snapshot_id],
                    bar_revision_id=revision("bars", bars), action_revision_id=revision("actions", actions),
                    evidence_sha256=content_digest(canonical_json(evidence)),
                    references={name: getattr(evidence, name).dict() for name in (
                        "currency_reference", "prices_reference", "actions_reference", "terminal_events_reference",
                    )},
                ))
    except (DecimalException, ValueError) as error:
        if isinstance(error, QualificationError):
            raise
        raise QualificationError("FINANCIAL_LIMIT_EXCEEDED") from None
    candidate = dict(
        schema_version=1, status="ready_for_operator_review", qualification="pending",
        input_sha256=content_digest(canonical_input),
        calendar_revision_id=calendar_id, calendar_reference=bundle.calendar_reference.dict(),
        coverage=coverage, revisions=sorted(revisions.values(), key=lambda row: (row["kind"], row["id"])),
        review_requirements=[
            "Authenticate independent calendar, currency, prices, actions, and terminal-event evidence.",
            "Verify canonical instrument identities, coverage, and shortability policy.",
            "Use a separately approved publication step; this candidate is not a qualified release.",
        ],
    )
    require(len(canonical_json(candidate).encode("utf-8")) <= MAX_RELEASE_BYTES, "CANDIDATE_CAPACITY_EXCEEDED")
    return candidate


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "INVALID_JSON")
        result[key] = value
    return result


def invalid_constant(value):
    raise QualificationError("INVALID_JSON")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Archived exports and independent reference evidence")
    parser.add_argument("--output", type=Path, help="Write a NEW candidate file; never overwrite or publish")
    args = parser.parse_args(argv)
    try:
        with args.input.open("rb") as source:
            raw = source.read(MAX_INPUT_BYTES + 1)
        require(len(raw) <= MAX_INPUT_BYTES, "INPUT_CAPACITY_EXCEEDED")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=invalid_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise QualificationError("INVALID_JSON") from None
        candidate = preflight(value)
        body = canonical_json(candidate)
        if args.output is not None:
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(body + "\n")
        print(canonical_json(dict(
            status=candidate["status"], qualification=candidate["qualification"],
            input_sha256=candidate["input_sha256"], candidate_sha256=content_digest(body),
            instrument_count=len(candidate["coverage"]), revision_count=len(candidate["revisions"]),
            output_written=args.output is not None,
        )))
        return 0
    except QualificationError as error:
        print(canonical_json(dict(status="blocked", code=error.code)))
    except OSError:
        print(canonical_json(dict(status="blocked", code="INPUT_OR_OUTPUT_ERROR")))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
