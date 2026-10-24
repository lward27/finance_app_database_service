"""Opt-in real PostgreSQL checks in a private schema of a disposable *_test DB."""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, SQLModel, create_engine, select

from finance_app_database_service.foundation_models import Principal
from finance_app_database_service.accounting import fill, funded_book, reconcile
from finance_app_database_service.financial import money
from foundation_support import ORIGIN, client, manifest, migrate, release_row, settings, test_app

TEST_URL = os.getenv("FINANCE_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_URL, "Set FINANCE_TEST_DATABASE_URL to a disposable PostgreSQL *_test database")
class TestFoundationPostgres(unittest.TestCase):
    def setUp(self):
        url = make_url(TEST_URL)
        if url.get_backend_name() != "postgresql" or not (url.database or "").endswith("_test"):
            raise RuntimeError("PostgreSQL integration tests require a dedicated *_test database")
        self.schema = "finance_api_test_" + uuid4().hex
        self.control = create_engine(url)
        with self.control.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_engine(
            url, connect_args={"options": f"-csearch_path={self.schema}"},
            pool_size=8, max_overflow=0,
        )
        self.addCleanup(self.cleanup_schema)
        migrate(self.engine)

    def cleanup_schema(self):
        self.engine.dispose()
        try:
            with self.control.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        finally:
            self.control.dispose()

    def test_real_postgres_migrations_are_repeatable_and_match_models(self):
        migrate(self.engine)
        with self.engine.connect() as connection:
            context = MigrationContext.configure(connection)
            self.assertEqual(context.get_current_revision(), "0005_google_identity")
            self.assertEqual(compare_metadata(context, SQLModel.metadata), [])

    def test_real_postgres_guards_reject_ruleset_and_published_release_mutations(self):
        with Session(self.engine) as db:
            row = release_row(manifest("ins_" + "a" * 32), published_at=datetime.utcnow())
            db.add(row)
            db.commit()
        for statement in (
            "UPDATE ruleset SET definition_json = '{}'",
            "DELETE FROM ruleset",
            "UPDATE dataset_release SET manifest_json = '{}'",
            "DELETE FROM dataset_release",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(DBAPIError) as caught:
                    with self.engine.begin() as connection:
                        connection.execute(text(statement))
                self.assertEqual(caught.exception.orig.pgcode, "55000")

    def test_real_postgres_creation_quota_serializes_concurrent_requests(self):
        app = test_app(self.engine, settings(guest_creation_limit=2))

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
        with Session(self.engine) as db:
            self.assertEqual(len(db.exec(select(Principal)).all()), 2)

    def test_real_postgres_numeric_round_trips_balanced_simulation_postings(self):
        book = funded_book(Decimal("1000.12345678"))
        book = fill(book, "short", "test", "sell_short",
                    Decimal("1.23456789"), Decimal("12.34567891"))
        book = fill(book, "cover", "test", "buy_to_cover",
                    Decimal("0.12345678"), Decimal("10.12345678"))
        reconcile(book)
        postings = [
            dict(key=entry.key, account=posting.account, amount=posting.amount)
            for entry in book.ledger for posting in entry.postings
        ]
        with self.engine.begin() as connection:
            connection.execute(text(
                "CREATE TEMP TABLE simulation_posting_probe "
                "(key TEXT NOT NULL, account TEXT NOT NULL, amount NUMERIC(28,8) NOT NULL) "
                "ON COMMIT DROP"
            ))
            connection.execute(text(
                "INSERT INTO simulation_posting_probe VALUES (:key, :account, :amount)"
            ), postings)
            stored = connection.execute(text(
                "SELECT key, account, amount FROM simulation_posting_probe ORDER BY key, account"
            )).all()
            self.assertEqual(
                [tuple(row) for row in stored],
                sorted((p["key"], p["account"], p["amount"]) for p in postings),
            )
            sums = connection.execute(text(
                "SELECT SUM(amount) FROM simulation_posting_probe GROUP BY key"
            )).scalars().all()
            self.assertTrue(all(amount == Decimal("0") for amount in sums))
            # PostgreSQL's own tie rounding differs; bind the engine's already
            # half-even-quantized amount instead of delegating policy to SQL.
            rounded = connection.execute(
                text("SELECT CAST(:amount AS NUMERIC(28,8))"),
                {"amount": money(Decimal("1.000000005"))},
            ).scalar_one()
            self.assertEqual(rounded, Decimal("1.00000000"))
