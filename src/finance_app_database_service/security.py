"""Cookie sessions, CSRF, and a separate legacy service credential boundary."""

import hashlib
import hmac
import re
from datetime import datetime
from typing import Optional

from fastapi import Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyCookie
from sqlalchemy import update
from sqlmodel import Session, select

from finance_app_database_service.database import engine
from finance_app_database_service.foundation_models import GuestSession, Principal
from finance_app_database_service.identity_models import GuestClaim
from finance_app_database_service.problems import Problem, is_versioned
from finance_app_database_service.settings import SecuritySettings, get_security_settings

SESSION_COOKIE = "__Host-finance_session"
session_cookie = APIKeyCookie(name=SESSION_COOKIE, auto_error=False)
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def get_v1_session():
    with Session(engine) as session:
        yield session


def session_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def csrf_token(token: str, session_id: str) -> str:
    return hmac.new(token.encode("ascii"), f"finance-csrf-v1:{session_id}".encode(),
                    hashlib.sha256).hexdigest()


def require_origin(
    request: Request, settings: SecuritySettings = Depends(get_security_settings),
):
    origin = request.headers.get("origin")
    if not origin or origin not in settings.allowed_origins:
        raise Problem(403, "ORIGIN_FORBIDDEN", "An allowed Origin is required")


def get_current_session(
    token: Optional[str] = Depends(session_cookie),
    session: Session = Depends(get_v1_session),
) -> GuestSession:
    if not token or not TOKEN_PATTERN.fullmatch(token):
        raise Problem(401, "AUTHENTICATION_REQUIRED", "A valid browser session is required")
    guest = session.exec(select(GuestSession).where(
        GuestSession.token_hash == session_hash(token),
        GuestSession.revoked_at.is_(None),
        GuestSession.expires_at > datetime.utcnow(),
    )).first()
    if guest is None:
        raise Problem(401, "AUTHENTICATION_REQUIRED", "A valid browser session is required")
    principal = session.get(Principal, guest.principal_id)
    if (principal is None or principal.kind not in ("guest", "registered")
            or (principal.kind == "registered" and guest.authenticated_at is None)
            or (principal.kind == "guest" and session.get(GuestClaim, principal.id) is not None)):
        raise Problem(401, "AUTHENTICATION_REQUIRED", "A valid browser session is required")
    return guest


def get_current_guest(
    guest: GuestSession = Depends(get_current_session),
    session: Session = Depends(get_v1_session),
) -> GuestSession:
    if session.get(Principal, guest.principal_id).kind != "guest":
        raise Problem(409, "GUEST_SESSION_REQUIRED", "This action requires a guest session")
    return guest


def get_current_principal(
    guest: GuestSession = Depends(get_current_session),
    session: Session = Depends(get_v1_session),
) -> Principal:
    """Future game handlers must derive owners from this dependency, never payloads."""
    return session.get(Principal, guest.principal_id)


def lock_session_owner(session: Session, guest: GuestSession) -> GuestSession:
    # The same principal -> session -> game order is used by claims and revocation.
    session.execute(update(Principal).where(Principal.id == guest.principal_id).values(id=Principal.id))
    current = session.exec(select(GuestSession).where(
        GuestSession.id == guest.id,
    ).with_for_update().execution_options(populate_existing=True)).first()
    if (current is None or current.revoked_at is not None or current.expires_at <= datetime.utcnow()
            or session.get(GuestClaim, current.principal_id) is not None):
        raise Problem(401, "AUTHENTICATION_REQUIRED", "A valid browser session is required")
    return current


def require_session_write(
    guest: GuestSession = Depends(get_current_session),
    token: str = Depends(session_cookie),
    supplied: Optional[str] = Header(default=None, alias="X-CSRF-Token"),
    origin=Depends(require_origin),
    session: Session = Depends(get_v1_session),
) -> GuestSession:
    if not supplied or not hmac.compare_digest(
        supplied.encode("utf-8"), csrf_token(token, guest.id).encode("ascii")
    ):
        raise Problem(403, "CSRF_FAILED", "A valid X-CSRF-Token is required")
    return lock_session_owner(session, guest)


def require_guest_write(
    guest: GuestSession = Depends(require_session_write),
    session: Session = Depends(get_v1_session),
) -> GuestSession:
    if session.get(Principal, guest.principal_id).kind != "guest":
        raise Problem(409, "GUEST_SESSION_REQUIRED", "This action requires a guest session")
    return guest


def require_service(
    authorization: Optional[str] = Header(default=None),
    settings: SecuritySettings = Depends(get_security_settings),
):
    if len(settings.service_token) < 32:
        raise Problem(503, "SERVICE_AUTH_NOT_CONFIGURED", "Service writes are not configured")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token or not hmac.compare_digest(
        token.encode("utf-8"), settings.service_token.encode("utf-8")
    ):
        raise Problem(401, "SERVICE_AUTH_REQUIRED", "A service credential is required",
                      {"WWW-Authenticate": "Bearer"})


class APICORSMiddleware:
    """Keep anonymous legacy reads public, never wildcard cookie-authenticated APIs."""

    def __init__(self, app, allowed_origins):
        self.private = CORSMiddleware(
            app, allow_origins=list(allowed_origins), allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key",
                           "If-Match", "Authorization"],
            expose_headers=["X-Request-ID", "ETag", "Location"],
        )
        self.public = CORSMiddleware(
            app, allow_origins=["*"], allow_credentials=False,
            allow_methods=["GET", "HEAD", "OPTIONS"], allow_headers=["*"],
            expose_headers=["X-Request-ID"],
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.private(scope, receive, send)
        method = scope["method"]
        if method == "OPTIONS":
            method = Request(scope).headers.get("access-control-request-method", "OPTIONS")
        private = is_versioned(scope["path"]) or method not in ("GET", "HEAD", "OPTIONS")
        await (self.private if private else self.public)(scope, receive, send)
