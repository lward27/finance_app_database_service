"""Read-only, hash-checked qualified revisions. Never read live Yahoo snapshots."""

from decimal import Decimal

from sqlmodel import select

from finance_app_database_service.contracts import (
    canonical_json, content_digest,
)
from finance_app_database_service.foundation_api import release_manifest
from finance_app_database_service.foundation_models import DatasetRelease, Instrument
from finance_app_database_service.game_models import CalendarBinding, MarketRevision
from finance_app_database_service.market_documents import (
    MAX_INSTRUMENTS, MAX_RELEASE_BYTES, MAX_REVISION_BYTES,
    ActionsDocument, BarsDocument, CalendarDocument, ValidationDocument,
)
from finance_app_database_service.problems import Problem
from finance_app_database_service.simulation import Bar, Simulator


def load_market(session, release_id):
    """Verify stored input identities and qualification reports before simulation.

    Only the explicitly approved operator publication workflow may populate these
    tables. This reader supplies no import endpoint or automatic qualification.
    """
    release = session.get(DatasetRelease, release_id)
    if release is None or release.published_at is None or release.qualification != "simulation_qualified":
        raise Problem(404, "RELEASE_NOT_FOUND", "Published qualified release not found")
    manifest = release_manifest(release)
    if len(manifest.instruments) > MAX_INSTRUMENTS:
        raise Problem(503, "RELEASE_CAPACITY_EXCEEDED", "Release exceeds the initial game universe limit")
    consumed = 0

    def revision(revision_id, kind, contract):
        nonlocal consumed
        row = session.get(MarketRevision, revision_id)
        if row is None:
            raise Problem(503, "QUALIFIED_INPUTS_UNAVAILABLE", "Verified market revisions are unavailable")
        consumed += len(row.body_json)
        if row.kind != kind or len(row.body_json) > MAX_REVISION_BYTES or consumed > MAX_RELEASE_BYTES:
            raise ValueError("Invalid revision envelope")
        document = contract.parse_raw(row.body_json)
        if content_digest(canonical_json(document)) != row.id:
            raise ValueError("Revision checksum mismatch")
        return document

    try:
        binding = session.get(CalendarBinding, (manifest.calendar_id, manifest.calendar_version))
        if binding is None:
            raise Problem(503, "QUALIFIED_INPUTS_UNAVAILABLE", "Verified calendar is unavailable")
        calendar = revision(binding.revision_id, "calendar", CalendarDocument)
        if (calendar.calendar_id, calendar.calendar_version) != (manifest.calendar_id, manifest.calendar_version):
            raise ValueError("Calendar identity mismatch")
        if calendar.sessions != sorted(set(calendar.sessions)):
            raise ValueError("Calendar is not sorted and unique")
        if calendar.sessions[0] > manifest.from_date or calendar.sessions[-1] < manifest.to_date:
            raise ValueError("Calendar does not cover the release")
        sessions = tuple(day for day in calendar.sessions if manifest.from_date <= day <= manifest.to_date)
        bars = []
        instruments = {
            row.id: row for row in session.exec(select(Instrument).where(
                Instrument.id.in_([item.instrument_id for item in manifest.instruments]),
            )).all()
        }
        for coverage in manifest.instruments:
            instrument = instruments.get(coverage.instrument_id)
            if instrument is None or instrument.currency != "USD" or instrument.metadata_status != "verified":
                raise ValueError("Instrument metadata is not verified")
            report = revision(coverage.validation_report_sha256, "validation", ValidationDocument)
            if (
                report.instrument_id != coverage.instrument_id
                or report.calendar_revision_id != binding.revision_id
                or report.bar_revision_id != coverage.bar_revision_id
                or report.action_revision_id != coverage.action_revision_id
                or report.source_snapshot_ids != coverage.source_snapshot_ids
            ):
                raise ValueError("Qualification report references differ from the release")
            prices = revision(coverage.bar_revision_id, "bars", BarsDocument)
            actions = revision(coverage.action_revision_id, "actions", ActionsDocument)
            if prices.instrument_id != coverage.instrument_id or actions.instrument_id != coverage.instrument_id:
                raise ValueError("Revision instrument mismatch")
            action_map = {item.date: item for item in actions.actions}
            if len(action_map) != len(actions.actions):
                raise ValueError("Duplicate corporate action")
            if len({bar.date for bar in prices.bars}) != len(prices.bars):
                raise ValueError("Duplicate bar")
            if set(action_map) - {bar.date for bar in prices.bars}:
                raise ValueError("Action lacks a bar")
            for price in prices.bars:
                action = action_map.get(price.date)
                values = {} if action is None else dict(
                    split_numerator=Decimal(action.split_numerator),
                    split_denominator=Decimal(action.split_denominator),
                    dividend_per_prior_share=Decimal(action.dividend_per_prior_share),
                    unverified_terminal_event=action.unverified_terminal_event,
                )
                bars.append(Bar(
                    coverage.instrument_id, price.date,
                    Decimal(price.open) if price.open is not None else None,
                    Decimal(price.close) if price.close is not None else None, **values,
                ))
        return Simulator(manifest, sessions, tuple(bars))
    except (ValueError, TypeError, ArithmeticError):
        raise Problem(503, "QUALIFIED_INPUT_INTEGRITY_ERROR", "Qualified market input validation failed") from None
