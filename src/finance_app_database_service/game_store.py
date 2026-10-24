"""Checkpoint serialization and atomic audit projection, without implicit commits."""

import json
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal

from pydantic import parse_obj_as
from sqlmodel import select

from finance_app_database_service.accounting import reconcile
from finance_app_database_service.contracts import canonical_json, content_digest
from finance_app_database_service.game_models import (
    GameEvent, GameFill, GameLedger, GameOrder, GamePosting, GameSnapshot,
)
from finance_app_database_service.problems import Problem
from finance_app_database_service.simulation import State

MAX_CHECKPOINT_BYTES = 16 * 1024 * 1024


def wire(value):
    if isinstance(value, Decimal):
        return format(value, ".8f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if is_dataclass(value):
        return {field.name: wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {key: wire(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [wire(item) for item in value]
    return value


def encode(value):
    return canonical_json(wire(value))


def load_state(game):
    try:
        if len(game.state_json) > MAX_CHECKPOINT_BYTES or content_digest(game.state_json) != game.state_sha256:
            raise ValueError()
        envelope = json.loads(game.state_json)
        if set(envelope) != {"version", "state"} or type(envelope["version"]) is not int or envelope["version"] != 1:
            raise ValueError()
        document = envelope["state"]
        state = parse_obj_as(State, document)
        if wire(state) != document or not state.snapshots or state.snapshots[-1].date != state.cursor:
            raise ValueError()
        if any(getattr(state, name) != getattr(game, name) for name in (
            "release_id", "ruleset_id", "start_date", "end_date", "cursor", "initial_cash", "status",
        )):
            raise ValueError()
        reconcile(state.book)
        if [item.sequence for item in state.events] != list(range(1, len(state.events) + 1)):
            raise ValueError()
        return state
    except (ValueError, TypeError, KeyError, ArithmeticError):
        raise Problem(503, "GAME_INTEGRITY_ERROR", "Game checkpoint integrity validation failed") from None


def save_state(session, game, state, previous=None):
    """Append audit deltas and replace the checkpoint inside the caller's transaction."""
    reconcile(state.book)
    encoded = canonical_json({"version": 1, "state": wire(state)})
    if len(encoded) > MAX_CHECKPOINT_BYTES:
        raise Problem(409, "GAME_CAPACITY_REACHED", "Game checkpoint capacity reached")
    if previous:
        for old, new in (
            (previous.book.ledger, state.book.ledger), (previous.fills, state.fills),
            (previous.snapshots, state.snapshots), (previous.events, state.events),
        ):
            if tuple(new[:len(old)]) != tuple(old):
                raise ValueError("Checkpoint history is append-only")
    game.state_json, game.state_sha256 = encoded, content_digest(encoded)
    game.cursor, game.status = state.cursor, state.status
    session.add(game)
    session.flush()
    old_orders = previous.orders if previous else ()
    for index, order in enumerate(state.orders, 1):
        if index <= len(old_orders) and order == old_orders[index - 1]:
            continue
        row = session.get(GameOrder, (game.id, index))
        if row is None:
            row = GameOrder(game_id=game.id, sequence=index, order_id=order.id,
                            status=order.status, body_json=encode(order))
        else:
            row.status, row.body_json = order.status, encode(order)
        session.add(row)
    old_ledger = len(previous.book.ledger) if previous else 0
    for index, entry in enumerate(state.book.ledger[old_ledger:], old_ledger + 1):
        session.add(GameLedger(game_id=game.id, sequence=index, key=entry.key, body_json=encode(entry)))
        session.flush()
        for posting_index, posting in enumerate(entry.postings, 1):
            session.add(GamePosting(
                game_id=game.id, ledger_sequence=index, sequence=posting_index,
                account=posting.account, amount=posting.amount,
            ))
    for model, items, old_items in (
        (GameFill, state.fills, previous.fills if previous else ()),
        (GameEvent, state.events, previous.events if previous else ()),
    ):
        for index, item in enumerate(items[len(old_items):], len(old_items) + 1):
            session.add(model(game_id=game.id, sequence=index, body_json=encode(item)))
    old_snapshots = len(previous.snapshots) if previous else 0
    for snapshot in state.snapshots[old_snapshots:]:
        session.add(GameSnapshot(game_id=game.id, date=snapshot.date, body_json=encode(snapshot)))


def audit_page(session, model, game_id, after, limit, ceiling):
    rows = session.exec(select(model).where(
        model.game_id == game_id, model.sequence > after, model.sequence <= ceiling,
    ).order_by(model.sequence).limit(limit + 1)).all()
    return rows[:limit], len(rows) > limit
