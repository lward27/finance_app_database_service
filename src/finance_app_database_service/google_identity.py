"""Fixed Google endpoints and bounded RS256 verification. No provider tokens are persisted."""

import base64
import hashlib
import hmac
import json
import re
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

import jwt
from jwt.algorithms import RSAAlgorithm

from finance_app_database_service.problems import Problem

GOOGLE_ISSUER = "https://accounts.google.com"
GOOGLE_ISSUERS = (GOOGLE_ISSUER, "accounts.google.com")
GOOGLE_AUTHORIZATION_URL = GOOGLE_ISSUER + "/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
FLOW_COOKIE = "__Host-finance_oidc"
KID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")


def derived_token(browser_token: str, purpose: str) -> str:
    digest = hmac.new(browser_token.encode("ascii"), ("finance-oidc-v1:" + purpose).encode(),
                      hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorization_url(settings, browser_token, state):
    verifier = derived_token(browser_token, "pkce")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return GOOGLE_AUTHORIZATION_URL + "?" + urlencode({
        "client_id": settings.google_client_id, "redirect_uri": settings.google_redirect_uri,
        "response_type": "code", "scope": "openid", "state": state,
        "nonce": derived_token(browser_token, "nonce"),
        "code_challenge": challenge, "code_challenge_method": "S256",
        "prompt": "select_account", "max_age": "0",
    })


def invalid_token():
    return Problem(401, "OIDC_TOKEN_INVALID", "Google identity verification failed")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite JSON value")


def strict_json(value):
    return json.loads(value, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def google_json_request(url, data=None):
    # Do not forward client credentials through redirects or environment-selected HTTP proxies.
    if url not in (GOOGLE_TOKEN_URL, GOOGLE_JWKS_URL):
        raise ValueError("Only fixed Google endpoints are allowed")
    opener = build_opener(ProxyHandler({}), NoRedirects(), HTTPSHandler(context=ssl.create_default_context()))
    headers = {"Accept": "application/json", "User-Agent": "finance-identity/1"}
    body = None
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = urlencode(data).encode("ascii")
    try:
        with opener.open(Request(url, data=body, headers=headers), timeout=10) as response:
            raw = response.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise ValueError("Oversized provider response")
            document = strict_json(raw)
            if not isinstance(document, dict):
                raise ValueError("Invalid provider response")
            match = re.search(r"(?:^|,)\s*max-age=(\d+)", response.headers.get("Cache-Control", ""))
            ttl = min(int(match.group(1)), 3600) if match else 300
            return document, ttl
    except Exception:
        # Never return/log an upstream exception, response body, authorization code, or token.
        raise Problem(503, "OIDC_PROVIDER_UNAVAILABLE", "Google authentication is temporarily unavailable") from None


@dataclass(frozen=True)
class GoogleIdentity:
    issuer: str
    subject: str
    expires_at: datetime


class GoogleClient:
    def __init__(self, fetch=None, clock=None):
        self.fetch = fetch or google_json_request
        self.clock = clock or time.monotonic
        self.keys = {}
        self.expires_at = 0
        self.next_refresh_at = 0
        self.lock = threading.Lock()

    def _signing_key(self, kid):
        with self.lock:
            now = self.clock()
            if kid in self.keys and now < self.expires_at:
                return self.keys[kid]
            if now < self.next_refresh_at:
                raise invalid_token()
            self.next_refresh_at = now + 30
            document, ttl = self.fetch(GOOGLE_JWKS_URL)
            try:
                values = document["keys"]
                if not isinstance(values, list) or not 1 <= len(values) <= 20:
                    raise ValueError()
                keys = {}
                for value in values:
                    if not isinstance(value, dict):
                        raise ValueError()
                    if (value.get("kty") != "RSA" or value.get("alg", "RS256") != "RS256"
                            or value.get("use", "sig") != "sig"):
                        continue
                    identifier = value["kid"]
                    if (not isinstance(identifier, str) or not KID_PATTERN.fullmatch(identifier)
                            or identifier in keys or not isinstance(value["n"], str)
                            or not isinstance(value["e"], str)
                            or len(value["n"]) > 1400 or len(value["e"]) > 16):
                        raise ValueError()
                    key = RSAAlgorithm.from_jwk(json.dumps({name: value[name] for name in ("kty", "n", "e")}))
                    if not 2048 <= key.key_size <= 8192:
                        raise ValueError()
                    keys[identifier] = key
                if not keys:
                    raise ValueError()
                self.keys, self.expires_at = keys, now + max(0, min(ttl, 3600))
            except (KeyError, ValueError, TypeError, jwt.PyJWTError):
                raise Problem(503, "OIDC_KEYS_UNAVAILABLE", "Google signing keys are unavailable") from None
            if kid not in self.keys:
                raise invalid_token()
            return self.keys[kid]

    def verify(self, token, settings, browser_token, created_at):
        if not isinstance(token, str) or len(token) > 16 * 1024:
            raise invalid_token()
        try:
            parts = token.split(".")
            if len(parts) != 3:
                raise ValueError()
            # Reject ambiguous JSON before the maintained JWT verifier handles cryptography.
            header = strict_json(jwt.utils.base64url_decode(parts[0]))
            strict_json(jwt.utils.base64url_decode(parts[1]))
            kid = header.get("kid")
            if (header.get("alg") != "RS256" or not isinstance(kid, str)
                    or not KID_PATTERN.fullmatch(kid)
                    or any(name in header for name in ("crit", "jku", "jwk", "x5u"))):
                raise ValueError()
            claims = jwt.decode(
                token, self._signing_key(kid), algorithms=["RS256"],
                audience=settings.google_client_id, issuer=GOOGLE_ISSUERS, leeway=30,
                options={"require": ["iss", "sub", "aud", "iat", "exp", "nonce"]},
            )
            now = datetime.now(timezone.utc).timestamp()
            issued, expires = claims["iat"], claims["exp"]
            if (type(issued) is not int or type(expires) is not int or expires <= issued
                    or issued < created_at.replace(tzinfo=timezone.utc).timestamp() - 30
                    or issued > now + 30 or expires > issued + 7200):
                raise ValueError()
            audience = claims["aud"]
            if (isinstance(audience, list) and len(audience) > 1 and "azp" not in claims
                    or "azp" in claims and claims["azp"] != settings.google_client_id):
                raise ValueError()
            subject, nonce = claims["sub"], claims["nonce"]
            if (not isinstance(subject, str) or not re.fullmatch(r"[!-~]{1,255}", subject)
                    or not isinstance(nonce, str)
                    or not hmac.compare_digest(nonce.encode("utf-8"),
                                               derived_token(browser_token, "nonce").encode())):
                raise ValueError()
            # Email, email_verified, name, picture, and hosted domain never establish identity.
            return GoogleIdentity(GOOGLE_ISSUER, subject, datetime.utcfromtimestamp(expires))
        except Problem:
            raise
        except (ValueError, TypeError, AttributeError, KeyError, UnicodeError, OverflowError,
                RecursionError, jwt.PyJWTError):
            raise invalid_token() from None

    def exchange(self, code, settings, browser_token, created_at):
        document, _ = self.fetch(GOOGLE_TOKEN_URL, {
            "grant_type": "authorization_code", "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret, "redirect_uri": settings.google_redirect_uri,
            "code": code, "code_verifier": derived_token(browser_token, "pkce"),
        })
        token = document.get("id_token")
        return self.verify(token, settings, browser_token, created_at)
