import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import func, text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select

from finance_app_database_service.advance_worker import checkpoint, claim
from finance_app_database_service.contracts import content_digest
from finance_app_database_service.foundation_models import GuestSession, Principal
from finance_app_database_service.game_models import Game, GameCommand, GameEvent, GameSnapshot
from finance_app_database_service.google_identity import FLOW_COOKIE, GOOGLE_TOKEN_URL
from finance_app_database_service.identity_models import GuestClaim, IdentityAudit, OIDCAuthentication
from finance_app_database_service.security import SESSION_COOKIE, session_hash
from foundation_support import ORIGIN, migrate, sqlite_engine
from game_support import seed_release
from identity_support import IdentityHarness, google_settings
from test_simulation import DAYS


class TestGoogleIdentityAPI(IdentityHarness, unittest.TestCase):
    def setUp(self):
        self.engine = sqlite_engine()
        self.addCleanup(self.engine.dispose)
        migrate(self.engine)
        self.release_id = seed_release(self.engine)
        self.configure_identity()

    def test_disabled_by_default_and_openapi_documents_frontend_contract(self):
        disabled = self.new_browser()
        disabled.app.state.security_settings = google_settings(google_auth_enabled=False)
        self.assertEqual(disabled.get("/v1/authentication").json()["google_enabled"], False)
        result = disabled.post("/v1/oidc-authentications", json={}, headers={"Origin": ORIGIN})
        self.assertEqual(result.status_code, 503, result.text)
        self.assertEqual(result.json()["code"], "OIDC_DISABLED")
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertEqual(self.fake.calls, [])
        paths = disabled.get("/openapi.json").json()["paths"]
        for path in ("/v1/oidc-authentications", "/v1/guest-claims", "/v1/sessions",
                     "/v1/session-revocations", "/v1/oidc-authentications/current/completion"):
            self.assertIn(path, paths)

    def test_start_sets_private_cookie_and_only_hashed_state_not_pkce_or_nonce_is_persisted(self):
        response = self.browser.post("/v1/oidc-authentications", json={}, headers={"Origin": ORIGIN})
        self.assertEqual(response.status_code, 201, response.text)
        for attribute in ("Secure", "HttpOnly", "SameSite=lax", "Path=/", "Max-Age=600"):
            self.assertIn(attribute, response.headers["set-cookie"])
        self.assertNotIn("Domain=", response.headers["set-cookie"])
        params = parse_qs(urlsplit(response.json()["authorization_url"]).query)
        self.assertEqual(params["scope"], ["openid"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertEqual(params["redirect_uri"], [self.app.state.security_settings.google_redirect_uri])
        with Session(self.engine) as db:
            row = db.exec(select(OIDCAuthentication)).one()
            self.assertEqual(row.state_hash, session_hash(params["state"][0]))
            for value in (self.browser.cookies.get(FLOW_COOKIE), params["state"][0], params["nonce"][0]):
                self.assertNotIn(value, row.json())
            self.assertNotIn("code_verifier", row.json())
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    def test_anonymous_sign_in_requires_verified_completion_and_recovers_same_account(self):
        url = self.begin()
        self.assertEqual(self.finish().status_code, 409)
        self.verify(url)
        self.assertEqual(self.browser.get("/v1/me").status_code, 401)
        response = self.finish()
        self.assertEqual(response.status_code, 200, response.text)
        account = response.json()["identity"]
        self.assertEqual(account["kind"], "registered")
        self.assertFalse(account["capabilities"]["claim_guest"])
        token = self.browser.cookies.get(SESSION_COOKIE)
        self.assertIsNone(self.browser.cookies.get(FLOW_COOKIE))
        with Session(self.engine) as db:
            row = db.exec(select(GuestSession)).one()
            self.assertEqual(row.token_hash, session_hash(token))
            self.assertNotIn(token, row.json())
        other = self.new_browser()
        recovered = self.login(other)["identity"]
        self.assertEqual(recovered["principal_id"], account["principal_id"])
        self.assertNotEqual(other.cookies.get(SESSION_COOKIE), token)
        self.assertEqual(len(self.browser.get("/v1/sessions").json()["items"]), 2)
        self.assertEqual(self.browser.post("/v1/guest-sessions", json={},
                                          headers={"Origin": ORIGIN}).status_code, 409)

    def test_email_never_links_distinct_subjects_and_issuer_alias_recovers_one_account(self):
        first = self.login()["identity"]
        second = self.new_browser()
        self.verify(self.begin(second), second, changes={"iss": "accounts.google.com", "email": "same@example.invalid"})
        response = self.finish(second)
        self.assertEqual(response.json()["identity"]["principal_id"], first["principal_id"])
        third = self.new_browser()
        self.verify(self.begin(third), third, "different-subject", changes={"email": "same@example.invalid"})
        self.assertNotEqual(self.finish(third).json()["identity"]["principal_id"], first["principal_id"])

    def test_guest_sign_in_requires_explicit_claim_and_cannot_override_an_owner(self):
        guest = self.guest()
        token = self.browser.cookies.get(SESSION_COOKIE)
        response = self.browser.post("/v1/oidc-authentications", json={}, headers=self.headers())
        self.assertEqual(response.json()["code"], "EXPLICIT_GUEST_CLAIM_REQUIRED")
        for body in ({"principal_id": "prn_other"}, {"return_url": "https://evil.test"},
                     {"issuer": "https://evil.test"}):
            denied = self.browser.post("/v1/guest-claims", json=body, headers=self.headers())
            self.assertEqual(denied.status_code, 422)
        self.assertEqual(self.browser.cookies.get(SESSION_COOKIE), token)
        self.assertEqual(self.browser.get("/v1/me").json()["principal_id"], guest["principal_id"])

    def test_claim_preserves_simulation_and_receipts_rotates_session_and_retires_guest(self):
        guest = self.guest()
        original_token = self.browser.cookies.get(SESSION_COOKIE)
        game = self.game(key="original-guest-command")
        game_id = game.json()["id"]
        with Session(self.engine) as db:
            before = db.get(Game, game_id).dict()
            commands = [row.dict() for row in db.exec(select(GameCommand)).all()]
            events = [row.dict() for row in db.exec(select(GameEvent)).all()]
            snapshots = [row.dict() for row in db.exec(select(GameSnapshot)).all()]
        result = self.login(claim=True)
        self.assertEqual(result["claimed_games"], 1)
        self.assertTrue(result["claim_id"].startswith("clm_"))
        account_id = result["identity"]["principal_id"]
        self.assertNotEqual(account_id, guest["principal_id"])
        self.assertNotEqual(original_token, self.browser.cookies.get(SESSION_COOKIE))
        with Session(self.engine) as db:
            after = db.get(Game, game_id).dict()
            for name in before:
                if name not in ("owner_id", "revision"):
                    self.assertEqual(after[name], before[name], name)
            self.assertEqual(after["owner_id"], account_id)
            self.assertEqual(after["revision"], before["revision"] + 1)
            self.assertEqual([row.dict() for row in db.exec(select(GameCommand)).all()], commands)
            self.assertEqual([row.dict() for row in db.exec(select(GameEvent)).all()], events)
            self.assertEqual([row.dict() for row in db.exec(select(GameSnapshot)).all()], snapshots)
            receipt = db.get(GuestClaim, guest["principal_id"])
            self.assertEqual(receipt.games_sha256, content_digest(receipt.games_json))
            self.assertEqual(db.get(Principal, guest["principal_id"]).kind, "guest")
        old = self.new_browser()
        old.cookies.set(SESSION_COOKIE, original_token, domain="api.test", path="/")
        self.assertEqual(old.get("/v1/me").status_code, 401)
        self.assertEqual(old.get("/v1/games/" + game_id).status_code, 401)
        current = self.browser.get("/v1/games/" + game_id)
        self.assertEqual(current.status_code, 200)
        self.assertNotEqual(current.headers["etag"], game.headers["etag"])
        # Registered writes retain the same CSRF/Origin/idempotency/revision boundary.
        renamed = self.browser.patch("/v1/games/" + game_id, json={"name": "Recovered"},
            headers={**self.headers(), "Idempotency-Key": "registered-rename", "If-Match": current.headers["etag"]})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(self.browser.post("/history/batch", json=[]).status_code, 401)

    def test_claim_merges_into_existing_account_without_copying_guest_command_keys(self):
        account_browser = self.new_browser()
        account = self.login(account_browser)["identity"]
        account_game = self.game(account_browser, key="overlapping-key").json()["id"]
        guest = self.guest()
        guest_game = self.game(key="overlapping-key").json()["id"]
        result = self.login(claim=True)
        self.assertEqual(result["identity"]["principal_id"], account["principal_id"])
        self.assertEqual({row["id"] for row in self.browser.get("/v1/games").json()["items"]},
                         {account_game, guest_game})
        replay = self.game(key="overlapping-key")
        self.assertEqual(replay.json()["id"], account_game)
        with Session(self.engine) as db:
            self.assertEqual(db.exec(select(func.count()).select_from(GameCommand)).one(), 2)
            self.assertIsNotNone(db.get(GuestClaim, guest["principal_id"]))

    def test_wrong_state_cross_browser_duplicate_callback_and_bad_nonce_cannot_login(self):
        url = self.begin()
        params = self.fake.issue(url)
        wrong = self.browser.get("/v1/oidc/callback", params={**params, "state": "x" * 43})
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(self.new_browser().get("/v1/oidc/callback", params=params).status_code, 401)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.browser.get("/v1/oidc/callback", params=params,
                                          follow_redirects=False).status_code, 303)
        self.assertEqual(self.browser.get("/v1/oidc/callback", params=params).status_code, 409)
        self.assertEqual(len([url for url, _ in self.fake.calls if url == GOOGLE_TOKEN_URL]), 1)
        bad = self.new_browser()
        self.verify(self.begin(bad), bad, changes={"nonce": "wrong"})
        self.assertEqual(bad.get("/v1/oidc-authentications/current").json()["status"], "failed")
        self.assertEqual(self.finish(bad).status_code, 409)
        self.assertEqual(bad.get("/v1/me").status_code, 401)

    def test_callback_query_credentials_are_hidden_and_error_redirect_is_fixed(self):
        url = self.begin()
        params = self.fake.issue(url)
        duplicate = self.browser.get("/v1/oidc/callback",
            params=[("state", params["state"]), ("state", "other"), ("code", params["code"])])
        self.assertEqual(duplicate.status_code, 400)
        denied = self.browser.get("/v1/oidc/callback", params={
            "state": params["state"], "error": "access_denied", "error_description": "private-upstream-detail",
        }, follow_redirects=False)
        self.assertEqual(denied.status_code, 303)
        self.assertEqual(denied.headers["location"], ORIGIN + "/auth/complete")
        self.assertNotIn("private-upstream-detail", denied.text)
        self.assertEqual(denied.headers["referrer-policy"], "no-referrer")
        self.assertEqual(denied.headers["cache-control"], "no-store")
        view = self.browser.get("/v1/oidc-authentications/current").json()
        self.assertEqual((view["status"], view["code"]), ("failed", "OIDC_CANCELLED"))
        self.assertEqual(self.fake.calls, [])

    def test_provider_failures_are_sanitized_and_code_is_consumed_before_exchange(self):
        url = self.begin()
        params = self.fake.issue(url)
        with patch.object(self.app.state.google_client, "exchange",
                          side_effect=RuntimeError("private-provider-token-and-secret")):
            response = self.browser.get("/v1/oidc/callback", params=params, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.browser.get("/v1/oidc/callback", params=params).status_code, 409)
        view = self.browser.get("/v1/oidc-authentications/current")
        self.assertNotIn("private-provider", view.text)
        self.assertEqual(view.json()["code"], "OIDC_PROVIDER_UNAVAILABLE")
        with Session(self.engine) as db:
            self.assertIsNotNone(db.exec(select(OIDCAuthentication)).one().consumed_at)
            self.assertEqual(db.exec(select(GuestSession)).all(), [])

    def test_completion_requires_flow_specific_csrf_and_origin_without_account_writes(self):
        self.verify(self.begin())
        csrf = self.browser.get("/v1/oidc-authentications/current").json()["csrf_token"]
        for headers in ({}, {"Origin": ORIGIN}, {"Origin": "https://evil.test", "X-CSRF-Token": csrf},
                        {"Origin": ORIGIN, "X-CSRF-Token": "wrong"}):
            response = self.browser.post("/v1/oidc-authentications/current/completion", json={}, headers=headers)
            self.assertEqual(response.status_code, 403, response.text)
        with Session(self.engine) as db:
            self.assertEqual(db.exec(select(Principal)).all(), [])
        self.assertEqual(self.finish().status_code, 200)

    def test_changed_cookie_and_revoked_source_block_claim_after_verification(self):
        self.guest()
        self.verify(self.begin(claim=True))
        other = self.new_browser()
        self.guest(other)
        other.cookies.set(FLOW_COOKIE, self.browser.cookies.get(FLOW_COOKIE), domain="api.test", path="/")
        self.assertEqual(self.finish(other).json()["code"], "OIDC_SESSION_CHANGED")
        with Session(self.engine) as db:
            source = db.exec(select(GuestSession).where(
                GuestSession.token_hash == session_hash(self.browser.cookies.get(SESSION_COOKIE)),
            )).one()
            source.revoked_at = datetime.utcnow()
            db.add(source)
            db.commit()
        self.assertEqual(self.finish().status_code, 401)
        with Session(self.engine) as db:
            self.assertEqual(db.exec(select(GuestClaim)).all(), [])
            self.assertEqual(db.exec(select(Principal).where(Principal.kind == "registered")).all(), [])

    def test_expired_flow_and_changed_configuration_fail_closed(self):
        self.verify(self.begin())
        with Session(self.engine) as db:
            flow = db.exec(select(OIDCAuthentication)).one()
            flow.created_at = datetime.utcnow() - timedelta(hours=1)
            flow.expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.add(flow)
            db.commit()
        self.assertEqual(self.browser.get("/v1/oidc-authentications/current").json()["status"], "none")
        self.assertEqual(self.finish().status_code, 401)
        self.begin()
        self.app.state.security_settings = google_settings(google_completion_uri=ORIGIN + "/changed")
        self.assertEqual(self.browser.get("/v1/oidc-authentications/current").json()["status"], "none")

    def test_completion_retry_reissues_same_session_without_repeating_claim_or_resurrecting_revocation(self):
        self.guest()
        self.game()
        self.verify(self.begin(claim=True))
        retry = self.new_browser()
        retry.cookies.update(self.browser.cookies)
        response = self.finish()
        repeated = self.finish(retry)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(repeated.json(), response.json())
        self.assertEqual(retry.cookies.get(SESSION_COOKIE), self.browser.cookies.get(SESSION_COOKIE))
        with Session(self.engine) as db:
            self.assertEqual(db.exec(select(func.count()).select_from(GuestClaim)).one(), 1)
            self.assertEqual(db.exec(select(func.count()).select_from(IdentityAudit)).one(), 1)
        # Restore the short-lived flow cookie as if a completion response was lost.
        with Session(self.engine) as db:
            flow_id = db.exec(select(OIDCAuthentication.id)).one()
        from finance_app_database_service.google_identity import derived_token
        replay = self.new_browser()
        # The original browser binding remains in the retry request's initial cookies.
        old_binding = response.request.headers.get("cookie", "").split(FLOW_COOKIE + "=", 1)[1].split(";", 1)[0]
        replay.cookies.set(FLOW_COOKIE, old_binding, domain="api.test", path="/")
        replay.cookies.set(SESSION_COOKIE, derived_token(old_binding, "session:" + flow_id), domain="api.test", path="/")
        self.assertEqual(self.browser.delete("/v1/sessions/current", headers=self.headers()).status_code, 204)
        self.assertEqual(self.finish(replay).json()["code"], "OIDC_SESSION_CHANGED")

    def test_active_advance_blocks_claim_atomically_and_finished_worker_can_be_claimed(self):
        self.guest()
        game = self.game()
        self.verify(self.begin(claim=True))
        operation = self.browser.post(f"/v1/games/{game.json()['id']}/advances", json={"to_date": str(DAYS[-1])},
            headers={**self.headers(), "If-Match": game.headers["etag"], "Idempotency-Key": "claim-race-advance"})
        self.assertEqual(operation.status_code, 202, operation.text)
        blocked = self.finish()
        self.assertEqual(blocked.json()["code"], "ADVANCE_IN_PROGRESS")
        with Session(self.engine) as db:
            self.assertEqual(db.exec(select(GuestClaim)).all(), [])
            self.assertEqual(db.exec(select(Principal).where(Principal.kind == "registered")).all(), [])
        leased_id, token = claim(self.engine)
        for _ in range(len(DAYS)):
            if checkpoint(self.engine, leased_id, token):
                break
        else:
            self.fail("Synthetic advance did not finish")
        self.assertEqual(self.finish().status_code, 200)
        self.assertEqual(self.browser.get(f"/v1/games/{game.json()['id']}").json()["cursor"], str(DAYS[-1]))

    def test_claim_cannot_bypass_account_game_quota_and_leaves_guest_recoverable(self):
        account_browser = self.new_browser()
        self.login(account_browser)
        for _ in range(10):
            self.game(account_browser)
        guest = self.guest()
        game = self.game().json()["id"]
        self.verify(self.begin(claim=True))
        response = self.finish()
        self.assertEqual(response.json()["code"], "GAME_LIMIT_REACHED")
        self.assertEqual(self.browser.get("/v1/me").json()["principal_id"], guest["principal_id"])
        self.assertEqual(self.browser.get("/v1/games/" + game).status_code, 200)
        with Session(self.engine) as db:
            self.assertIsNone(db.get(GuestClaim, guest["principal_id"]))

    def test_claim_and_identity_audit_are_immutable_and_old_guest_cannot_reenter_commands(self):
        guest = self.guest()
        self.login(claim=True)
        for table in ("guest_claim", "identity_audit"):
            for operation in ("UPDATE " + table + " SET id = id", "DELETE FROM " + table):
                with self.subTest(operation=operation), self.assertRaises(DBAPIError):
                    with self.engine.begin() as connection:
                        connection.execute(text(operation))
        from finance_app_database_service.game_contracts import EmptyCommand
        from finance_app_database_service.game_service import command
        from finance_app_database_service.problems import Problem
        with Session(self.engine) as db, self.assertRaises(Problem) as caught:
            command(db, guest["principal_id"], "late-command", "late", EmptyCommand(), None, None,
                    lambda game: self.fail("Retired guest handler ran"))
        self.assertEqual(caught.exception.status, 401)

    def test_durable_start_limit_applies_without_storing_client_addresses(self):
        for index in range(21):
            result = self.browser.post("/v1/oidc-authentications", json={}, headers={"Origin": ORIGIN})
            self.assertEqual(result.status_code, 201 if index < 20 else 429, result.text)
        self.assertGreater(int(result.headers["retry-after"]), 0)
        self.assertEqual(self.fake.calls, [])

    def test_claim_deadline_is_rechecked_before_commit_and_every_change_rolls_back(self):
        from finance_app_database_service import identity_service
        guest = self.guest()
        game_id = self.game().json()["id"]
        self.verify(self.begin(claim=True))
        original = identity_service.claim_games
        def expire_after_claim(db, source, target, flow, account):
            receipt = original(db, source, target, flow, account)
            flow.created_at = datetime.utcnow() - timedelta(hours=1)
            flow.expires_at = datetime.utcnow() - timedelta(seconds=1)
            return receipt
        with patch.object(identity_service, "claim_games", side_effect=expire_after_claim):
            result = self.finish()
        self.assertEqual(result.json()["code"], "OIDC_FLOW_EXPIRED")
        self.assertEqual(self.browser.get("/v1/me").json()["principal_id"], guest["principal_id"])
        with Session(self.engine) as db:
            self.assertEqual(db.get(Game, game_id).owner_id, guest["principal_id"])
            self.assertEqual(db.get(Game, game_id).revision, 1)
            self.assertEqual(db.exec(select(GuestClaim)).all(), [])
            self.assertEqual(db.exec(select(IdentityAudit)).all(), [])
            self.assertEqual(db.exec(select(Principal).where(Principal.kind == "registered")).all(), [])

    def test_disabling_google_does_not_revoke_existing_account_sessions(self):
        self.login()
        self.app.state.security_settings = google_settings(google_auth_enabled=False)
        self.assertEqual(self.browser.get("/v1/me").json()["kind"], "registered")
        self.assertEqual(self.browser.get("/v1/sessions").status_code, 200)
        self.assertEqual(self.browser.post("/v1/oidc-authentications", json={},
                                           headers=self.headers()).status_code, 503)
        self.assertEqual(self.browser.delete("/v1/sessions/current", headers=self.headers()).status_code, 204)

    def test_claim_receipt_hash_and_structure_are_validated_on_completion_retry(self):
        from finance_app_database_service.contracts import canonical_json
        from finance_app_database_service.identity_service import claimed_game_count
        from finance_app_database_service.problems import Problem
        self.guest()
        self.game()
        self.login(claim=True)
        with Session(self.engine) as db:
            receipt = db.exec(select(GuestClaim)).one()
            db.expunge(receipt)
        self.assertEqual(claimed_game_count(receipt), 1)
        receipt.games_sha256 = "0" * 64
        with self.assertRaises(Problem) as caught:
            claimed_game_count(receipt)
        self.assertEqual(caught.exception.code, "CLAIM_INTEGRITY_ERROR")
        receipt.games_json = canonical_json([{"game_id": "bad", "from_revision": 1, "to_revision": 99}])
        receipt.games_sha256 = content_digest(receipt.games_json)
        with self.assertRaises(Problem):
            claimed_game_count(receipt)
