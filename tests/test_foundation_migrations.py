import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, SQLModel, select

from finance_app_database_service import database
from finance_app_database_service.backfill import initialize
from finance_app_database_service.foundation_models import DatasetRelease, Principal
from finance_app_database_service.market_models import MarketBackfillItem, MarketBackfillThrottle
from finance_app_database_service.models import History, Ticker
from foundation_support import ORIGIN, client, manifest, migrate, release_row, settings, sqlite_engine, test_app
from test_backfill import CONFIG


class TestFoundationMigrations(unittest.TestCase):
    def setUp(self):
        self.engine = sqlite_engine()

    def tearDown(self):
        self.engine.dispose()

    def test_fresh_migration_is_repeatable_and_matches_registered_models(self):
        migrate(self.engine)
        migrate(self.engine)
        with self.engine.connect() as connection:
            context = MigrationContext.configure(connection)
            self.assertEqual(context.get_current_revision(), "0005_google_identity")
            self.assertEqual(compare_metadata(context, SQLModel.metadata), [])

    def test_legacy_startup_does_not_create_foundation_tables(self):
        with patch.object(database, "engine", self.engine):
            database.create_db_and_tables()
        tables = set(inspect(self.engine).get_table_names())
        self.assertIn("market_backfill_throttle", tables)
        self.assertIn("price_history", tables)
        self.assertNotIn("guest_session", tables)
        self.assertNotIn("principal", tables)
        self.assertNotIn("ruleset", tables)
        self.assertNotIn("game", tables)
        self.assertNotIn("qualified_market_revision", tables)
        self.assertNotIn("market_publication", tables)
        self.assertNotIn("oidc_authentication", tables)
        self.assertNotIn("guest_claim", tables)
        self.assertNotIn("identity_audit", tables)

    def test_publication_upgrade_retains_existing_qualified_releases_without_fabricating_approvals(self):
        from finance_app_database_service.game_market import load_market
        from finance_app_database_service.game_models import MarketPublication
        from game_support import seed_release

        migrate(self.engine, "0003_persistent_games")
        release_id = seed_release(self.engine)
        with Session(self.engine) as session:
            before = session.get(DatasetRelease, release_id).dict()
            market_id = load_market(session, release_id).release_id
        migrate(self.engine)
        with Session(self.engine) as session:
            self.assertEqual(session.get(DatasetRelease, release_id).dict(), before)
            self.assertEqual(load_market(session, release_id).release_id, market_id)
            self.assertEqual(session.exec(select(MarketPublication)).all(), [])

    def test_identity_upgrade_preserves_existing_guest_credentials_and_owned_games(self):
        from finance_app_database_service.foundation_models import GuestSession
        from finance_app_database_service.game_contracts import GameCreate
        from finance_app_database_service.game_models import Game
        from finance_app_database_service.game_service import create_game
        from finance_app_database_service.identity_models import GuestClaim, IdentityAudit
        from finance_app_database_service.security import SESSION_COOKIE, session_hash
        from game_support import seed_release

        migrate(self.engine, "0004_market_publication")
        token, now = "g" * 43, datetime.utcnow()
        principal_id, session_id = "prn_" + "a" * 32, "ses_" + "a" * 32
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO principal (id, kind, created_at) VALUES (:id, 'guest', :now)"
            ), {"id": principal_id, "now": now})
            connection.execute(text(
                "INSERT INTO guest_session (id, principal_id, token_hash, created_at, expires_at) "
                "VALUES (:id, :principal, :hash, :now, :expires)"
            ), {"id": session_id, "principal": principal_id, "hash": session_hash(token),
                "now": now, "expires": now + timedelta(days=7)})
        release_id = seed_release(self.engine)
        with Session(self.engine) as session:
            game = create_game(session, principal_id, GameCreate(
                name="Pre-identity game", start_date=date(2020, 1, 2), initial_cash="1000", release_id=release_id,
            ))
            session.commit()
            game_id, before = game.id, game.dict()
        migrate(self.engine)
        with Session(self.engine) as session:
            self.assertEqual(session.get(Game, game_id).dict(), before)
            old = session.get(GuestSession, session_id)
            self.assertEqual(old.token_hash, session_hash(token))
            self.assertIsNone(old.authenticated_at)
            self.assertIsNone(session.get(Principal, principal_id).sessions_revoked_at)
            self.assertEqual(session.exec(select(GuestClaim)).all(), [])
            self.assertEqual(session.exec(select(IdentityAudit)).all(), [])
        browser = client(test_app(self.engine))
        self.addCleanup(browser.close)
        response = browser.get("/v1/me", headers={"Cookie": f"{SESSION_COOKIE}={token}"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["principal_id"], principal_id)
        self.assertEqual(response.json()["kind"], "guest")

    def test_adoption_preserves_existing_history_items_and_backfill_cooldown(self):
        with patch.object(database, "engine", self.engine):
            database.create_db_and_tables()
        with Session(self.engine) as db:
            db.add(Ticker(id=1, ticker="A", name="Agilent", exchange="NYSE"))
            db.commit()
            db.add(History(ticker_id=1, ts=date(2000, 1, 3), volume=123))
            initialize(db, "ongoing", CONFIG)
            item = db.get(MarketBackfillItem, ("ongoing", 1))
            item.status, item.attempts = "retry", 1
            db.add(item)
            gate = db.get(MarketBackfillThrottle, 1)
            gate.next_request_at, gate.rate_limit_streak = datetime(2030, 1, 1), 2
            db.add(gate)
            db.commit()
        migrate(self.engine)
        with Session(self.engine) as db:
            self.assertEqual(db.get(Ticker, 1).ticker, "A")
            self.assertEqual(db.get(History, (1, date(2000, 1, 3))).volume, 123)
            self.assertEqual(db.get(MarketBackfillItem, ("ongoing", 1)).status, "retry")
            self.assertEqual(db.get(MarketBackfillItem, ("ongoing", 1)).attempts, 1)
            gate = db.get(MarketBackfillThrottle, 1)
            self.assertEqual(gate.next_request_at, datetime(2030, 1, 1))
            self.assertEqual(gate.rate_limit_streak, 2)

    def test_database_blocks_ruleset_updates_and_deletes(self):
        migrate(self.engine)
        for statement in (
            "UPDATE ruleset SET definition_json = '{}' WHERE id = 'us-equity-margin-v1'",
            "DELETE FROM ruleset WHERE id = 'us-equity-margin-v1'",
        ):
            with self.assertRaises(DBAPIError):
                with self.engine.begin() as connection:
                    connection.execute(text(statement))

    def test_published_releases_are_immutable_but_drafts_can_publish_once(self):
        migrate(self.engine)
        with Session(self.engine) as db:
            row = release_row(manifest("ins_" + "a" * 32), qualification="pending")
            db.add(row)
            db.commit()
            row.qualification = "simulation_qualified"
            row.published_at = datetime.utcnow()
            db.add(row)
            db.commit()
            release_id = row.id
        for statement in (
            "UPDATE dataset_release SET manifest_json = '{}' WHERE id = :id",
            "DELETE FROM dataset_release WHERE id = :id",
        ):
            with self.assertRaises(DBAPIError):
                with self.engine.begin() as connection:
                    connection.execute(text(statement), {"id": release_id})

    def test_unmigrated_versioned_routes_fail_closed_without_changing_schema(self):
        browser = client(test_app(self.engine))
        try:
            response = browser.get("/v1/rulesets")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["code"], "DATABASE_UNAVAILABLE")
            self.assertEqual(inspect(self.engine).get_table_names(), [])
        finally:
            browser.close()

    def test_creation_quota_is_atomic_across_concurrent_requests(self):
        with tempfile.TemporaryDirectory(prefix="finance-api-quota-") as directory:
            engine = sqlite_engine("sqlite:///" + str(Path(directory) / "quota.sqlite"))
            try:
                migrate(engine)
                app = test_app(engine, settings(guest_creation_limit=2))

                def create(index):
                    browser = client(app)
                    try:
                        return browser.post("/v1/guest-sessions", json={},
                                             headers={"Origin": ORIGIN}).status_code
                    finally:
                        browser.close()

                with ThreadPoolExecutor(max_workers=6) as pool:
                    statuses = list(pool.map(create, range(6)))
                self.assertEqual(sorted(statuses), [201, 201, 429, 429, 429, 429])
                with Session(engine) as db:
                    self.assertEqual(len(db.exec(select(Principal)).all()), 2)
            finally:
                engine.dispose()
