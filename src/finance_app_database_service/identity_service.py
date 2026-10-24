"""One-use authentication and principal-serialized ownership/session transitions."""

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, update
from sqlmodel import select

from finance_app_database_service.catalog import insert_missing
from finance_app_database_service.contracts import canonical_json, content_digest
from finance_app_database_service.foundation_models import GuestSession, Principal
from finance_app_database_service.game_models import AdvanceOperation, Game
from finance_app_database_service.game_service import ACTIVE, MAX_OWNED_GAMES
from finance_app_database_service.google_identity import (
    authorization_url, derived_token,
)
from finance_app_database_service.identity_models import (
    GuestClaim, IdentityAudit, IdentityRateWindow, OIDCAuthentication,
)
from finance_app_database_service.problems import Problem
from finance_app_database_service.security import TOKEN_PATTERN, get_current_session, session_hash


def require_google(settings):
    if not settings.google_auth_enabled:
        raise Problem(503, "OIDC_DISABLED", "Google authentication is not enabled")


def context_digest(settings):
    return content_digest(canonical_json({
        "client_id": settings.google_client_id, "redirect_uri": settings.google_redirect_uri,
        "completion_uri": settings.google_completion_uri,
    }))


def cookie_hash(token):
    return session_hash(token) if token and TOKEN_PATTERN.fullmatch(token) else None


def read_flow(session, browser_token, settings, *, lock=False):
    if not browser_token or not TOKEN_PATTERN.fullmatch(browser_token):
        raise Problem(401, "OIDC_BROWSER_REQUIRED", "Start authentication in this browser")
    query = select(OIDCAuthentication).where(
        OIDCAuthentication.browser_hash == session_hash(browser_token),
    ).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    flow = session.exec(query).first()
    if (flow is None or flow.context_sha256 != context_digest(settings)
            or flow.expires_at <= datetime.utcnow()):
        raise Problem(401, "OIDC_FLOW_EXPIRED", "Start a new authentication")
    return flow


def consume_identity_rate(session, settings, request, source):
    now = datetime.utcnow()
    window = int(now.replace(tzinfo=timezone.utc).timestamp()) // 600
    client = source.principal_id if source else (request.client.host if request.client else "unknown")
    key = hmac.new(settings.guest_rate_limit_key.encode(),
                   f"finance-identity-v1:{window}:{client}".encode(), hashlib.sha256).hexdigest()
    expires = datetime.utcfromtimestamp((window + 1) * 600)
    insert_missing(session, IdentityRateWindow, {
        "key_hash": key, "used": 0, "expires_at": expires,
    }, ["key_hash"])
    result = session.execute(update(IdentityRateWindow).where(
        IdentityRateWindow.key_hash == key, IdentityRateWindow.used < 20,
    ).values(used=IdentityRateWindow.used + 1))
    if result.rowcount != 1:
        raise Problem(429, "IDENTITY_RATE_LIMITED", "Too many authentication attempts",
                      {"Retry-After": str(max(1, int((expires - now).total_seconds()) + 1))})
    expired = select(IdentityRateWindow.key_hash).where(IdentityRateWindow.expires_at <= now).limit(100)
    session.execute(delete(IdentityRateWindow).where(
        IdentityRateWindow.key_hash.in_(expired),
    ).execution_options(synchronize_session=False))
    abandoned = select(OIDCAuthentication.id).where(
        OIDCAuthentication.expires_at <= now, OIDCAuthentication.completed_session_id.is_(None),
    ).limit(100)
    session.execute(delete(OIDCAuthentication).where(
        OIDCAuthentication.id.in_(abandoned),
    ).execution_options(synchronize_session=False))


def start_authentication(session, settings, request, source, token, intent):
    consume_identity_rate(session, settings, request, source)
    now = datetime.utcnow()
    browser_token, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    flow = OIDCAuthentication(
        state_hash=session_hash(state), browser_hash=session_hash(browser_token),
        context_sha256=context_digest(settings), intent=intent,
        source_session_id=source.id if source else None,
        source_principal_id=source.principal_id if source else None, source_token_hash=cookie_hash(token),
        created_at=now, expires_at=now + timedelta(seconds=settings.oidc_flow_seconds),
    )
    session.add(flow)
    expires = flow.expires_at
    session.commit()
    return browser_token, authorization_url(settings, browser_token, state), expires


def require_source(session, flow, token):
    if cookie_hash(token) != flow.source_token_hash:
        raise Problem(409, "OIDC_SESSION_CHANGED", "The browser session changed; start again")
    if flow.source_session_id:
        source = get_current_session(token, session)
        if source.id != flow.source_session_id or source.principal_id != flow.source_principal_id:
            raise Problem(409, "OIDC_SESSION_CHANGED", "The browser session changed; start again")
        return source
    return None


def claim_games(session, source, target, authentication, account_session):
    if source is None or session.get(Principal, source.principal_id).kind != "guest":
        raise Problem(409, "GUEST_SESSION_REQUIRED", "This action requires an unclaimed guest")
    if session.get(GuestClaim, source.principal_id):
        raise Problem(409, "GUEST_ALREADY_CLAIMED", "This guest was already claimed")
    games = session.exec(select(Game).where(
        Game.owner_id == source.principal_id,
    ).order_by(Game.id).with_for_update().execution_options(populate_existing=True)).all()
    count = session.exec(select(func.count()).select_from(Game).where(Game.owner_id == target.id)).one()
    if count + len(games) > MAX_OWNED_GAMES:
        raise Problem(409, "GAME_LIMIT_REACHED", "The account cannot exceed the ten-game ownership limit")
    if games:
        active = session.exec(select(AdvanceOperation.id).where(
            AdvanceOperation.game_id.in_([game.id for game in games]),
            AdvanceOperation.status.in_(ACTIVE),
        ).limit(1)).first()
        if active:
            raise Problem(409, "ADVANCE_IN_PROGRESS", "Wait for guest advances to finish before claiming")
    records = []
    for game in games:
        records.append({"game_id": game.id, "from_revision": game.revision, "to_revision": game.revision + 1})
        game.owner_id = target.id
        game.revision += 1
        session.add(game)
    document = canonical_json(records)
    receipt = GuestClaim(
        guest_principal_id=source.principal_id, account_principal_id=target.id,
        source_session_id=source.id, account_session_id=account_session.id,
        authentication_id=authentication.id, games_json=document, games_sha256=content_digest(document),
    )
    session.add(receipt)
    # Receipts and simulator audit remain under their original principal/checkpoint.
    # A permanent claim marker prevents in-flight or recreated guest credentials from writing.
    session.execute(update(GuestSession).where(
        GuestSession.principal_id == source.principal_id, GuestSession.revoked_at.is_(None),
    ).values(revoked_at=datetime.utcnow()).execution_options(synchronize_session=False))
    return receipt


def claimed_game_count(receipt):
    if receipt is None:
        return 0
    try:
        body = receipt.games_json
        if len(body) > 16 * 1024 or content_digest(body) != receipt.games_sha256:
            raise ValueError()
        values = json.loads(body)
        if not isinstance(values, list) or len(values) > MAX_OWNED_GAMES or canonical_json(values) != body:
            raise ValueError()
        ids = []
        for record in values:
            if (not isinstance(record, dict) or set(record) != {"game_id", "from_revision", "to_revision"}
                    or not isinstance(record["game_id"], str) or not 1 <= len(record["game_id"]) <= 128
                    or type(record["from_revision"]) is not int or type(record["to_revision"]) is not int
                    or record["from_revision"] < 1 or record["to_revision"] != record["from_revision"] + 1):
                raise ValueError()
            ids.append(record["game_id"])
        if ids != sorted(set(ids)):
            raise ValueError()
        return len(values)
    except (ValueError, TypeError, RecursionError):
        raise Problem(503, "CLAIM_INTEGRITY_ERROR", "Guest transfer receipt failed integrity validation") from None


def complete_authentication(session, settings, browser_token, token, supplied_csrf):
    flow = read_flow(session, browser_token, settings)
    expected = derived_token(browser_token, "csrf:" + flow.id)
    if not supplied_csrf or not hmac.compare_digest(supplied_csrf.encode("utf-8"), expected.encode()):
        raise Problem(403, "CSRF_FAILED", "A valid authentication X-CSRF-Token is required")
    if flow.status not in ("verified", "completed"):
        raise Problem(409, "OIDC_NOT_VERIFIED", "Complete Google verification before confirming")
    new_token = derived_token(browser_token, "session:" + flow.id)
    if flow.status == "completed":
        previous = session.get(GuestSession, flow.completed_session_id)
        target_id = previous.principal_id
    else:
        candidate = Principal(kind="registered", oidc_issuer=flow.issuer, oidc_subject=flow.subject)
        insert_missing(session, Principal, candidate.dict(), ["oidc_issuer", "oidc_subject"])
        target_id = session.exec(select(Principal.id).where(
            Principal.oidc_issuer == flow.issuer, Principal.oidc_subject == flow.subject,
        )).one()
    # No flow row is held while acquiring principals. Competing claims use the same order.
    for principal_id in sorted({value for value in (target_id, flow.source_principal_id) if value}):
        session.execute(update(Principal).where(Principal.id == principal_id).values(id=Principal.id))
    target = session.get(Principal, target_id)
    flow = read_flow(session, browser_token, settings, lock=True)
    if target.sessions_revoked_at and flow.created_at <= target.sessions_revoked_at:
        raise Problem(409, "OIDC_AUTHORIZATION_REVOKED", "Start a new authentication after session revocation")
    if flow.status == "completed":
        account = session.exec(select(GuestSession).where(
            GuestSession.id == flow.completed_session_id,
        ).with_for_update().execution_options(populate_existing=True)).one()
        if (account.revoked_at is not None or account.expires_at <= datetime.utcnow()
                or account.token_hash != session_hash(new_token)
                or cookie_hash(token) not in (flow.source_token_hash, account.token_hash)):
            raise Problem(409, "OIDC_SESSION_CHANGED", "The completed session is no longer available")
        receipt = session.exec(select(GuestClaim).where(GuestClaim.authentication_id == flow.id)).first()
        if (flow.intent == "guest_claim") != (receipt is not None):
            raise Problem(503, "CLAIM_INTEGRITY_ERROR", "Guest transfer receipt is unavailable")
        claimed_game_count(receipt)
        session.commit()
        return target, account, new_token, receipt
    if flow.status != "verified":
        raise Problem(409, "OIDC_NOT_VERIFIED", "Complete Google verification before confirming")
    source = require_source(session, flow, token)
    if source:
        source = session.exec(select(GuestSession).where(
            GuestSession.id == source.id,
        ).with_for_update().execution_options(populate_existing=True)).one()
        if source.revoked_at is not None or source.expires_at <= datetime.utcnow():
            raise Problem(401, "AUTHENTICATION_REQUIRED", "The original session is no longer active")
        if flow.intent == "reauthenticate" and source.principal_id != target.id:
            raise Problem(409, "ACCOUNT_SWITCH_FORBIDDEN", "Reauthenticate with the same Google account")
    elif flow.intent != "sign_in":
        raise Problem(409, "OIDC_SESSION_CHANGED", "The original browser session is required")
    now = datetime.utcnow()
    account = GuestSession(
        principal_id=target.id, token_hash=session_hash(new_token), created_at=now,
        authenticated_at=flow.verified_at,
        expires_at=now + timedelta(seconds=settings.registered_session_seconds),
    )
    session.add(account)
    session.flush()
    receipt = None
    if flow.intent == "guest_claim":
        receipt = claim_games(session, source, target, flow, account)
    elif source:
        source.revoked_at = now
        session.add(source)
    flow.status, flow.completed_session_id = "completed", account.id
    session.add(flow)
    session.add(IdentityAudit(
        principal_id=target.id, session_id=account.id, kind="guest_claim" if receipt else "sign_in",
        authentication_id=flow.id, affected_sessions=1 if source else 0,
    ))
    # Recheck the authorization deadline after waiting for account/game locks.
    if flow.expires_at <= datetime.utcnow():
        raise Problem(401, "OIDC_FLOW_EXPIRED", "Start a new authentication")
    claimed_game_count(receipt)
    session.commit()
    return target, account, new_token, receipt


def require_recent_authentication(current, settings):
    if current.authenticated_at is None:
        raise Problem(409, "REGISTERED_ACCOUNT_REQUIRED", "Save this guest to an account first")
    if current.authenticated_at <= datetime.utcnow() - timedelta(seconds=settings.recent_auth_seconds):
        raise Problem(403, "REAUTHENTICATION_REQUIRED", "Sign in again before revoking other sessions")


def revoke_sessions(session, current, *, scope=None, target_id=None):
    now = datetime.utcnow()
    if scope is not None:
        session.execute(update(Principal).where(
            Principal.id == current.principal_id,
        ).values(sessions_revoked_at=now))
    query = update(GuestSession).where(
        GuestSession.principal_id == current.principal_id,
        GuestSession.revoked_at.is_(None), GuestSession.expires_at > now,
    )
    if target_id:
        query = query.where(GuestSession.id == target_id)
    elif scope == "other":
        query = query.where(GuestSession.id != current.id)
    result = session.execute(query.values(revoked_at=now).execution_options(synchronize_session=False))
    session.add(IdentityAudit(
        principal_id=current.principal_id, session_id=current.id, target_session_id=target_id,
        kind=("sign_out" if target_id == current.id else "session_revoked") if target_id else "sessions_revoked",
        affected_sessions=result.rowcount,
    ))
    session.commit()
    return result.rowcount
