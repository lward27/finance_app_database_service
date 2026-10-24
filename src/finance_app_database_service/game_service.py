"""Ownership, serialized idempotency admission, revisions, and game creation."""

import json
import re
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi.responses import JSONResponse
from sqlalchemy import func, update
from sqlmodel import select

from finance_app_database_service.contracts import canonical_json, content_digest
from finance_app_database_service.foundation_api import published_releases, ruleset_view
from finance_app_database_service.foundation_models import DatasetRelease, Principal, Ruleset
from finance_app_database_service.identity_models import GuestClaim
from finance_app_database_service.game_market import load_market
from finance_app_database_service.game_models import AdvanceOperation, Game, GameCommand
from finance_app_database_service.game_store import save_state, wire
from finance_app_database_service.problems import Problem

ACTIVE = ("queued", "running")
MAX_OWNED_GAMES = 10
KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def etag(game):
    return f'"{game.id}:{game.revision}"'


def owned_game(session, owner_id, game_id):
    game = session.exec(select(Game).where(Game.id == game_id, Game.owner_id == owner_id)).first()
    if game is None:
        raise Problem(404, "GAME_NOT_FOUND", "Game not found")
    return game


def lock_game(session, game_id):
    # A write lock also works on SQLite, where SELECT FOR UPDATE is a no-op.
    session.execute(update(Game).where(Game.id == game_id).values(
        revision=Game.revision,
    ).execution_options(synchronize_session=False))
    return session.exec(select(Game).where(Game.id == game_id).execution_options(populate_existing=True)).one()


def active_operation(session, game_id):
    return session.exec(select(AdvanceOperation).where(
        AdvanceOperation.game_id == game_id, AdvanceOperation.status.in_(ACTIVE),
    )).first()


def game_view(session, game):
    operation = active_operation(session, game.id)
    allowed = []
    if not game.archived and operation is None and game.status in ("ready", "margin_call"):
        allowed = ["cancel_order", "reduce_position", "resolve_margin"] if game.status == "margin_call" else [
            "queue_order", "cancel_order", "advance",
        ]
    return dict(
        id=game.id, name=game.name, revision=game.revision,
        status="advancing" if operation else game.status, archived=game.archived,
        requested_start=game.requested_start.isoformat(), start_date=game.start_date.isoformat(),
        end_date=game.end_date.isoformat(), cursor=game.cursor.isoformat(),
        initial_cash=format(game.initial_cash, ".8f"), currency="USD",
        ruleset_id=game.ruleset_id, release_id=game.release_id,
        active_operation_id=operation.id if operation else None, allowed_actions=allowed,
    )


def operation_view(operation, game):
    return dict(
        id=operation.id, game_id=game.id, kind=operation.kind, status=operation.status,
        requested_target=operation.requested_target.isoformat(),
        effective_target=operation.effective_target.isoformat(), actual_cursor=operation.actual_cursor.isoformat(),
        game_status=operation.game_status, revision=operation.game_revision, code=operation.code,
    )


def response(body, game=None, *, status=200, location=None, stored_etag=None):
    headers = {"Cache-Control": "no-store"}
    if game is not None or stored_etag:
        headers["ETag"] = stored_etag or etag(game)
    if location:
        headers["Location"] = location
    return JSONResponse(wire(body), status_code=status, headers=headers)


def command(session, owner_id, key, route, payload, if_match, game_id, handler):
    if not key or not KEY_PATTERN.fullmatch(key):
        raise Problem(422, "INVALID_IDEMPOTENCY_KEY", "A bounded ASCII Idempotency-Key is required")
    # Serializes receipt lookup + command admission across replicas for this owner.
    # Workers never lock principals, so all game/operation locks follow one order.
    session.execute(update(Principal).where(Principal.id == owner_id).values(id=Principal.id))
    if session.get(GuestClaim, owner_id) is not None:
        raise Problem(401, "AUTHENTICATION_REQUIRED", "This guest has been claimed")
    game = None
    if game_id:
        owned_game(session, owner_id, game_id)
        game = lock_game(session, game_id)
    key_hash = content_digest(key)
    digest = content_digest(canonical_json({"route": route, "payload": wire(payload.dict())}))
    receipt = session.get(GameCommand, (owner_id, key_hash))
    if receipt:
        if receipt.request_sha256 != digest:
            raise Problem(409, "IDEMPOTENCY_CONFLICT", "Idempotency key was used for a different command")
        result = response(json.loads(receipt.response_json), status=receipt.status_code,
                          location=receipt.location, stored_etag=receipt.etag)
        session.rollback()
        return result
    if game:
        if if_match is None:
            raise Problem(428, "GAME_REVISION_REQUIRED", "If-Match is required", {"ETag": etag(game)})
        if if_match != etag(game):
            raise Problem(412, "GAME_REVISION_MISMATCH", "Game revision is stale", {"ETag": etag(game)})
        if game.archived:
            raise Problem(409, "GAME_ARCHIVED", "Archived games are frozen")
        if active_operation(session, game.id):
            raise Problem(409, "ADVANCE_IN_PROGRESS", "An advance is already active")
    used = session.exec(select(func.count()).select_from(GameCommand).where(
        GameCommand.principal_id == owner_id,
        GameCommand.created_at > datetime.utcnow() - timedelta(minutes=10),
    )).one()
    if used >= 60:
        raise Problem(429, "GAME_COMMAND_RATE_LIMITED", "Too many game commands", {"Retry-After": "600"})
    game, body, status, location = handler(game)
    session.flush()
    receipt = GameCommand(
        principal_id=owner_id, key_hash=key_hash, request_sha256=digest,
        response_json=canonical_json(wire(body)), status_code=status,
        etag=etag(game), location=location,
    )
    session.add(receipt)
    session.commit()
    return response(body, status=status, location=location, stored_etag=receipt.etag)


def create_game(session, owner_id, payload):
    count = session.exec(select(func.count()).select_from(Game).where(Game.owner_id == owner_id)).one()
    if count >= MAX_OWNED_GAMES:
        raise Problem(409, "GAME_LIMIT_REACHED", "This principal has reached the initial ten-game limit")
    rules = session.get(Ruleset, payload.ruleset_id)
    if rules is None:
        raise Problem(503, "RULESET_UNAVAILABLE", "Ruleset is unavailable")
    ruleset_view(rules)
    release_id = payload.release_id
    if release_id is None:
        release = session.exec(published_releases().order_by(
            # A deterministic publication order, never silently fall back from a corrupt release.
            DatasetRelease.published_at.desc(), DatasetRelease.id,
        ).limit(1)).first()
        if release is None:
            raise Problem(503, "QUALIFIED_RELEASE_UNAVAILABLE", "No qualified release is published")
        release_id = release.id
    market = load_market(session, release_id)
    index = bisect_left(market.sessions, payload.start_date)
    if index >= len(market.sessions) or payload.start_date < market.sessions[0]:
        raise Problem(422, "INVALID_GAME_DATES", "Start is outside the qualified calendar")
    start = market.sessions[index]
    end = market.sessions[-1]
    if payload.end_date:
        if payload.end_date > end:
            raise Problem(422, "INVALID_GAME_DATES", "End is outside the qualified calendar")
        end_index = bisect_right(market.sessions, payload.end_date) - 1
        if end_index < 0:
            raise Problem(422, "INVALID_GAME_DATES", "End is outside the qualified calendar")
        end = market.sessions[end_index]
    state = market.create(start, end, Decimal(payload.initial_cash))
    game = Game(
        owner_id=owner_id, name=payload.name, release_id=market.release_id, ruleset_id=state.ruleset_id,
        requested_start=payload.start_date, start_date=start, end_date=end, cursor=start,
        initial_cash=state.initial_cash, state_json="", state_sha256="",
    )
    save_state(session, game, state)
    session.flush()
    return game
