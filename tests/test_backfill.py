import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from sqlmodel import Session, SQLModel, create_engine, select

from finance_app_database_service.backfill import (
    initialize, process_item, progress, retry_after, save_export, wait_for_slot,
)
from finance_app_database_service.market_models import (
    MarketBackfillItem, MarketBackfillThrottle, MarketHistory, MarketSnapshot,
)
from finance_app_database_service.models import Ticker
from test_market_import import export


CONFIG = {
    "wrapper_url": "http://wrapper:8000", "interval_seconds": 60,
    "batch_size": 2, "write_pause": 0, "ticker_batch_size": 10,
    "batch_pause_seconds": 300, "max_failures": 5,
}


class TestBackfill(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.session.add(Ticker(id=1, ticker="AAPL", name="Apple", exchange="NASDAQ"))
        self.session.commit()
        initialize(self.session, "test", CONFIG)
        self.item = self.session.get(MarketBackfillItem, ("test", 1))

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_resume_keeps_original_membership_and_rejects_changed_config(self):
        self.session.add(Ticker(id=2, ticker="MSFT", name="Microsoft", exchange="NASDAQ"))
        self.session.commit()
        initialize(self.session, "test", CONFIG)
        self.assertEqual(progress(self.session, "test"), {"pending": 1})
        with self.assertRaises(ValueError):
            initialize(self.session, "test", {**CONFIG, "interval_seconds": 61})

    def test_commits_idempotent_batches_and_preserves_actions(self):
        snapshot = save_export(self.session, self.item, export(), 2, 0)
        save_export(self.session, self.item, export(), 1, 0)
        rows = self.session.exec(select(MarketHistory).order_by(MarketHistory.ts)).all()
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[1].split_ratio, 4)
        self.assertEqual(snapshot.status, "complete")
        self.assertEqual(snapshot.qualification, "source_only_not_simulation_qualified")
        self.assertEqual(self.item.status, "complete")

    def test_partial_snapshot_is_invisible_and_replayed_without_duplicates(self):
        calls = []
        def interrupt():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("worker lost")
        with self.assertRaises(RuntimeError):
            save_export(self.session, self.item, export(), 2, 0, interrupt)
        self.session.rollback()
        snapshot = self.session.exec(select(MarketSnapshot)).one()
        self.assertEqual(snapshot.status, "loading")
        self.assertEqual(len(self.session.exec(select(MarketHistory)).all()), 2)
        save_export(self.session, self.item, export(), 2, 0)
        self.assertEqual(len(self.session.exec(select(MarketHistory)).all()), 4)
        self.assertEqual(snapshot.status, "complete")

    @patch("finance_app_database_service.backfill.fetch_export")
    def test_rate_limit_is_global_persistent_and_does_not_exhaust_failure_budget(self, fetch):
        fetch.side_effect = HTTPError("http://wrapper", 429, "limited", {"Retry-After": "3600"}, None)
        process_item(self.session, self.item, CONFIG)
        gate = self.session.get(MarketBackfillThrottle, 1)
        self.assertGreater((gate.next_request_at - datetime.utcnow()).total_seconds(), 3595)
        self.assertEqual(gate.rate_limit_streak, 1)
        self.assertEqual(self.item.status, "retry")
        self.assertEqual(self.item.failures, 0)
        self.session.close()
        self.session = Session(self.engine)
        self.assertEqual(self.session.get(MarketBackfillThrottle, 1).rate_limit_streak, 1)

    @patch("finance_app_database_service.backfill.fetch_export")
    def test_404_is_a_recorded_gap_not_a_success(self, fetch):
        fetch.side_effect = HTTPError("http://wrapper", 404, "missing", {}, None)
        process_item(self.session, self.item, CONFIG)
        self.assertEqual(self.item.status, "no_data")
        self.assertIsNone(self.item.snapshot_id)

    @patch("finance_app_database_service.backfill.fetch_export")
    def test_next_request_waits_after_response_even_with_slow_dispatch(self, fetch):
        clock = [datetime.utcnow()]
        started = clock[0]
        def delayed_response(*args):
            clock[0] += timedelta(seconds=30)
            return export()
        fetch.side_effect = delayed_response
        with patch("finance_app_database_service.backfill.utcnow", side_effect=lambda: clock[0]):
            process_item(self.session, self.item, CONFIG)
        gate = self.session.get(MarketBackfillThrottle, 1)
        self.assertEqual(gate.next_request_at, started + timedelta(seconds=95))

    @patch("finance_app_database_service.backfill.fetch_export", return_value=export())
    def test_success_does_not_shorten_an_existing_cooldown(self, fetch):
        gate = self.session.get(MarketBackfillThrottle, 1)
        deadline = datetime.utcnow() + timedelta(hours=1)
        gate.next_request_at = deadline
        self.session.add(gate)
        self.session.commit()
        process_item(self.session, self.item, CONFIG)
        self.assertEqual(self.session.get(MarketBackfillThrottle, 1).next_request_at, deadline)

    @patch("finance_app_database_service.backfill.fetch_export")
    def test_transient_errors_have_bounded_retries(self, fetch):
        fetch.side_effect = URLError("unavailable")
        for _ in range(5):
            process_item(self.session, self.item, CONFIG)
        self.assertEqual(self.item.status, "failed")
        self.assertEqual(self.item.failures, 5)

    @patch("finance_app_database_service.backfill.STOP")
    def test_reserves_next_request_before_network_work(self, stop):
        stop.wait.return_value = False
        self.assertTrue(wait_for_slot(self.session, 60))
        gate = self.session.get(MarketBackfillThrottle, 1)
        self.assertGreater((gate.next_request_at - datetime.utcnow()).total_seconds(), 59)

    @patch("finance_app_database_service.backfill.fetch_export", return_value=export())
    def test_lost_lock_stops_before_import(self, fetch):
        def lost():
            raise RuntimeError("lock lost")
        with self.assertRaises(RuntimeError):
            process_item(self.session, self.item, CONFIG, lost)
        self.assertEqual(self.session.exec(select(MarketSnapshot)).all(), [])

    def test_retry_after_supports_seconds_dates_and_invalid_headers(self):
        now = datetime(2021, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(retry_after("120", now), 120)
        self.assertEqual(retry_after("Fri, 01 Jan 2021 00:01:00 GMT", now), 60)
        self.assertEqual(retry_after("invalid", now), 0)
        self.assertEqual(retry_after("NaN", now), 0)
