import json
import unittest

from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine

from finance_app_database_service.backfill import initialize, save_export
from finance_app_database_service.market_api import get_backfill, get_market_history
from finance_app_database_service.models import Ticker
from test_backfill import CONFIG
from test_market_import import export
from finance_app_database_service.market_models import MarketBackfillItem


class TestMarketAPI(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.session.add(Ticker(id=1, ticker="AAPL", name="Apple", exchange="NASDAQ"))
        self.session.commit()
        initialize(self.session, "test", CONFIG)
        item = self.session.get(MarketBackfillItem, ("test", 1))
        self.snapshot = save_export(self.session, item, export(), 2, 0)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_reports_backfill_counts(self):
        response = get_backfill("test", session=self.session)
        self.assertEqual(response["counts"], {"complete": 1})

    def test_decimal_strings_stable_pagination_and_no_incomplete_current_day(self):
        page = json.loads(get_market_history(1, limit=2, session=self.session).body)
        self.assertEqual(len(page["items"]), 2)
        self.assertIsInstance(page["items"][0]["open"], str)
        self.assertEqual(page["next_after"], "2020-08-31")
        from datetime import date
        page2 = json.loads(get_market_history(
            1, after=date.fromisoformat(page["next_after"]),
            snapshot_id=self.snapshot.id, limit=2, session=self.session,
        ).body)
        self.assertEqual([r["ts"] for r in page2["items"]], ["2021-01-04"])
        self.assertIsNone(page2["next_after"])

    def test_loading_snapshots_are_not_exposed(self):
        self.snapshot.status = "loading"
        self.session.add(self.snapshot)
        self.session.commit()
        with self.assertRaises(HTTPException) as ctx:
            get_market_history(1, limit=2, session=self.session)
        self.assertEqual(ctx.exception.status_code, 404)
