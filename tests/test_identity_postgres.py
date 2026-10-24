"""Opt-in identity races in private schemas of the existing disposable test database."""

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import func, text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select

from finance_app_database_service import security
from finance_app_database_service.foundation_models import GuestSession, Principal
from finance_app_database_service.game_models import Game, GameCommand
from finance_app_database_service.identity_models import GuestClaim, IdentityAudit
from foundation_support import ORIGIN
from game_support import seed_release
from identity_support import IdentityHarness
import test_foundation_postgres as postgres_support
import test_identity_api as identity_tests


@unittest.skipUnless(os.getenv("FINANCE_TEST_DATABASE_URL"), "Requires disposable PostgreSQL *_test database")
class TestIdentityPostgres(IdentityHarness, unittest.TestCase):
    cleanup_schema = postgres_support.TestFoundationPostgres.cleanup_schema

    def setUp(self):
        postgres_support.TestFoundationPostgres.setUp(self)
        self.release_id = seed_release(self.engine)
        self.configure_identity()

    def test_concurrent_first_logins_create_one_account_with_separate_sessions(self):
        browsers = [self.new_browser() for _ in range(6)]
        for browser in browsers:
            self.verify(self.begin(browser), browser)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(self.finish, browsers))
        self.assertTrue(all(response.status_code == 200 for response in results),
                        [(response.status_code, response.text) for response in results])
        self.assertEqual(len({response.json()["identity"]["principal_id"] for response in results}), 1)
        with Session(self.engine) as db:
            self.assertEqual(db.exec(select(func.count()).select_from(Principal)).one(), 1)
            self.assertEqual(db.exec(select(func.count()).select_from(GuestSession)).one(), 6)

    def test_duplicate_claim_confirmations_have_one_transfer_receipt_and_account_session(self):
        self.guest()
        game = self.game().json()["id"]
        self.verify(self.begin(claim=True))
        browsers = [self.new_browser() for _ in range(5)]
        for browser in browsers:
            browser.cookies.update(self.browser.cookies)
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(self.finish, browsers))
        self.assertTrue(all(response.status_code == 200 for response in results),
                        [(response.status_code, response.text) for response in results])
        self.assertTrue(all(response.json() == results[0].json() for response in results))
        with Session(self.engine) as db:
            self.assertEqual(db.exec(select(func.count()).select_from(GuestClaim)).one(), 1)
            self.assertEqual(db.exec(select(func.count()).select_from(IdentityAudit)).one(), 1)
            self.assertEqual(db.exec(select(func.count()).select_from(GuestSession)).one(), 2)
            self.assertEqual(db.get(Game, game).revision, 2)

    def test_competing_google_accounts_cannot_both_claim_one_guest(self):
        guest = self.guest()
        game = self.game().json()["id"]
        self.verify(self.begin(claim=True), subject="claim-target-a")
        other = self.new_browser()
        other.cookies.update(self.browser.cookies)
        self.verify(self.begin(other, claim=True), other, "claim-target-b")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.finish, [self.browser, other]))
        self.assertEqual(sorted(response.status_code for response in results), [200, 401])
        winner = next(response.json()["identity"]["principal_id"] for response in results if response.status_code == 200)
        with Session(self.engine) as db:
            receipt = db.get(GuestClaim, guest["principal_id"])
            self.assertEqual(receipt.account_principal_id, winner)
            self.assertEqual(db.get(Game, game).owner_id, winner)
            self.assertEqual(db.exec(select(func.count()).select_from(Principal)).one(), 2)

    def test_already_authenticated_guest_write_is_rechecked_after_concurrent_claim(self):
        self.guest()
        game = self.game()
        game_id = game.json()["id"]
        self.verify(self.begin(claim=True))
        writer = self.new_browser()
        writer.cookies.update(self.browser.cookies)
        headers = {**self.headers(writer), "Idempotency-Key": "late-guest-write", "If-Match": game.headers["etag"]}
        ready, release = threading.Event(), threading.Event()
        original = security.lock_session_owner

        def paused(db, current):
            ready.set()
            if not release.wait(10):
                raise AssertionError("Timed out waiting for claim")
            return original(db, current)

        with patch.object(security, "lock_session_owner", side_effect=paused), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writer.patch, "/v1/games/" + game_id,
                                 json={"name": "Late unauthorized rename"}, headers=headers)
            try:
                self.assertTrue(ready.wait(10))
                result = self.finish()
                self.assertEqual(result.status_code, 200, result.text)
            finally:
                release.set()
            late = future.result(timeout=15)
        self.assertEqual(late.status_code, 401, late.text)
        with Session(self.engine) as db:
            self.assertEqual(db.get(Game, game_id).name, "Synthetic identity game")
            self.assertEqual(db.exec(select(func.count()).select_from(GameCommand)).one(), 1)

    def test_global_revocation_serializes_with_anonymous_completion_and_leaves_no_live_session(self):
        self.login()
        pending = self.new_browser()
        self.verify(self.begin(pending), pending)
        headers = self.headers()
        barrier = threading.Barrier(2)

        def revoke():
            barrier.wait(timeout=10)
            return self.browser.post("/v1/session-revocations", json={"scope": "all"}, headers=headers)

        def finish():
            barrier.wait(timeout=10)
            return self.finish(pending)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(revoke), pool.submit(finish)
            revoked, completed = first.result(timeout=20), second.result(timeout=20)
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertIn(completed.status_code, (200, 409), completed.text)
        with Session(self.engine) as db:
            count = db.exec(select(func.count()).select_from(GuestSession).where(
                GuestSession.revoked_at.is_(None), GuestSession.expires_at > datetime.utcnow(),
            )).one()
            self.assertEqual(count, 0)
        self.assertEqual(pending.get("/v1/me").status_code, 401)

    def test_authentication_start_quota_is_atomic_across_replicas(self):
        browsers = [self.new_browser() for _ in range(24)]
        def start(browser):
            return browser.post("/v1/oidc-authentications", json={}, headers={"Origin": ORIGIN}).status_code
        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(start, browsers))
        self.assertEqual(sorted(statuses), [201] * 20 + [429] * 4)

    def test_database_blocks_mutating_claim_and_identity_audit(self):
        self.guest()
        self.login(claim=True)
        for table in ("guest_claim", "identity_audit"):
            for query in (f"UPDATE {table} SET id = id", f"DELETE FROM {table}"):
                with self.subTest(query=query), self.assertRaises(DBAPIError) as caught:
                    with self.engine.begin() as connection:
                        connection.execute(text(query))
                self.assertEqual(caught.exception.orig.pgcode, "55000")

    test_real_worker_claim_is_blocked_until_advance_finishes = (
        identity_tests.TestGoogleIdentityAPI.test_active_advance_blocks_claim_atomically_and_finished_worker_can_be_claimed
    )
