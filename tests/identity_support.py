"""Offline Google protocol fixture. All credentials and subjects here are synthetic."""

import json
import secrets
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from finance_app_database_service.google_identity import (
    FLOW_COOKIE, GOOGLE_ISSUER, GOOGLE_JWKS_URL, GOOGLE_TOKEN_URL, GoogleClient,
)
from foundation_support import ORIGIN, client, settings, test_app
from test_simulation import DAYS

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_JWK = {**json.loads(RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key())), "kid": "synthetic-key", "alg": "RS256"}


def google_settings(**overrides):
    values = dict(
        games_enabled=True, google_auth_enabled=True, google_client_id="synthetic-client.apps.googleusercontent.com",
        google_client_secret="synthetic-google-client-secret-not-real",
        google_redirect_uri="https://api.test/v1/oidc/callback",
        google_completion_uri=ORIGIN + "/auth/complete", guest_creation_limit=100,
    )
    values.update(overrides)
    return settings(**values)


class FakeGoogle:
    def __init__(self):
        self.codes = {}
        self.calls = []

    def issue(self, url, subject="google-subject-a", *, changes=None, remove=(), key=None):
        params = {name: values[0] for name, values in parse_qs(urlsplit(url).query).items()}
        now = int(datetime.now(timezone.utc).timestamp())
        claims = {"iss": GOOGLE_ISSUER, "sub": subject, "aud": params["client_id"],
                  "iat": now, "exp": now + 3600, "nonce": params["nonce"]}
        claims.update(changes or {})
        for name in remove:
            claims.pop(name, None)
        token = jwt.encode(claims, key or PRIVATE_KEY, algorithm="RS256", headers={"kid": PUBLIC_JWK["kid"]})
        code = secrets.token_urlsafe(24)
        self.codes[code] = (params, token)
        return {"state": params["state"], "code": code}

    def fetch(self, url, data=None):
        self.calls.append((url, data))
        if url == GOOGLE_JWKS_URL:
            return {"keys": [PUBLIC_JWK]}, 3600
        if url != GOOGLE_TOKEN_URL:
            raise AssertionError("Unexpected provider endpoint")
        params, token = self.codes.pop(data["code"])
        import base64
        import hashlib
        challenge = base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        assert challenge == params["code_challenge"]
        assert data["client_id"] == params["client_id"]
        assert data["redirect_uri"] == params["redirect_uri"]
        assert data["grant_type"] == "authorization_code"
        return {"id_token": token, "access_token": "synthetic-discarded-access-token"}, 0


class IdentityHarness:
    def configure_identity(self, config=None):
        self.app = test_app(self.engine, config or google_settings())
        self.fake = FakeGoogle()
        self.app.state.google_client = GoogleClient(fetch=self.fake.fetch)
        self.browser = self.new_browser()

    def new_browser(self):
        browser = client(self.app)
        self.addCleanup(browser.close)
        return browser

    def headers(self, browser=None):
        browser = browser or self.browser
        me = browser.get("/v1/me")
        return {"Origin": ORIGIN, **({"X-CSRF-Token": me.json()["csrf_token"]} if me.status_code == 200 else {})}

    def guest(self, browser=None):
        browser = browser or self.browser
        response = browser.post("/v1/guest-sessions", json={}, headers={"Origin": ORIGIN})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def begin(self, browser=None, *, claim=False):
        browser = browser or self.browser
        response = browser.post("/v1/guest-claims" if claim else "/v1/oidc-authentications",
                                json={}, headers=self.headers(browser))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["authorization_url"]

    def verify(self, url, browser=None, subject="google-subject-a", **kwargs):
        browser = browser or self.browser
        params = self.fake.issue(url, subject, **kwargs)
        response = browser.get("/v1/oidc/callback", params=params, follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        self.assertEqual(response.headers["location"], self.app.state.security_settings.google_completion_uri)
        return params

    def finish(self, browser=None):
        browser = browser or self.browser
        view = browser.get("/v1/oidc-authentications/current").json()
        return browser.post("/v1/oidc-authentications/current/completion", json={},
                            headers={"Origin": ORIGIN, "X-CSRF-Token": view.get("csrf_token") or "missing"})

    def login(self, browser=None, subject="google-subject-a", *, claim=False):
        browser = browser or self.browser
        self.verify(self.begin(browser, claim=claim), browser, subject)
        response = self.finish(browser)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def game(self, browser=None, *, key=None):
        browser = browser or self.browser
        response = browser.post("/v1/games", json={
            "name": "Synthetic identity game", "start_date": str(DAYS[0]),
            "initial_cash": "1000", "release_id": self.release_id,
        }, headers={**self.headers(browser), "Idempotency-Key": key or secrets.token_urlsafe(12)})
        self.assertEqual(response.status_code, 201, response.text)
        return response
