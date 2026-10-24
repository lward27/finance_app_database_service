import json
import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import patch

from sqlmodel import Session, select

from finance_app_database_service.contracts import canonical_json, content_digest
from finance_app_database_service.foundation_models import DatasetRelease, Instrument
from finance_app_database_service.game_market import load_market
from finance_app_database_service.game_models import CalendarBinding, MarketRevision
from finance_app_database_service.problems import Problem
from foundation_support import manifest, migrate, release_row, sqlite_engine
from game_support import seed_release
from test_simulation import A, DAYS


class TestQualifiedGameInputs(unittest.TestCase):
    def setUp(self):
        self.engine = sqlite_engine()
        self.addCleanup(self.engine.dispose)
        migrate(self.engine)
        self.release_id = seed_release(self.engine)

    def test_loads_actual_pinned_calendar_bars_actions_and_validation_reports(self):
        with Session(self.engine) as session:
            market = load_market(session, self.release_id)
        self.assertEqual(market.release_id, self.release_id)
        self.assertEqual(market.sessions, DAYS)
        self.assertEqual(str(market.bars[(DAYS[1], A)].open), "100")

    def test_published_manifest_alone_is_not_sufficient_for_game_inputs(self):
        engine = sqlite_engine()
        self.addCleanup(engine.dispose)
        migrate(engine)
        with Session(engine) as session:
            row = release_row(manifest(A), published_at=datetime.utcnow())
            session.add(row)
            session.commit()
            with self.assertRaises(Problem) as caught:
                load_market(session, row.id)
            self.assertEqual(caught.exception.code, "QUALIFIED_INPUTS_UNAVAILABLE")

    def test_corrupt_wrong_kind_float_and_missing_revisions_fail_closed(self):
        with Session(self.engine) as session:
            bars = session.exec(select(MarketRevision).where(MarketRevision.kind == "bars")).one()
            original_get = session.get
            original = json.loads(bars.body_json)
            invalid = [
                None, MarketRevision(id=bars.id, kind="actions", body_json=bars.body_json),
                MarketRevision(id=bars.id, kind="bars", body_json="not JSON"),
                MarketRevision(id=bars.id, kind="bars", body_json=canonical_json({
                    **original, "bars": [{**original["bars"][0], "open": 1.25}],
                })),
                MarketRevision(id=bars.id, kind="bars", body_json=canonical_json({
                    **original, "bars": [{**original["bars"][0], "open": "999"}] + original["bars"][1:],
                })),
            ]
            for replacement in invalid:
                def get(model, identity, *args, **kwargs):
                    return replacement if model is MarketRevision and identity == bars.id else original_get(
                        model, identity, *args, **kwargs,
                    )
                with self.subTest(replacement=replacement), patch.object(session, "get", side_effect=get):
                    with self.assertRaises(Problem) as caught:
                        load_market(session, self.release_id)
                    self.assertEqual(caught.exception.status, 503)

    def test_qualification_report_must_reference_exact_revisions_and_all_checks(self):
        with Session(self.engine) as session:
            release = session.get(DatasetRelease, self.release_id)
            base_manifest = json.loads(release.manifest_json)
            coverage = base_manifest["instruments"][0]
            row = session.get(MarketRevision, coverage["validation_report_sha256"])
            report = json.loads(row.body_json)
            for change in ({"bar_revision_id": "a" * 64}, {"checks": ["currency"]},
                           {"source_snapshot_ids": ["e" * 64]}):
                modified = {**report, **change}
                body = canonical_json(modified)
                digest = content_digest(body)
                session.add(MarketRevision(id=digest, kind="validation", body_json=body))
                data = {**base_manifest, "instruments": [{**coverage, "validation_report_sha256": digest}]}
                body = canonical_json(data)
                release_id = "rel_" + content_digest(body)
                session.add(DatasetRelease(id=release_id, manifest_json=body, sha256=content_digest(body),
                                           qualification="simulation_qualified", published_at=datetime.utcnow()))
                session.commit()
                with self.subTest(change=change), self.assertRaises(Problem) as caught:
                    load_market(session, release_id)
                self.assertEqual(caught.exception.code, "QUALIFIED_INPUT_INTEGRITY_ERROR")

    def test_unverified_currency_metadata_and_raw_source_releases_are_rejected(self):
        with Session(self.engine) as session:
            instrument = session.get(Instrument, A)
            instrument.metadata_status = "unverified"
            session.add(instrument)
            session.commit()
            with self.assertRaises(Problem) as caught:
                load_market(session, self.release_id)
            self.assertEqual(caught.exception.code, "QUALIFIED_INPUT_INTEGRITY_ERROR")
            with self.assertRaises(Problem) as caught:
                load_market(session, "source-only-snapshot")
            self.assertEqual(caught.exception.status, 404)
