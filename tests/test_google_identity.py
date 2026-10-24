import base64
import json
import logging
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from starlette.datastructures import URL

from finance_app_database_service.google_identity import (
    GOOGLE_ISSUER, GOOGLE_JWKS_URL, GOOGLE_TOKEN_URL, GoogleClient, NoRedirects,
    derived_token, google_json_request,
)
from finance_app_database_service.identity_middleware import CallbackAccessLogFilter, CallbackCredentialsMiddleware
from finance_app_database_service.problems import Problem
from identity_support import PRIVATE_KEY, PUBLIC_JWK, google_settings


class TestGoogleVerification(unittest.TestCase):
    def setUp(self):
        self.settings = google_settings()
        self.browser = "b" * 43
        self.created = datetime.utcnow() - timedelta(seconds=1)
        self.calls = []

        def fetch(url, data=None):
            self.calls.append(url)
            return {"keys": [PUBLIC_JWK]}, 3600

        self.client = GoogleClient(fetch=fetch)

    def token(self, *, changes=None, remove=(), headers=None, key=None):
        now = int(datetime.now(timezone.utc).timestamp())
        claims = {"iss": GOOGLE_ISSUER, "sub": "synthetic-subject", "aud": self.settings.google_client_id,
                  "iat": now, "exp": now + 3600, "nonce": derived_token(self.browser, "nonce")}
        claims.update(changes or {})
        for name in remove:
            claims.pop(name, None)
        return jwt.encode(claims, key or PRIVATE_KEY, algorithm="RS256",
                          headers={"kid": PUBLIC_JWK["kid"], **(headers or {})})

    def verify(self, token):
        return self.client.verify(token, self.settings, self.browser, self.created)

    def test_canonicalizes_both_documented_issuers_and_ignores_email_for_identity(self):
        for issuer in (GOOGLE_ISSUER, "accounts.google.com"):
            identity = self.verify(self.token(changes={
                "iss": issuer, "email": "shared@example.invalid", "email_verified": False,
            }))
            self.assertEqual((identity.issuer, identity.subject), (GOOGLE_ISSUER, "synthetic-subject"))
        self.assertEqual(self.calls, [GOOGLE_JWKS_URL])

    def test_rejects_missing_required_claims_and_bad_types_values_or_lifetimes(self):
        now = int(datetime.now(timezone.utc).timestamp())
        variants = [
            {"iss": "https://evil.test"}, {"iss": GOOGLE_ISSUER + "/"}, {"aud": "other-client"},
            {"azp": "other-client"}, {"aud": [self.settings.google_client_id, "other"]},
            {"nonce": "wrong"}, {"nonce": ["wrong"]}, {"sub": ""}, {"sub": 123},
            {"sub": "control\nsubject"}, {"sub": "x" * 256}, {"iat": True}, {"exp": True},
            {"iat": str(now)}, {"exp": str(now + 3600)}, {"iat": now + 61},
            {"iat": now - 3600}, {"exp": now - 61}, {"exp": now},
            {"exp": now + 7201}, {"nbf": now + 61},
        ]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(Problem) as caught:
                self.verify(self.token(changes=changes))
            self.assertEqual(caught.exception.code, "OIDC_TOKEN_INVALID")
        for name in ("iss", "sub", "aud", "iat", "exp", "nonce"):
            with self.subTest(missing=name), self.assertRaises(Problem):
                self.verify(self.token(remove=[name]))

    def test_multi_audience_requires_matching_authorized_party(self):
        identity = self.verify(self.token(changes={
            "aud": [self.settings.google_client_id, "other"], "azp": self.settings.google_client_id,
        }))
        self.assertEqual(identity.subject, "synthetic-subject")

    def test_rejects_wrong_signature_untrusted_key_urls_critical_headers_and_algorithms(self):
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with self.assertRaises(Problem):
            self.verify(self.token(key=other_key))
        for header in ({"kid": "../../key"}, {"jku": "https://evil.test/keys"},
                       {"jwk": PUBLIC_JWK}, {"x5u": "https://evil.test/cert"}, {"crit": ["x"]}):
            with self.subTest(header=header), self.assertRaises(Problem):
                self.verify(self.token(headers=header))
        for algorithm, key in (("none", ""), ("HS256", "synthetic-key-for-hmac-testing-only")):
            token = jwt.encode({"sub": "forged"}, key, algorithm=algorithm, headers={"kid": PUBLIC_JWK["kid"]})
            with self.subTest(algorithm=algorithm), self.assertRaises(Problem):
                self.verify(token)

    def test_duplicate_json_members_and_oversized_or_invalid_tokens_fail_closed(self):
        token = self.token()
        header, payload, signature = token.split(".")
        duplicate_header = base64.urlsafe_b64encode(
            b'{"alg":"RS256","alg":"RS256","kid":"synthetic-key"}'
        ).rstrip(b"=").decode()
        duplicate_payload = base64.urlsafe_b64encode(
            b'{"sub":"one","sub":"two"}'
        ).rstrip(b"=").decode()
        for value in (None, "x" * 16385, "bad", "a.b.c", f"{duplicate_header}.{payload}.{signature}",
                      f"{header}.{duplicate_payload}.{signature}"):
            with self.subTest(value_type=type(value)), self.assertRaises(Problem):
                self.verify(value)

    def test_unknown_key_refresh_is_throttled_and_rotation_recovers(self):
        clock = [100]
        self.client.clock = lambda: clock[0]
        self.verify(self.token())
        unknown = self.token(headers={"kid": "rotated-key"})
        for _ in range(5):
            with self.assertRaises(Problem):
                self.verify(unknown)
        self.assertEqual(len(self.calls), 1)
        clock[0] += 31
        self.client.fetch = lambda url: ({"keys": [{**PUBLIC_JWK, "kid": "rotated-key"}]}, 3600)
        self.assertEqual(self.verify(unknown).subject, "synthetic-subject")

    def test_malformed_or_weak_provider_keys_are_not_cached(self):
        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        weak_jwk = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(weak.public_key())), "kid": PUBLIC_JWK["kid"]}
        for document in ({}, {"keys": []}, {"keys": [PUBLIC_JWK] * 2}, {"keys": [weak_jwk]},
                         {"keys": [{**PUBLIC_JWK, "n": "x" * 1401}]}):
            with self.subTest(document_keys=list(document)):
                self.client = GoogleClient(fetch=lambda url: (document, 3600))
                with self.assertRaises(Problem) as caught:
                    self.verify(self.token())
                self.assertEqual(caught.exception.code, "OIDC_KEYS_UNAVAILABLE")
                self.assertEqual(self.client.keys, {})

    def test_http_transport_bounds_responses_refuses_redirects_and_sanitizes_errors(self):
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.read.return_value = b'{"keys":[]}'
        response.headers = {"Cache-Control": "public, max-age=7200"}
        with patch("finance_app_database_service.google_identity.build_opener", return_value=opener):
            self.assertEqual(google_json_request(GOOGLE_JWKS_URL), ({"keys": []}, 3600))
            response.read.assert_called_with(64 * 1024 + 1)
            for body in (b"x" * (64 * 1024 + 1), b'{"id_token":"one","id_token":"two"}', b"NaN", b"[]"):
                response.read.return_value = body
                with self.assertRaises(Problem):
                    google_json_request(GOOGLE_TOKEN_URL, {"code": "synthetic-code"})
            opener.open.side_effect = RuntimeError("sensitive-upstream-body")
            with self.assertRaises(Problem) as caught:
                google_json_request(GOOGLE_TOKEN_URL, {"client_secret": "synthetic-secret"})
            self.assertNotIn("sensitive", str(caught.exception.__dict__))
        self.assertIsNone(NoRedirects().redirect_request(None, None, 302, "", {}, "https://evil.test"))
        with self.assertRaises(ValueError):
            google_json_request("https://evil.test")

    def test_uvicorn_proxy_scope_access_logs_cannot_include_callback_queries(self):
        for path in ("/v1/oidc/callback?code=synthetic-private-code&state=synthetic-private-state",
                     "/prefix/v1/oidc%2Fcallback?code=synthetic-private-code",
                     "/v1/oidc/callback/?code=synthetic-private-code"):
            record = logging.LogRecord("uvicorn.access", 20, "", 1, '%s - "%s %s HTTP/%s" %d',
                                       ("client", "GET", path, "1.1", 303), None)
            self.assertTrue(CallbackAccessLogFilter().filter(record))
            self.assertNotIn("synthetic-private", record.getMessage())

    def test_settings_fail_closed_and_never_include_secrets_in_repr(self):
        for changes in (
            {"google_client_id": "wrong"}, {"google_client_secret": ""},
            {"google_redirect_uri": "http://api.test/v1/oidc/callback"},
            {"google_redirect_uri": "https://api.test/other"},
            {"google_redirect_uri": "https://api.test/v1/oidc/callback?next=evil"},
            {"google_completion_uri": "https://evil.test/complete"}, {"guest_rate_limit_key": ""},
            {"oidc_flow_seconds": 601}, {"recent_auth_seconds": 601},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                google_settings(**changes)
        self.assertNotIn(self.settings.google_client_secret, repr(self.settings))
        self.assertNotIn(self.settings.guest_rate_limit_key, repr(self.settings))

    def test_callback_scope_is_redacted_before_downstream_url_generation(self):
        observed = {}
        async def downstream(scope, receive, send):
            observed["query"] = scope["query_string"]
            observed["url"] = str(URL(scope=scope))
            observed["values"] = scope["state"]["oidc_callback"]
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})
        browser = TestClient(CallbackCredentialsMiddleware(downstream))
        self.addCleanup(browser.close)
        response = browser.get("/v1/oidc/callback?code=synthetic-private-code&state=synthetic-state")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(observed["query"], b"")
        self.assertNotIn("synthetic-private-code", observed["url"])
        self.assertEqual(observed["values"]["code"], "synthetic-private-code")
