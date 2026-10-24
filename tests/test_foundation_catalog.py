import json
import unittest
from datetime import date, datetime

from pydantic import ValidationError
from sqlmodel import Session, select

from finance_app_database_service.catalog import bootstrap_catalog
from finance_app_database_service.contracts import ReleaseManifest
from finance_app_database_service.foundation_models import (
    DatasetRelease, Instrument, InstrumentAlias, LegacyTickerMap,
)
from finance_app_database_service.market_models import MarketSnapshot
from finance_app_database_service.models import Ticker
from foundation_support import (
    ORIGIN, client, manifest, migrate, release_row, seed_catalog, settings, sqlite_engine, test_app,
)


class TestFoundationCatalog(unittest.TestCase):
    def setUp(self):
        self.engine = sqlite_engine()
        migrate(self.engine)
        seed_catalog(self.engine)
        self.app = test_app(self.engine)
        self.client = client(self.app)
        with Session(self.engine) as db:
            self.instrument_id = db.get(LegacyTickerMap, 1).instrument_id

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def test_ruleset_financial_values_and_rounding_are_disclosed_strings(self):
        response = self.client.get("/v1/rulesets")
        self.assertEqual(response.status_code, 200, response.text)
        item, = response.json()["items"]
        rules = item["definition"]
        self.assertEqual(rules["initial_margin_rate"], "0.50")
        self.assertEqual(rules["maintenance_margin_rate"], "0.30")
        self.assertEqual(rules["annual_margin_interest_rate"], "0.08")
        self.assertEqual(rules["annual_short_borrow_rate"], "0.03")
        self.assertEqual(rules["execution"], "next_session_open")
        self.assertEqual(rules["money_rounding"], "ROUND_HALF_EVEN")
        self.assertEqual(rules["notional_quantity_rounding"], "ROUND_DOWN")
        self.assertEqual(self.client.get("/v1/rulesets/" + item["id"]).json(), item)
        self.assertEqual(self.client.get("/v1/rulesets/unknown").status_code, 404)

    def test_duplicate_symbols_keep_distinct_stable_ids_and_legacy_mappings(self):
        items = self.client.get("/v1/instruments", params={"query": " aapl "}).json()["items"]
        self.assertEqual(len(items), 2)
        self.assertNotEqual(items[0]["id"], items[1]["id"])
        for item in items:
            self.assertRegex(item["id"], r"^ins_[a-f0-9]{32}$")
            self.assertIsNone(item["currency"])
            self.assertEqual(item["metadata_status"], "unverified")
        detail = self.client.get("/v1/instruments/" + self.instrument_id).json()
        self.assertEqual(detail["legacy_ticker_ids"], [1])
        self.assertEqual(detail["aliases"], ["AAPL"])
        with Session(self.engine) as db:
            before = [(r.ticker_id, r.instrument_id) for r in db.exec(select(LegacyTickerMap)).all()]
            self.assertEqual(bootstrap_catalog(db, execute=True)["mapped"], 0)
            self.assertEqual(before, [(r.ticker_id, r.instrument_id)
                                     for r in db.exec(select(LegacyTickerMap)).all()])

    def test_bootstrap_preview_does_not_write_or_touch_legacy_records(self):
        with Session(self.engine) as db:
            db.add(Ticker(id=4, ticker="MSFT", name="Microsoft", exchange="NASDAQ"))
            db.commit()
            preview = bootstrap_catalog(db)
            self.assertEqual(preview, {"execute": False, "unmapped_tickers": 1, "mapped": 0})
            self.assertIsNone(db.get(LegacyTickerMap, 4))
            self.assertEqual(bootstrap_catalog(db, execute=True, batch_size=1)["mapped"], 1)
            self.assertEqual(db.get(Ticker, 4).ticker, "MSFT")
            with self.assertRaises(ValueError):
                bootstrap_catalog(db, execute=True, batch_size=0)

    def test_keyset_pagination_has_no_duplicate_or_missing_equal_symbol_rows(self):
        all_items = self.client.get("/v1/instruments").json()["items"]
        found, cursor = [], None
        while True:
            page = self.client.get("/v1/instruments", params={
                "limit": 1, **({"cursor": cursor} if cursor else {}),
            }).json()
            found += page["items"]
            cursor = page["next_cursor"]
            if cursor is None:
                break
            self.assertLessEqual(len(found), 3)
        self.assertEqual(found, all_items)

    def test_cursor_rejects_corruption_changed_filters_and_cross_endpoint_reuse(self):
        cursor = self.client.get("/v1/instruments", params={"limit": 1}).json()["next_cursor"]
        for parameters in (
            {"cursor": "not-base64"},
            {"cursor": cursor, "query": "AAPL"},
            {"cursor": cursor, "exchange": "NASDAQ"},
        ):
            response = self.client.get("/v1/instruments", params=parameters)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["code"], "INVALID_CURSOR")
        self.assertEqual(self.client.get("/v1/market-data/releases",
                                         params={"cursor": cursor}).status_code, 422)

    def test_search_includes_aliases_names_and_literal_like_characters(self):
        with Session(self.engine) as db:
            db.add(InstrumentAlias(instrument_id=self.instrument_id, symbol="OLD-AAPL",
                                   source="verified_test_alias"))
            db.commit()
        for query in ("old-aapl", "apple"):
            items = self.client.get("/v1/instruments", params={"query": query}).json()["items"]
            self.assertIn(self.instrument_id, [item["id"] for item in items])
        for query in ("%", "_", "\\"):
            self.assertEqual(self.client.get("/v1/instruments",
                                             params={"query": query}).json()["items"], [])
        items = self.client.get("/v1/instruments", params={"exchange": " nyse "}).json()["items"]
        self.assertEqual([item["symbol"] for item in items], ["BRK-B"])

    def test_invalid_limits_and_missing_instruments_return_problem_documents(self):
        for limit in (0, -1, 101):
            self.assertEqual(self.client.get("/v1/instruments", params={"limit": limit}).status_code, 422)
        response = self.client.get("/v1/instruments/missing")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "INSTRUMENT_NOT_FOUND")

    def test_source_snapshots_and_drafts_never_become_published_game_releases(self):
        with Session(self.engine) as db:
            db.add(MarketSnapshot(
                id="f" * 64, ticker_id=1, ticker="AAPL", source_version="test",
                fetched_at=datetime.utcnow(), exchange_timezone="America/New_York",
                complete_through=date(2020, 1, 3), source_last_date=date(2020, 1, 3),
                row_count=1, status="complete",
            ))
            draft = release_row(manifest(self.instrument_id), qualification="pending")
            db.add(draft)
            db.commit()
            draft_id = draft.id
        self.assertEqual(self.client.get("/v1/market-data/releases").json()["items"], [])
        self.assertEqual(self.client.get("/v1/market-data/releases/" + draft_id).status_code, 404)
        self.assertEqual(self.client.get("/v1/guest-claims").status_code, 405)
        disabled = self.client.post("/v1/games", json={})
        self.assertEqual(disabled.status_code, 503)
        self.assertEqual(disabled.json()["code"], "GAMES_DISABLED")

    def test_release_contract_rejects_unqualified_basis_duplicate_members_and_date_errors(self):
        valid = json.loads(manifest(self.instrument_id).json())
        invalid = []
        for key, value in (
            ("price_basis", "yahoo_split_adjusted"),
            ("qualification", "source_only_not_simulation_qualified"),
            ("from_date", "2020-01-04"),
        ):
            invalid.append({**valid, key: value})
        invalid.append({**valid, "instruments": valid["instruments"] * 2})
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    ReleaseManifest.parse_obj(payload)

    def test_published_release_details_are_content_addressed_and_pagination_is_stable(self):
        ids = []
        with Session(self.engine) as db:
            for version in ("test-1", "test-2", "test-3"):
                row = release_row(manifest(self.instrument_id, calendar_version=version),
                                  published_at=datetime.utcnow())
                ids.append(row.id)
                db.add(row)
            db.commit()
        page = self.client.get("/v1/market-data/releases", params={"limit": 2}).json()
        self.assertEqual([item["id"] for item in page["items"]], sorted(ids)[:2])
        self.assertNotIn("manifest", page["items"][0])
        page2 = self.client.get("/v1/market-data/releases", params={
            "limit": 2, "cursor": page["next_cursor"],
        }).json()
        self.assertEqual([item["id"] for item in page2["items"]], sorted(ids)[2:])
        self.assertIsNone(page2["next_cursor"])
        detail = self.client.get("/v1/market-data/releases/" + ids[0]).json()
        self.assertEqual(detail["id"], "rel_" + detail["sha256"])
        self.assertEqual(detail["manifest"]["instruments"][0]["instrument_id"], self.instrument_id)
        self.assertEqual(detail["price_basis"], "as_traded")

    def test_corrupt_published_release_fails_closed_without_echoing_storage(self):
        with Session(self.engine) as db:
            db.add(DatasetRelease(
                id="rel_" + "e" * 64, manifest_json='{"private":"do-not-echo"}',
                sha256="e" * 64, qualification="simulation_qualified",
                published_at=datetime.utcnow(),
            ))
            db.commit()
        response = self.client.get("/v1/market-data/releases")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "RELEASE_INTEGRITY_ERROR")
        self.assertNotIn("do-not-echo", response.text)

    def test_legacy_read_contracts_still_work_and_writes_require_a_separate_service_token(self):
        response = self.client.get("/tickers")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0], {
            "id": 1, "ticker": "AAPL", "name": "Apple", "exchange": "NASDAQ", "last_updated": None,
        })
        self.assertEqual(self.client.get("/tickers/count").json(), 3)
        self.assertEqual(self.client.get("/history", params={"ticker_name": "AAPL"}).json(), [])
        self.assertEqual(self.client.get("/history", params={"ticker_name": "!!!"}).json(),
                         {"detail": "Invalid ticker symbol"})
        writes = [
            ("POST", "/tickers", {"ticker": "AA", "name": "Alcoa", "exchange": "NYSE"}),
            ("DELETE", "/tickers/1", None),
            ("POST", "/history", {"ticker_id": 1, "ts": "2020-01-02"}),
            ("POST", "/history/batch", []),
            ("POST", "/scraper-runs/start", {"run_key": "test", "scheduled_for": "2020-01-02"}),
            ("POST", "/scraper-runs/fake/progress", {}),
            ("POST", "/scraper-runs/fake/complete", {}),
        ]
        for method, path, payload in writes:
            with self.subTest(path=path):
                denied = self.client.request(method, path, json=payload)
                self.assertEqual(denied.status_code, 401, denied.text)
                wrong = self.client.request(method, path, json=payload,
                                             headers={"Authorization": "Bearer invalid"})
                self.assertEqual(wrong.status_code, 401, wrong.text)
        authorized = self.client.post("/history/batch", json=[], headers={
            "Authorization": "Bearer " + settings().service_token,
        })
        self.assertEqual(authorized.status_code, 201)
        self.assertEqual(authorized.json(), "success")
        self.assertEqual(self.client.post("/v1/guest-sessions", json={},
                                          headers={"Origin": ORIGIN}).status_code, 201)
        self.assertEqual(self.client.post("/history/batch", json=[]).status_code, 401)

    def test_service_writes_fail_closed_when_not_configured(self):
        browser = client(test_app(self.engine, settings(service_token="")))
        try:
            response = browser.post("/history/batch", json=[])
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["code"], "SERVICE_AUTH_NOT_CONFIGURED")
        finally:
            browser.close()

    def test_openapi_documents_new_routes_cookie_auth_and_problem_errors(self):
        schema = self.client.get("/openapi.json").json()
        for path in ("/v1/guest-sessions", "/v1/me", "/v1/rulesets",
                     "/v1/instruments", "/v1/market-data/releases"):
            self.assertIn(path, schema["paths"])
        self.assertIn("/v1/games", schema["paths"])
        self.assertIn("503", schema["paths"]["/v1/games"]["post"]["responses"])
        self.assertEqual(schema["components"]["securitySchemes"]["APIKeyCookie"]["in"], "cookie")
        self.assertIn("application/problem+json",
                      schema["paths"]["/v1/me"]["get"]["responses"]["401"]["content"])
