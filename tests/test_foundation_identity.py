import hashlib
import unittest
from datetime import datetime, timedelta

from sqlmodel import Session, select

from finance_app_database_service.foundation_models import GuestCreationWindow, GuestSession, Principal
from finance_app_database_service.security import SESSION_COOKIE
from foundation_support import ORIGIN, client, migrate, settings, sqlite_engine, test_app


class TestGuestIdentity(unittest.TestCase):
    def setUp(self):
        self.engine = sqlite_engine()
        migrate(self.engine)
        self.app = test_app(self.engine)
        self.client = client(self.app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def create(self, browser=None, **kwargs):
        return (browser or self.client).post(
            "/v1/guest-sessions", json={}, headers={"Origin": ORIGIN}, **kwargs,
        )

    def test_creates_hash_only_secure_host_cookie_and_own_identity(self):
        response = self.create()
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        cookie = response.headers["set-cookie"]
        for attribute in ("Secure", "HttpOnly", "Path=/", "SameSite=lax", "Max-Age=604800"):
            self.assertIn(attribute, cookie)
        self.assertNotIn("Domain=", cookie)
        token = self.client.cookies.get(SESSION_COOKIE)
        self.assertEqual(len(token), 43)
        with Session(self.engine) as db:
            guest = db.exec(select(GuestSession)).one()
            self.assertEqual(guest.token_hash, hashlib.sha256(token.encode()).hexdigest())
            self.assertNotIn(token, guest.json())
            self.assertEqual(guest.principal_id, body["principal_id"])
        me = self.client.get("/v1/me")
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json(), body)
        self.assertEqual(me.headers["cache-control"], "no-store")
        self.assertEqual(body["capabilities"], {
            "create_games": False, "join_ranked": False, "claim_guest": False,
        })
        self.assertTrue(body["created_at"].endswith("+00:00"))

    def test_missing_untrusted_and_null_origin_cannot_create(self):
        for origin in (None, "https://evil.test", "null"):
            with self.subTest(origin=origin):
                response = self.client.post("/v1/guest-sessions", json={},
                                            headers={"Origin": origin} if origin else {})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["code"], "ORIGIN_FORBIDDEN")
        with Session(self.engine) as db:
            self.assertEqual(db.exec(select(Principal)).all(), [])

    def test_client_cannot_select_or_claim_a_principal(self):
        response = self.client.post("/v1/guest-sessions",
                                    json={"principal_id": "prn_someone_else"},
                                    headers={"Origin": ORIGIN})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.headers["content-type"], "application/problem+json")
        self.assertNotIn("prn_someone_else", response.text)

    def test_anonymous_malformed_and_expired_sessions_are_unauthenticated(self):
        self.assertEqual(self.client.get("/v1/me").status_code, 401)
        for token in ("bad-token", "a" * 43):
            self.assertEqual(self.client.get("/v1/me", headers={
                "Cookie": f"{SESSION_COOKIE}={token}",
            }).status_code, 401)
        self.assertEqual(self.create().status_code, 201)
        with Session(self.engine) as db:
            guest = db.exec(select(GuestSession)).one()
            guest.expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.add(guest)
            db.commit()
        self.assertEqual(self.client.get("/v1/me").status_code, 401)

    def test_revoke_requires_both_csrf_and_origin_and_retains_principal(self):
        created = self.create().json()
        token = self.client.cookies.get(SESSION_COOKIE)
        url = "/v1/guest-sessions/current"
        for headers in (
            {"Origin": ORIGIN},
            {"X-CSRF-Token": created["csrf_token"]},
            {"Origin": "https://evil.test", "X-CSRF-Token": created["csrf_token"]},
            {"Origin": ORIGIN, "X-CSRF-Token": "wrong"},
        ):
            self.assertEqual(self.client.delete(url, headers=headers).status_code, 403)
            self.assertEqual(self.client.get("/v1/me").status_code, 200)
        response = self.client.delete(url, headers={
            "Origin": ORIGIN, "X-CSRF-Token": created["csrf_token"],
        })
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(response.content, b"")
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        self.assertEqual(self.client.get("/v1/me").status_code, 401)
        self.assertEqual(self.client.get("/v1/me", headers={
            "Cookie": f"{SESSION_COOKIE}={token}",
        }).status_code, 401)
        with Session(self.engine) as db:
            self.assertIsNotNone(db.exec(select(GuestSession)).one().revoked_at)
            self.assertIsNotNone(db.get(Principal, created["principal_id"]))

    def test_two_guests_cannot_use_each_others_csrf_or_override_ownership(self):
        first = self.create().json()
        other = client(self.app)
        try:
            second = self.create(other).json()
            self.assertNotEqual(first["principal_id"], second["principal_id"])
            response = self.client.delete("/v1/guest-sessions/current", headers={
                "Origin": ORIGIN, "X-CSRF-Token": second["csrf_token"],
            })
            self.assertEqual(response.status_code, 403)
            me = self.client.get("/v1/me", params={"principal_id": second["principal_id"]},
                                 headers={"X-Principal-ID": second["principal_id"]})
            self.assertEqual(me.json()["principal_id"], first["principal_id"])
            self.assertEqual(other.get("/v1/me").json()["principal_id"], second["principal_id"])
        finally:
            other.close()

    def test_existing_session_is_not_silently_replaced(self):
        first = self.create().json()
        self.assertEqual(self.create().status_code, 409)
        self.assertEqual(self.client.get("/v1/me").json()["principal_id"], first["principal_id"])

    def test_creation_limit_is_durable_and_uses_no_raw_client_address(self):
        app = test_app(self.engine, settings(guest_creation_limit=2))
        for index in range(3):
            browser = client(app)
            try:
                response = self.create(browser)
                self.assertEqual(response.status_code, 201 if index < 2 else 429, response.text)
                if index == 2:
                    self.assertGreater(int(response.headers["retry-after"]), 0)
            finally:
                browser.close()
        with Session(self.engine) as db:
            window = db.exec(select(GuestCreationWindow)).one()
            self.assertEqual(window.used, 2)
            self.assertEqual(len(window.key_hash), 64)
            self.assertNotIn("testclient", window.json())
            self.assertEqual(len(db.exec(select(Principal)).all()), 2)

    def test_missing_rate_limit_configuration_disables_guest_creation(self):
        browser = client(test_app(self.engine, settings(guest_rate_limit_key="")))
        try:
            self.assertEqual(self.create(browser).status_code, 503)
        finally:
            browser.close()

    def test_cookie_cors_uses_exact_origin_and_legacy_reads_stay_public(self):
        response = self.client.options("/v1/guest-sessions", headers={
            "Origin": ORIGIN, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-csrf-token",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], ORIGIN)
        self.assertEqual(response.headers["access-control-allow-credentials"], "true")
        denied = self.client.options("/v1/guest-sessions", headers={
            "Origin": "https://evil.test", "Access-Control-Request-Method": "POST",
        })
        self.assertEqual(denied.status_code, 400)
        self.assertNotIn("access-control-allow-origin", denied.headers)
        public = self.client.get("/healthz", headers={"Origin": "https://evil.test"})
        self.assertEqual(public.json(), {"status": "ok"})
        self.assertEqual(public.headers["access-control-allow-origin"], "*")
        self.assertNotIn("access-control-allow-credentials", public.headers)

    def test_problem_responses_have_safe_request_ids_and_no_reflected_input(self):
        response = self.client.get("/v1/instruments", params={"limit": "a-secret-value"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["request_id"], response.headers["x-request-id"])
        self.assertRegex(response.json()["request_id"], r"^[a-f0-9]{32}$")
        self.assertNotIn("a-secret-value", response.text)
        self.assertEqual(self.client.get("/v1/unknown").json()["code"], "NOT_FOUND")
