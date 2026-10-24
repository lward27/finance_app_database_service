"""Google verification never silently replaces a guest or links accounts by email."""

import hmac
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from finance_app_database_service.foundation_api import identity_view, utc
from finance_app_database_service.foundation_models import GuestSession, Principal
from finance_app_database_service.game_models import Game
from finance_app_database_service.google_identity import FLOW_COOKIE, GOOGLE_ISSUERS, derived_token
from finance_app_database_service.identity_contracts import (
    AuthenticationCapabilities, AuthenticationComplete, AuthenticationStart, AuthenticationView,
    EmptyIdentityCommand, RevocationView, SessionPage, SessionRevocations, SessionView,
)
from finance_app_database_service.identity_models import OIDCAuthentication
from finance_app_database_service.identity_service import (
    claimed_game_count, complete_authentication, read_flow, require_google, require_recent_authentication,
    require_source, revoke_sessions, start_authentication,
)
from finance_app_database_service.pagination import cursor_scope, decode_cursor, encode_cursor
from finance_app_database_service.problems import PROBLEM_RESPONSES, Problem
from finance_app_database_service.security import (
    SESSION_COOKIE, TOKEN_PATTERN, get_current_principal, get_current_session, get_v1_session,
    require_guest_write, require_origin, require_session_write, session_cookie, session_hash,
)
from finance_app_database_service.settings import SecuritySettings, get_security_settings

router = APIRouter(prefix="/v1", responses=PROBLEM_RESPONSES, tags=["Identity"])


def enabled_google(settings: SecuritySettings = Depends(get_security_settings)):
    require_google(settings)
    return settings


@router.get("/authentication", response_model=AuthenticationCapabilities)
def authentication_capabilities(settings: SecuritySettings = Depends(get_security_settings)):
    return AuthenticationCapabilities(google_enabled=settings.google_auth_enabled)


def started(session, settings, request, response, source, token, intent):
    browser, url, expires = start_authentication(session, settings, request, source, token, intent)
    response.set_cookie(FLOW_COOKIE, browser, max_age=settings.oidc_flow_seconds,
                        secure=True, httponly=True, samesite="lax", path="/")
    return AuthenticationStart(authorization_url=url, expires_at=utc(expires))


@router.post("/oidc-authentications", response_model=AuthenticationStart, status_code=201)
def sign_in(
    payload: EmptyIdentityCommand, request: Request, response: Response,
    origin=Depends(require_origin), settings: SecuritySettings = Depends(enabled_google),
    token: Optional[str] = Depends(session_cookie),
    supplied: Optional[str] = Header(None, alias="X-CSRF-Token"), session: Session = Depends(get_v1_session),
):
    source = None
    if token:
        try:
            source = get_current_session(token, session)
        except Problem as exc:
            if exc.status != 401:
                raise
    intent = "sign_in"
    if source:
        source = require_session_write(source, token, supplied, None, session)
        if session.get(Principal, source.principal_id).kind == "guest":
            raise Problem(409, "EXPLICIT_GUEST_CLAIM_REQUIRED", "Use the explicit save-guest-games action")
        intent = "reauthenticate"
    return started(session, settings, request, response, source, token, intent)


@router.post("/guest-claims", response_model=AuthenticationStart, status_code=201)
def guest_claim(
    payload: EmptyIdentityCommand, request: Request, response: Response,
    source: GuestSession = Depends(require_guest_write),
    token: str = Depends(session_cookie), settings: SecuritySettings = Depends(enabled_google),
    session: Session = Depends(get_v1_session),
):
    return started(session, settings, request, response, source, token, "guest_claim")


@router.get("/oidc/callback", response_class=RedirectResponse)
def google_callback(
    request: Request, settings: SecuritySettings = Depends(enabled_google),
    token: Optional[str] = Depends(session_cookie), session: Session = Depends(get_v1_session),
):
    values = getattr(request.state, "oidc_callback", None)
    if not isinstance(values, dict) or not TOKEN_PATTERN.fullmatch(values.get("state", "")):
        raise Problem(400, "OIDC_CALLBACK_INVALID", "Invalid Google callback")
    browser_token = request.cookies.get(FLOW_COOKIE)
    flow = read_flow(session, browser_token, settings, lock=True)
    if not hmac.compare_digest(flow.state_hash, session_hash(values["state"])):
        raise Problem(403, "OIDC_STATE_INVALID", "Authentication state did not match")
    if flow.status != "pending":
        raise Problem(409, "OIDC_FLOW_USED", "This authentication callback was already consumed")
    require_source(session, flow, token)
    if values.get("iss", "https://accounts.google.com") not in GOOGLE_ISSUERS:
        raise Problem(400, "OIDC_CALLBACK_INVALID", "Invalid Google callback")
    code, error = values.get("code"), values.get("error")
    if error:
        flow.status, flow.failure_code = "failed", "OIDC_CANCELLED"
        flow.consumed_at = datetime.utcnow()
        session.add(flow)
        session.commit()
        return RedirectResponse(settings.google_completion_uri, status_code=303)
    if not code or len(code) > 4096 or any(ord(char) < 32 or ord(char) > 126 for char in code):
        raise Problem(400, "OIDC_CALLBACK_INVALID", "Invalid Google callback")
    flow_id, created_at = flow.id, flow.created_at
    flow.status, flow.consumed_at = "exchanging", datetime.utcnow()
    session.add(flow)
    # Consume before network access. A crash is fail-closed and requires a new flow.
    session.commit()
    identity, failure = None, None
    try:
        identity = request.app.state.google_client.exchange(code, settings, browser_token, created_at)
    except Problem as exc:
        failure = exc.code
    except Exception:
        failure = "OIDC_PROVIDER_UNAVAILABLE"
    flow = session.exec(select(OIDCAuthentication).where(
        OIDCAuthentication.id == flow_id,
    ).with_for_update().execution_options(populate_existing=True)).first()
    if flow is not None and flow.status == "exchanging":
        if failure or flow.expires_at <= datetime.utcnow() or identity.expires_at <= datetime.utcnow():
            flow.status, flow.failure_code = "failed", failure or "OIDC_FLOW_EXPIRED"
        else:
            flow.status, flow.verified_at = "verified", datetime.utcnow()
            flow.issuer, flow.subject = identity.issuer, identity.subject
            flow.expires_at = min(flow.expires_at, identity.expires_at)
        session.add(flow)
        session.commit()
    return RedirectResponse(settings.google_completion_uri, status_code=303)


@router.get("/oidc-authentications/current", response_model=AuthenticationView)
def current_authentication(
    request: Request, settings: SecuritySettings = Depends(enabled_google),
    session: Session = Depends(get_v1_session),
):
    browser_token = request.cookies.get(FLOW_COOKIE)
    try:
        flow = read_flow(session, browser_token, settings)
    except Problem as exc:
        if exc.status != 401:
            raise
        return AuthenticationView(status="none")
    count = 0
    if flow.intent == "guest_claim" and flow.source_principal_id:
        # Bound by the same ten-game ownership quota.
        count = len(session.exec(select(Game.id).where(Game.owner_id == flow.source_principal_id)).all())
    return AuthenticationView(
        status=flow.status, intent=flow.intent, expires_at=utc(flow.expires_at), guest_game_count=count,
        csrf_token=derived_token(browser_token, "csrf:" + flow.id), code=flow.failure_code,
    )


@router.post("/oidc-authentications/current/completion", response_model=AuthenticationComplete)
def confirm_authentication(
    payload: EmptyIdentityCommand, request: Request, response: Response,
    origin=Depends(require_origin), settings: SecuritySettings = Depends(enabled_google),
    token: Optional[str] = Depends(session_cookie),
    supplied: Optional[str] = Header(None, alias="X-CSRF-Token"), session: Session = Depends(get_v1_session),
):
    principal, account, new_token, receipt = complete_authentication(
        session, settings, request.cookies.get(FLOW_COOKIE), token, supplied,
    )
    response.set_cookie(
        SESSION_COOKIE, new_token, max_age=max(1, int((account.expires_at - datetime.utcnow()).total_seconds())),
        secure=True, httponly=True, samesite="lax", path="/",
    )
    response.delete_cookie(FLOW_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return AuthenticationComplete(
        identity=identity_view(principal, account, new_token, settings.games_enabled, settings.google_auth_enabled),
        claimed_games=claimed_game_count(receipt),
        claim_id=receipt.id if receipt else None,
    )


@router.get("/sessions", response_model=SessionPage)
def list_sessions(
    principal: Principal = Depends(get_current_principal),
    current: GuestSession = Depends(get_current_session), session: Session = Depends(get_v1_session),
    cursor: Optional[str] = Query(None, max_length=1024), limit: int = Query(20, ge=1, le=50),
):
    scope = cursor_scope("sessions", principal=principal.id)
    query = select(GuestSession).where(
        GuestSession.principal_id == principal.id, GuestSession.revoked_at.is_(None),
        GuestSession.expires_at > datetime.utcnow(),
    )
    if cursor:
        after, = decode_cursor(cursor, scope, 1)
        query = query.where(GuestSession.id > after)
    rows = session.exec(query.order_by(GuestSession.id).limit(limit + 1)).all()
    return SessionPage(
        items=[SessionView(id=row.id, created_at=utc(row.created_at), expires_at=utc(row.expires_at),
                           authenticated_at=utc(row.authenticated_at) if row.authenticated_at else None,
                           is_current=row.id == current.id) for row in rows[:limit]],
        next_cursor=encode_cursor(scope, rows[limit - 1].id) if len(rows) > limit else None,
    )


def cleared_response():
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    response.delete_cookie(FLOW_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


@router.delete("/sessions/current", status_code=204, response_class=Response)
def sign_out(current: GuestSession = Depends(require_session_write), session: Session = Depends(get_v1_session)):
    revoke_sessions(session, current, target_id=current.id)
    return cleared_response()


@router.delete("/sessions/{session_id}", status_code=204, response_class=Response)
def revoke_session(
    session_id: str, current: GuestSession = Depends(require_session_write),
    settings: SecuritySettings = Depends(get_security_settings), session: Session = Depends(get_v1_session),
):
    target = session.exec(select(GuestSession).where(
        GuestSession.id == session_id, GuestSession.principal_id == current.principal_id,
        GuestSession.revoked_at.is_(None), GuestSession.expires_at > datetime.utcnow(),
    )).first()
    if target is None:
        raise Problem(404, "SESSION_NOT_FOUND", "Session not found")
    if target.id != current.id:
        require_recent_authentication(current, settings)
    revoke_sessions(session, current, target_id=target.id)
    return cleared_response() if target.id == current.id else Response(status_code=204)


@router.post("/session-revocations", response_model=RevocationView)
def revoke_account_sessions(
    payload: SessionRevocations, response: Response, current: GuestSession = Depends(require_session_write),
    settings: SecuritySettings = Depends(get_security_settings), session: Session = Depends(get_v1_session),
):
    require_recent_authentication(current, settings)
    count = revoke_sessions(session, current, scope=payload.scope)
    if payload.scope == "all":
        response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        response.delete_cookie(FLOW_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return RevocationView(revoked_count=count)
