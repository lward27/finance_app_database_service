"""Real PostgreSQL command serialization, fenced recovery, and audit constraints."""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import func, text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select

from finance_app_database_service.advance_worker import LEASE_SECONDS, LeaseLost, checkpoint, claim
from finance_app_database_service.game_models import AdvanceOperation, Game, GameFill, GameOrder, GamePosting
from finance_app_database_service.game_store import load_state, save_state
from foundation_support import ORIGIN, client, settings, test_app
from game_support import seed_release
from test_simulation import A, DAYS
import test_foundation_postgres as postgres_support


@unittest.skipUnless(os.getenv("FINANCE_TEST_DATABASE_URL"), "Requires disposable PostgreSQL *_test database")
class TestGamePostgres(unittest.TestCase):
    cleanup_schema = postgres_support.TestFoundationPostgres.cleanup_schema

    def setUp(self):
        postgres_support.TestFoundationPostgres.setUp(self)
        self.release_id = seed_release(self.engine)
        self.app = test_app(self.engine, settings(games_enabled=True))
        self.browser = client(self.app)
        self.addCleanup(self.browser.close)
        guest = self.browser.post("/v1/guest-sessions", json={}, headers={"Origin": ORIGIN}).json()
        self.headers = {"Origin": ORIGIN, "X-CSRF-Token": guest["csrf_token"]}
        result = self.browser.post("/v1/games", json={
            "name": "PostgreSQL game", "start_date": str(DAYS[0]), "initial_cash": "1000.12345678",
            "release_id": self.release_id,
        }, headers={**self.headers, "Idempotency-Key": "create"})
        self.assertEqual(result.status_code, 201, result.text)
        self.game_id, self.match = result.json()["id"], result.headers["etag"]

    def parallel_command(self, index, *, same_key, resource="orders"):
        browser = client(self.app)
        browser.cookies.update(self.browser.cookies)
        try:
            headers = {**self.headers, "If-Match": self.match,
                       "Idempotency-Key": "same-command" if same_key else f"command-{index}"}
            body = {"instrument_id": A, "action": "buy", "quantity": "1"} if resource == "orders" else {
                "to_date": str(DAYS[-1]),
            }
            response = browser.post(f"/v1/games/{self.game_id}/{resource}", json=body, headers=headers)
            return response.status_code, response.json(), response.headers.get("etag")
        finally:
            browser.close()

    def queue_advance(self):
        response = self.browser.post(f"/v1/games/{self.game_id}/advances", json={
            "to_date": str(DAYS[-1]),
        }, headers={**self.headers, "If-Match": self.match, "Idempotency-Key": "advance"})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["id"]

    def test_concurrent_duplicate_orders_replay_one_result_and_one_revision(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda index: self.parallel_command(index, same_key=True), range(6)))
        self.assertTrue(all(result[0] == 201 for result in results), results)
        self.assertTrue(all(result == results[0] for result in results))
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(func.count()).select_from(GameOrder)).one(), 1)
            self.assertEqual(session.get(Game, self.game_id).revision, 2)

    def test_concurrent_distinct_orders_cannot_both_use_the_same_revision(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda index: self.parallel_command(index, same_key=False), range(2)))
        self.assertEqual(sorted(item[0] for item in results), [201, 412])
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(func.count()).select_from(GameOrder)).one(), 1)

    def test_concurrent_advances_admit_one_operation_and_unique_index_is_defensive(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda index: self.parallel_command(index, same_key=False, resource="advances"), range(2),
            ))
        self.assertEqual(sorted(item[0] for item in results), [202, 412])
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(func.count()).select_from(AdvanceOperation)).one(), 1)
            operation = session.exec(select(AdvanceOperation)).one()
            copy = operation.dict(exclude={"id"})
            session.add(AdvanceOperation(**copy))
            with self.assertRaises(DBAPIError):
                session.commit()

    def test_only_one_worker_claims_and_stale_claim_cannot_commit(self):
        self.queue_advance()
        now = datetime(2030, 1, 1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: claim(self.engine, now=now), range(4)))
        leases = [item for item in results if item]
        self.assertEqual(len(leases), 1)
        operation_id, token = leases[0]
        self.assertFalse(checkpoint(self.engine, operation_id, token, now=now))
        new_id, new_token = claim(self.engine, now=now + timedelta(seconds=LEASE_SECONDS + 1))
        self.assertEqual(new_id, operation_id)
        with self.assertRaises(LeaseLost):
            checkpoint(self.engine, operation_id, token, now=now + timedelta(seconds=LEASE_SECONDS + 2))
        self.assertFalse(checkpoint(self.engine, operation_id, new_token,
                                    now=now + timedelta(seconds=LEASE_SECONDS + 2)))
        with Session(self.engine) as session:
            self.assertEqual(session.get(Game, self.game_id).cursor, DAYS[2])

    def test_real_postgres_checkpoint_rollback_and_decimal_postings_are_exact(self):
        ordered = self.browser.post(f"/v1/games/{self.game_id}/orders", json={
            "instrument_id": A, "action": "buy", "quantity": "1.23456789",
        }, headers={**self.headers, "If-Match": self.match, "Idempotency-Key": "order"})
        self.assertEqual(ordered.status_code, 201, ordered.text)
        self.match = ordered.headers["etag"]
        self.queue_advance()
        operation_id, token = claim(self.engine)

        def crash(session, game, state, previous):
            save_state(session, game, state, previous)
            session.flush()
            raise RuntimeError("simulated pre-commit failure")

        with patch("finance_app_database_service.advance_worker.save_state", side_effect=crash):
            with self.assertRaises(RuntimeError):
                checkpoint(self.engine, operation_id, token)
        with Session(self.engine) as session:
            self.assertEqual(session.get(Game, self.game_id).cursor, DAYS[0])
            self.assertEqual(session.exec(select(func.count()).select_from(GameFill)).one(), 0)
        self.assertFalse(checkpoint(self.engine, operation_id, token))
        with Session(self.engine) as session:
            state = load_state(session.get(Game, self.game_id))
            self.assertEqual(state.book.cash, Decimal("876.66666778"))
            amounts = session.exec(select(GamePosting.amount).where(
                GamePosting.game_id == self.game_id, GamePosting.ledger_sequence == 2,
            )).all()
            self.assertEqual(sorted(amounts), [Decimal("-123.45678900"), Decimal("123.45678900")])
            self.assertEqual(sum(amounts), 0)
        for statement in ("UPDATE game_posting SET amount = 0", "DELETE FROM game_fill",
                          "UPDATE game_snapshot SET body_json = '{}'", "DELETE FROM game_command"):
            with self.subTest(statement=statement), self.assertRaises(DBAPIError) as caught:
                with self.engine.begin() as connection:
                    connection.execute(text(statement))
            self.assertEqual(caught.exception.orig.pgcode, "55000")
