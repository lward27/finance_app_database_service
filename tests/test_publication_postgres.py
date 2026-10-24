"""Private-schema PostgreSQL publication races, rollback, and immutable approval."""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import func, text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select

from finance_app_database_service import publication_store as store
from finance_app_database_service.foundation_models import DatasetRelease, Instrument
from finance_app_database_service.game_market import load_market
from finance_app_database_service.game_models import CalendarBinding, MarketPublication, MarketRevision
from finance_app_database_service.market_publication import PublicationError
from publication_support import KEY, NOW, artifacts, seed_instrument
from test_market_qualification import A
import test_foundation_postgres as postgres_support


@unittest.skipUnless(os.getenv("FINANCE_TEST_DATABASE_URL"), "Requires disposable PostgreSQL *_test database")
class TestPublicationPostgres(unittest.TestCase):
    cleanup_schema = postgres_support.TestFoundationPostgres.cleanup_schema

    def setUp(self):
        postgres_support.TestFoundationPostgres.setUp(self)
        seed_instrument(self.engine)
        self.values = artifacts()

    def publish(self, values=None, clock=None):
        return store.publish(self.engine, *(values or self.values), KEY,
                             store.target_sha256(self.engine, self.schema),
                             expected_schema=self.schema, clock=clock or (lambda: NOW))

    def counts(self):
        with Session(self.engine) as session:
            return tuple(session.exec(select(func.count()).select_from(model)).one()
                         for model in (DatasetRelease, MarketPublication, MarketRevision, CalendarBinding))

    def test_concurrent_duplicate_publishers_commit_exactly_one_release_and_approval(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.publish(), range(6)))
        self.assertEqual(sum(result["status"] == "published" for result in results), 1)
        self.assertEqual(sum(result["status"] == "already_published" for result in results), 5)
        self.assertEqual(len({result["release_id"] for result in results}), 1)
        self.assertEqual(self.counts(), (1, 1, 4, 1))
        with Session(self.engine) as session:
            self.assertEqual(load_market(session, results[0]["release_id"]).release_id, results[0]["release_id"])

    def test_competing_calendar_bindings_cannot_leave_the_losing_release_or_revisions(self):
        alternate = artifacts()[0]
        alternate["calendar"]["sessions"].append("2020-09-04")
        competing = (self.values, artifacts(alternate))

        def publish(values):
            try:
                return self.publish(values)
            except PublicationError as error:
                return {"code": error.code}

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(publish, competing))
        self.assertEqual(sum(result.get("status") == "published" for result in results), 1)
        self.assertEqual(sum(result.get("code") == "CALENDAR_BINDING_CONFLICT" for result in results), 1)
        self.assertEqual(self.counts(), (1, 1, 4, 1))

    def test_partial_failure_and_late_expiry_roll_back_all_metadata_and_inputs(self):
        with patch.object(store, "load_market", side_effect=RuntimeError("synthetic pre-commit crash")):
            with self.assertRaises(RuntimeError):
                self.publish()
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        times = iter((NOW, NOW, NOW + timedelta(hours=1)))
        with self.assertRaises(PublicationError) as caught:
            self.publish(clock=lambda: next(times))
        self.assertEqual(caught.exception.code, "REVIEW_NOT_CURRENT")
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        with Session(self.engine) as session:
            instrument = session.get(Instrument, A)
            self.assertEqual((instrument.currency, instrument.metadata_status), (None, "unverified"))
        self.assertEqual(self.publish()["status"], "published")

    def test_approval_receipts_are_immutable_under_real_postgres_triggers(self):
        self.publish()
        for statement in ("UPDATE market_publication SET approval_json = '{}'", "DELETE FROM market_publication"):
            with self.subTest(statement=statement), self.assertRaises(DBAPIError) as caught:
                with self.engine.begin() as connection:
                    connection.execute(text(statement))
            self.assertEqual(caught.exception.orig.pgcode, "55000")

    def test_runtime_schema_mismatch_blocks_writes_before_table_access(self):
        with self.assertRaises(PublicationError) as caught:
            store.publish(self.engine, *self.values, KEY, store.target_sha256(self.engine),
                          clock=lambda: NOW)
        self.assertEqual(caught.exception.code, "DATABASE_SCHEMA_MISMATCH")
        self.assertEqual(self.counts(), (0, 0, 0, 0))
