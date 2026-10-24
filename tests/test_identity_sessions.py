import unittest
from datetime import datetime, timedelta

from sqlmodel import Session, select

from finance_app_database_service.foundation_models import GuestSession
from finance_app_database_service.identity_models import IdentityAudit
from finance_app_database_service.security import SESSION_COOKIE, session_hash
from foundation_support import ORIGIN, migrate, sqlite_engine
from game_support import seed_release
from identity_support import IdentityHarness


class TestIdentitySessions(IdentityHarness, unittest.TestCase):
    def setUp(self):
        self.engine = sqlite_engine()
        self.addCleanup(self.engine.dispose)
        migrate(self.engine)
        self.release_id = seed_release(self.engine)
        self.configure_identity()
        self.identity = self.login()["identity"]

    def test_sessions_are_private_bounded_paginated_and_cursor_scoped_to_owner(self):
        for _ in range(2):
            self.login(self.new_browser())
        page = self.browser.get("/v1/sessions", params={"limit": 1})
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertEqual(len(page.json()["items"]), 1)
        self.assertIsNotNone(page.json()["next_cursor"])
        self.assertEqual(set(page.json()["items"][0]), {
            "id", "created_at", "expires_at", "authenticated_at", "is_current",
        })
        other = self.new_browser()
        self.login(other, "other-subject")
        self.assertEqual(other.get("/v1/sessions", params={
            "cursor": page.json()["next_cursor"],
        }).status_code, 422)
        all_rows = self.browser.get("/v1/sessions").json()["items"]
        self.assertEqual(len(all_rows), 3)
        self.assertEqual(sum(row["is_current"] for row in all_rows), 1)
        self.assertEqual(self.browser.get("/v1/sessions", params={"limit": 51}).status_code, 422)

    def test_revocation_requires_origin_csrf_and_hides_other_owners(self):
        other = self.new_browser()
        self.login(other, "other-subject")
        foreign_id = other.get("/v1/sessions").json()["items"][0]["id"]
        for headers in ({}, {"Origin": ORIGIN}, {"X-CSRF-Token": self.identity["csrf_token"]}):
            denied = self.browser.delete("/v1/sessions/current", headers=headers)
            self.assertEqual(denied.status_code, 403, denied.text)
        for target in ("ses_unknown", foreign_id):
            denied = self.browser.delete("/v1/sessions/" + target, headers=self.headers())
            self.assertEqual(denied.status_code, 404)
        self.assertEqual(other.get("/v1/me").status_code, 200)

    def test_current_logout_does_not_revoke_other_devices_or_delete_games(self):
        game_id = self.game().json()["id"]
        other = self.new_browser()
        self.login(other)
        raw_token = self.browser.cookies.get(SESSION_COOKIE)
        response = self.browser.delete("/v1/sessions/current", headers=self.headers())
        self.assertEqual(response.status_code, 204)
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        self.assertEqual(self.browser.get("/v1/me").status_code, 401)
        self.assertEqual(self.browser.get("/v1/me", headers={
            "Cookie": f"{SESSION_COOKIE}={raw_token}",
        }).status_code, 401)
        self.assertEqual(other.get("/v1/games/" + game_id).status_code, 200)

    def test_stale_authentication_cannot_revoke_other_devices_and_same_account_reauth_rotates_cookie(self):
        other = self.new_browser()
        self.login(other)
        with Session(self.engine) as db:
            current = db.exec(select(GuestSession).where(
                GuestSession.token_hash == session_hash(self.browser.cookies.get(SESSION_COOKIE)),
            )).one()
            current.authenticated_at = datetime.utcnow() - timedelta(minutes=10)
            db.add(current)
            db.commit()
        old_token = self.browser.cookies.get(SESSION_COOKIE)
        denied = self.browser.post("/v1/session-revocations", json={"scope": "other"}, headers=self.headers())
        self.assertEqual(denied.json()["code"], "REAUTHENTICATION_REQUIRED")
        reauthed = self.login()["identity"]
        self.assertEqual(reauthed["principal_id"], self.identity["principal_id"])
        self.assertNotEqual(old_token, self.browser.cookies.get(SESSION_COOKIE))
        response = self.browser.post("/v1/session-revocations", json={"scope": "other"}, headers=self.headers())
        self.assertEqual(response.json()["revoked_count"], 1)
        self.assertEqual(other.get("/v1/me").status_code, 401)
        self.assertEqual(self.browser.get("/v1/me").status_code, 200)

    def test_reauthentication_does_not_switch_accounts_or_create_another_principal(self):
        from finance_app_database_service.foundation_models import Principal
        old_token = self.browser.cookies.get(SESSION_COOKIE)
        self.verify(self.begin(), subject="wrong-account")
        result = self.finish()
        self.assertEqual(result.json()["code"], "ACCOUNT_SWITCH_FORBIDDEN")
        self.assertEqual(self.browser.cookies.get(SESSION_COOKIE), old_token)
        with Session(self.engine) as db:
            self.assertEqual(len(db.exec(select(Principal)).all()), 1)

    def test_revoke_all_invalidates_pending_anonymous_authorizations_and_cannot_resurrect_sessions(self):
        pending = self.new_browser()
        self.verify(self.begin(pending), pending)
        result = self.browser.post("/v1/session-revocations", json={"scope": "all"}, headers=self.headers())
        self.assertEqual(result.json()["revoked_count"], 1)
        self.assertEqual(self.browser.get("/v1/me").status_code, 401)
        denied = self.finish(pending)
        self.assertEqual(denied.json()["code"], "OIDC_AUTHORIZATION_REVOKED")
        self.assertEqual(pending.get("/v1/me").status_code, 401)
        # New Google proof after the cutoff is still a valid recovery path.
        self.assertEqual(self.login(pending)["identity"]["principal_id"], self.identity["principal_id"])

    def test_specific_revocation_invalidates_source_flow_and_is_audited(self):
        other = self.new_browser()
        self.login(other)
        self.verify(self.begin(other), other)
        other_id = next(row["id"] for row in self.browser.get("/v1/sessions").json()["items"]
                        if not row["is_current"])
        response = self.browser.delete("/v1/sessions/" + other_id, headers=self.headers())
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.finish(other).status_code, 401)
        with Session(self.engine) as db:
            audit = db.exec(select(IdentityAudit).where(IdentityAudit.kind == "session_revoked")).one()
            self.assertEqual(audit.target_session_id, other_id)
            self.assertEqual(audit.affected_sessions, 1)

    def test_expired_and_revoked_sessions_do_not_appear_and_legacy_guest_logout_is_guest_only(self):
        other = self.new_browser()
        self.login(other)
        with Session(self.engine) as db:
            stale = db.exec(select(GuestSession).where(
                GuestSession.token_hash == session_hash(other.cookies.get(SESSION_COOKIE)),
            )).one()
            stale.expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.add(stale)
            db.commit()
        self.assertEqual(len(self.browser.get("/v1/sessions").json()["items"]), 1)
        self.assertEqual(other.get("/v1/me").status_code, 401)
        denied = self.browser.delete("/v1/guest-sessions/current", headers=self.headers())
        self.assertEqual(denied.json()["code"], "GUEST_SESSION_REQUIRED")
        self.assertEqual(self.browser.get("/v1/me").status_code, 200)

    def test_guest_can_log_out_but_cannot_use_account_wide_session_actions(self):
        guest = self.new_browser()
        self.guest(guest)
        denied = guest.post("/v1/session-revocations", json={"scope": "all"}, headers=self.headers(guest))
        self.assertEqual(denied.json()["code"], "REGISTERED_ACCOUNT_REQUIRED")
        self.assertEqual(guest.delete("/v1/sessions/current", headers=self.headers(guest)).status_code, 204)
