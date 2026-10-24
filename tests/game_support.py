"""Synthetic qualified fixtures, never an importer or publication command."""

from datetime import datetime
from decimal import Decimal

from sqlmodel import Session

from finance_app_database_service.contracts import canonical_json, content_digest
from finance_app_database_service.foundation_models import Instrument
from finance_app_database_service.game_market import (
    ActionsDocument, BarsDocument, CalendarDocument, ValidationDocument,
)
from finance_app_database_service.game_models import CalendarBinding, MarketRevision
from foundation_support import manifest, release_row
from test_simulation import A, DAYS


def seed_release(engine, *, prices=None, overrides=None):
    overrides = overrides or {}
    prices = prices or ["100"] * len(DAYS)
    with Session(engine) as session:
        def revision(kind, document):
            body = canonical_json(document)
            digest = content_digest(body)
            session.add(MarketRevision(id=digest, kind=kind, body_json=body))
            session.flush()
            return digest

        session.add(Instrument(id=A, symbol="A", name="Synthetic listing", exchange="TEST",
                               currency="USD", metadata_status="verified"))
        calendar = revision("calendar", CalendarDocument(
            schema_version=1, calendar_id="XNYS", calendar_version="test-calendar-v1", sessions=list(DAYS),
        ))
        session.add(CalendarBinding(calendar_id="XNYS", version="test-calendar-v1", revision_id=calendar))
        bars = []
        for day, price in zip(DAYS, prices):
            opening, closing = price if isinstance(price, tuple) else (price, price)
            bars.append(dict(date=day, open=opening, close=closing))
        bars_id = revision("bars", BarsDocument(schema_version=1, instrument_id=A,
                                               currency="USD", price_basis="as_traded", bars=bars))
        actions_id = revision("actions", ActionsDocument(
            schema_version=1, instrument_id=A, actions=overrides.get("actions", []),
        ))
        report_id = revision("validation", ValidationDocument(
            schema_version=1, instrument_id=A, qualification="simulation_qualified",
            currency="USD", price_basis="as_traded", calendar_revision_id=calendar,
            bar_revision_id=bars_id, action_revision_id=actions_id, source_snapshot_ids=["c" * 64],
            checks=["currency", "calendar", "price_basis", "corporate_actions", "terminal_events"],
        ))
        value = manifest(A)
        value.from_date, value.to_date = DAYS[0], DAYS[-1]
        coverage = value.instruments[0]
        coverage.from_date, coverage.to_date = DAYS[0], DAYS[-1]
        coverage.bar_revision_id, coverage.action_revision_id = bars_id, actions_id
        coverage.validation_report_sha256 = report_id
        row = release_row(value, published_at=datetime.utcnow())
        session.add(row)
        session.commit()
        return row.id
