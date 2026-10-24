"""First /v1 slice: identity, disclosed rules, catalog, and qualified release reads."""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import and_, delete, or_, update
from sqlmodel import Session, select

from finance_app_database_service.catalog import insert_missing
from finance_app_database_service.contracts import (
    Capabilities, GuestCreate, GuestView, InstrumentDetail, InstrumentPage, InstrumentView,
    ReleaseDetail, ReleaseManifest, ReleasePage, ReleaseSummary,
    RulesDefinition, RulesetPage, RulesetView, canonical_json, content_digest,
)
from finance_app_database_service.foundation_models import (
    DatasetRelease, GuestCreationWindow, GuestSession, Instrument, InstrumentAlias,
    LegacyTickerMap, Principal, Ruleset,
)
from finance_app_database_service.pagination import cursor_scope, decode_cursor, encode_cursor
from finance_app_database_service.identity_contracts import IdentityView
from finance_app_database_service.identity_models import IdentityAudit
from finance_app_database_service.problems import PROBLEM_RESPONSES, Problem
from finance_app_database_service.security import (
    SESSION_COOKIE, csrf_token, get_current_session, get_current_principal, get_v1_session,
    require_guest_write, require_origin, session_cookie, session_hash,
)
from finance_app_database_service.settings import SecuritySettings, get_security_settings

router = APIRouter(prefix="/v1", responses=PROBLEM_RESPONSES)


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc)


def identity_view(principal: Principal, guest: GuestSession, token: str, games_enabled=False,
                  google_enabled=False) -> IdentityView:
    return IdentityView(
        principal_id=principal.id, kind=principal.kind, created_at=utc(principal.created_at),
        session_expires_at=utc(guest.expires_at), csrf_token=csrf_token(token, guest.id),
        capabilities=Capabilities(create_games=games_enabled, claim_guest=google_enabled and principal.kind == "guest"),
    )


def consume_guest_creation(request: Request, session: Session, settings: SecuritySettings):
    if len(settings.guest_rate_limit_key) < 32:
        raise Problem(503, "GUEST_SESSIONS_DISABLED", "Guest session creation is not configured")
    now = datetime.utcnow()
    window_seconds = settings.guest_creation_window_seconds
    window = int(now.replace(tzinfo=timezone.utc).timestamp()) // window_seconds
    expires = datetime.utcfromtimestamp((window + 1) * window_seconds)
    client = request.client.host if request.client else "unknown"
    key_hash = hmac.new(
        settings.guest_rate_limit_key.encode(), f"{window}:{client}".encode(), hashlib.sha256,
    ).hexdigest()
    insert_missing(session, GuestCreationWindow, {
        "key_hash": key_hash, "used": 0, "expires_at": expires,
    }, ["key_hash"])
    updated = session.execute(update(GuestCreationWindow).where(
        GuestCreationWindow.key_hash == key_hash,
        GuestCreationWindow.used < settings.guest_creation_limit,
    ).values(used=GuestCreationWindow.used + 1))
    if updated.rowcount != 1:
        retry = max(1, int((expires - now).total_seconds()) + 1)
        raise Problem(429, "GUEST_RATE_LIMITED", "Too many guest sessions",
                      {"Retry-After": str(retry)})
    expired_keys = select(GuestCreationWindow.key_hash).where(
        GuestCreationWindow.expires_at <= now,
    ).limit(100)
    session.execute(delete(GuestCreationWindow).where(
        GuestCreationWindow.key_hash.in_(expired_keys),
    ).execution_options(synchronize_session=False))


@router.post("/guest-sessions", response_model=GuestView, status_code=201, tags=["Identity"])
def create_guest(
    payload: GuestCreate, request: Request, response: Response,
    origin=Depends(require_origin),
    token: Optional[str] = Depends(session_cookie),
    settings: SecuritySettings = Depends(get_security_settings),
    session: Session = Depends(get_v1_session),
):
    if token:
        try:
            get_current_session(token, session)
        except Problem as exc:
            if exc.status != 401:
                raise
        else:
            raise Problem(409, "SESSION_ALREADY_ACTIVE", "Revoke the current session first")
    consume_guest_creation(request, session, settings)
    token = secrets.token_urlsafe(32)
    principal = Principal()
    guest = GuestSession(
        principal_id=principal.id, token_hash=session_hash(token),
        expires_at=datetime.utcnow() + timedelta(seconds=settings.guest_session_seconds),
    )
    session.add(principal)
    session.flush()
    session.add(guest)
    session.commit()
    response.set_cookie(
        SESSION_COOKIE, token, max_age=settings.guest_session_seconds,
        secure=True, httponly=True, samesite="lax", path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return identity_view(principal, guest, token, settings.games_enabled, settings.google_auth_enabled)


@router.get("/me", response_model=IdentityView, tags=["Identity"])
def read_me(
    response: Response,
    principal: Principal = Depends(get_current_principal),
    guest: GuestSession = Depends(get_current_session),
    token: str = Depends(session_cookie),
    settings: SecuritySettings = Depends(get_security_settings),
):
    response.headers["Cache-Control"] = "no-store"
    return identity_view(principal, guest, token, settings.games_enabled, settings.google_auth_enabled)


@router.delete("/guest-sessions/current", status_code=204, response_class=Response,
               tags=["Identity"])
def revoke_guest(
    guest: GuestSession = Depends(require_guest_write),
    session: Session = Depends(get_v1_session),
):
    guest.revoked_at = datetime.utcnow()
    session.add(guest)
    session.add(IdentityAudit(
        principal_id=guest.principal_id, session_id=guest.id, kind="sign_out",
        target_session_id=guest.id, affected_sessions=1,
    ))
    session.commit()
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


def ruleset_view(row: Ruleset) -> RulesetView:
    try:
        definition = RulesDefinition.parse_raw(row.definition_json)
        if row.id != definition.id or content_digest(canonical_json(definition)) != row.sha256:
            raise ValueError()
        return RulesetView(id=row.id, sha256=row.sha256, definition=definition)
    except (ValueError, ValidationError):
        raise Problem(503, "RULESET_INTEGRITY_ERROR", "Ruleset integrity validation failed") from None


@router.get("/rulesets", response_model=RulesetPage, tags=["Rules"])
def read_rulesets(session: Session = Depends(get_v1_session)):
    rows = session.exec(select(Ruleset).order_by(Ruleset.id).limit(100)).all()
    return RulesetPage(items=[ruleset_view(row) for row in rows])


@router.get("/rulesets/{ruleset_id}", response_model=RulesetView, tags=["Rules"])
def read_ruleset(ruleset_id: str, session: Session = Depends(get_v1_session)):
    row = session.get(Ruleset, ruleset_id)
    if row is None:
        raise Problem(404, "RULESET_NOT_FOUND", "Ruleset not found")
    return ruleset_view(row)


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/instruments", response_model=InstrumentPage, tags=["Instruments"])
def read_instruments(
    query: str = Query(default="", max_length=80),
    exchange: str = Query(default="", max_length=64),
    cursor: Optional[str] = Query(default=None, max_length=1024),
    limit: int = Query(default=50, ge=1, le=100),
    session: Session = Depends(get_v1_session),
):
    query, exchange = query.strip().upper(), exchange.strip().upper()
    scope = cursor_scope("instruments", query=query, exchange=exchange)
    statement = select(Instrument)
    if query:
        pattern = "%" + escape_like(query) + "%"
        alias = select(InstrumentAlias.instrument_id).where(
            InstrumentAlias.instrument_id == Instrument.id,
            InstrumentAlias.symbol.ilike(pattern, escape="\\"),
        ).exists()
        statement = statement.where(or_(
            Instrument.symbol.ilike(pattern, escape="\\"),
            Instrument.name.ilike(pattern, escape="\\"), alias,
        ))
    if exchange:
        statement = statement.where(Instrument.exchange == exchange)
    if cursor:
        symbol, instrument_id = decode_cursor(cursor, scope, 2)
        statement = statement.where(or_(
            Instrument.symbol > symbol,
            and_(Instrument.symbol == symbol, Instrument.id > instrument_id),
        ))
    rows = session.exec(statement.order_by(Instrument.symbol, Instrument.id).limit(limit + 1)).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return InstrumentPage(
        items=[instrument_view(row) for row in rows],
        next_cursor=encode_cursor(scope, rows[-1].symbol, rows[-1].id) if more else None,
    )


def instrument_view(row: Instrument) -> InstrumentView:
    return InstrumentView(**row.dict(exclude={"created_at"}))


@router.get("/instruments/{instrument_id}", response_model=InstrumentDetail, tags=["Instruments"])
def read_instrument(instrument_id: str, session: Session = Depends(get_v1_session)):
    row = session.get(Instrument, instrument_id)
    if row is None:
        raise Problem(404, "INSTRUMENT_NOT_FOUND", "Instrument not found")
    aliases = session.exec(select(InstrumentAlias.symbol).where(
        InstrumentAlias.instrument_id == row.id,
    ).order_by(InstrumentAlias.symbol)).all()
    legacy_ids = session.exec(select(LegacyTickerMap.ticker_id).where(
        LegacyTickerMap.instrument_id == row.id,
    ).order_by(LegacyTickerMap.ticker_id)).all()
    return InstrumentDetail(**instrument_view(row).dict(), aliases=sorted(set(aliases)),
                            legacy_ticker_ids=legacy_ids)


def release_manifest(row: DatasetRelease) -> ReleaseManifest:
    try:
        if len(row.manifest_json) > 8 * 1024 * 1024:
            raise ValueError()
        manifest = ReleaseManifest.parse_raw(row.manifest_json)
        digest = content_digest(canonical_json(manifest))
        if row.sha256 != digest or row.id != "rel_" + digest:
            raise ValueError()
        return manifest
    except (ValueError, ValidationError):
        raise Problem(503, "RELEASE_INTEGRITY_ERROR", "Release integrity validation failed") from None


def release_summary(row: DatasetRelease, manifest: ReleaseManifest) -> ReleaseSummary:
    return ReleaseSummary(
        id=row.id, sha256=row.sha256, published_at=utc(row.published_at),
        qualification=manifest.qualification, currency=manifest.currency,
        price_basis=manifest.price_basis, calendar_id=manifest.calendar_id,
        calendar_version=manifest.calendar_version, from_date=manifest.from_date,
        to_date=manifest.to_date, instrument_count=len(manifest.instruments),
    )


def published_releases():
    return select(DatasetRelease).where(
        DatasetRelease.qualification == "simulation_qualified",
        DatasetRelease.published_at.is_not(None),
    )


@router.get("/market-data/releases", response_model=ReleasePage, tags=["Market releases"])
def read_releases(
    cursor: Optional[str] = Query(default=None, max_length=1024),
    limit: int = Query(default=10, ge=1, le=20),
    session: Session = Depends(get_v1_session),
):
    scope = cursor_scope("releases")
    statement = published_releases()
    if cursor:
        after, = decode_cursor(cursor, scope, 1)
        statement = statement.where(DatasetRelease.id > after)
    rows = session.exec(statement.order_by(DatasetRelease.id).limit(limit + 1)).all()
    items = [release_summary(row, release_manifest(row)) for row in rows[:limit]]
    return ReleasePage(
        items=items,
        next_cursor=encode_cursor(scope, rows[limit - 1].id) if len(rows) > limit else None,
    )


@router.get("/market-data/releases/{release_id}", response_model=ReleaseDetail,
            tags=["Market releases"])
def read_release(release_id: str, session: Session = Depends(get_v1_session)):
    row = session.exec(published_releases().where(DatasetRelease.id == release_id)).first()
    if row is None:
        raise Problem(404, "RELEASE_NOT_FOUND", "Published qualified release not found")
    manifest = release_manifest(row)
    return ReleaseDetail(**release_summary(row, manifest).dict(), manifest=manifest)
