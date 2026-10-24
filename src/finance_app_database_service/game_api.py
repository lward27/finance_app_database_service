"""Owned single-player resources and asynchronous, revision-checked commands."""

import json
from bisect import bisect_right
from datetime import date
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, Query
from sqlmodel import Session, select

from finance_app_database_service.accounting import decimal_calculation
from finance_app_database_service.contracts import InstrumentView
from finance_app_database_service.financial import money, notional_quantity
from finance_app_database_service.foundation_models import GuestSession, Instrument, Principal
from finance_app_database_service.game_contracts import (
    AdvanceCreate, EmptyCommand, GameCreate, GameRename, MarginResolution, OrderCreate,
    EventView, FillView, GameView, HistoryPoint, LedgerView, OperationView, OrderView,
    Page, PerformancePoint, PortfolioView, PositionView, PreviewView, QuotesView,
)
from finance_app_database_service.game_market import load_market
from finance_app_database_service.game_models import (
    AdvanceOperation, Game, GameEvent, GameFill, GameLedger,
)
from finance_app_database_service.game_service import (
    ACTIVE, active_operation, command, create_game, game_view, operation_view, owned_game, response,
)
from finance_app_database_service.game_store import audit_page, load_state, save_state, wire
from finance_app_database_service.pagination import cursor_scope, decode_cursor, encode_cursor
from finance_app_database_service.problems import PROBLEM_RESPONSES, Problem
from finance_app_database_service.security import (
    get_current_principal, get_v1_session, require_session_write,
)
from finance_app_database_service.settings import SecuritySettings, get_security_settings
from finance_app_database_service.simulation import SimulationError, shifted_date


def games_enabled(settings: SecuritySettings = Depends(get_security_settings)):
    if not settings.games_enabled:
        raise Problem(503, "GAMES_DISABLED", "Games are not enabled")


router = APIRouter(prefix="/v1/games", tags=["Games"], responses=PROBLEM_RESPONSES,
                   dependencies=[Depends(games_enabled)])


def domain(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except SimulationError as exc:
        status = 422 if exc.code.startswith(("INVALID_", "ADVANCE_LIMIT", "INSTRUMENT_")) else 409
        raise Problem(status, exc.code, "Game command cannot be applied") from None
    except (ValueError, ArithmeticError):
        raise Problem(422, "INVALID_FINANCIAL_VALUE", "Financial value exceeds simulation limits") from None


def sequence_after(cursor, scope):
    if not cursor:
        return 0
    value, = decode_cursor(cursor, scope, 1)
    if not value.isascii() or not value.isdigit() or len(value) > 12:
        raise Problem(422, "INVALID_CURSOR", "Invalid cursor")
    return int(value)


def sized_page(items, scope, after, limit):
    values = items[after:after + limit]
    return dict(items=wire(values), next_cursor=(
        encode_cursor(scope, str(after + limit)) if len(items) > after + limit else None
    ))


@router.post("", status_code=201, response_model=GameView)
def post_game(
    payload: GameCreate, guest: GuestSession = Depends(require_session_write),
    key: Optional[str] = Header(None, alias="Idempotency-Key"), session: Session = Depends(get_v1_session),
):
    def apply(_):
        game = domain(create_game, session, guest.principal_id, payload)
        return game, game_view(session, game), 201, f"/v1/games/{game.id}"
    return command(session, guest.principal_id, key, "POST /v1/games", payload, None, None, apply)


@router.get("", response_model=Page[GameView])
def list_games(
    status: Optional[Literal["ready", "advancing", "margin_call", "completed", "bankrupt"]] = None,
    include_archived: bool = False, cursor: Optional[str] = Query(None, max_length=1024),
    limit: int = Query(20, ge=1, le=50), principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_v1_session),
):
    scope = cursor_scope("games", owner=principal.id, status=status, archived=include_archived)
    statement = select(Game).where(Game.owner_id == principal.id)
    if not include_archived:
        statement = statement.where(Game.archived.is_(False))
    if status:
        active_ids = select(AdvanceOperation.game_id).where(AdvanceOperation.status.in_(ACTIVE))
        if status == "advancing":
            statement = statement.where(Game.id.in_(active_ids))
        else:
            statement = statement.where(Game.status == status, Game.id.not_in(active_ids))
    if cursor:
        after, = decode_cursor(cursor, scope, 1)
        statement = statement.where(Game.id > after)
    rows = session.exec(statement.order_by(Game.id).limit(limit + 1)).all()
    return response(dict(items=[game_view(session, game) for game in rows[:limit]],
                         next_cursor=encode_cursor(scope, rows[limit - 1].id) if len(rows) > limit else None))


@router.get("/{game_id}", response_model=GameView)
def get_game(game_id: str, principal: Principal = Depends(get_current_principal),
             session: Session = Depends(get_v1_session)):
    game = owned_game(session, principal.id, game_id)
    return response(game_view(session, game), game)


def mutate_game(session, guest, game_id, key, match, route, payload, function):
    def apply(game):
        state = load_state(game)
        updated = domain(function, game, state)
        if updated is not None:
            save_state(session, game, updated, state)
        game.revision += 1
        session.add(game)
        session.flush()
        return game, game_view(session, game), 200, None
    return command(session, guest.principal_id, key, route, payload, match, game_id, apply)


@router.patch("/{game_id}", response_model=GameView)
def rename_game(
    game_id: str, payload: GameRename, guest: GuestSession = Depends(require_session_write),
    key: Optional[str] = Header(None, alias="Idempotency-Key"),
    match: Optional[str] = Header(None, alias="If-Match"), session: Session = Depends(get_v1_session),
):
    def rename(game, state):
        game.name = payload.name
    return mutate_game(session, guest, game_id, key, match, f"PATCH /v1/games/{game_id}", payload, rename)


@router.post("/{game_id}/archives", response_model=GameView)
def archive_game(
    game_id: str, payload: EmptyCommand, guest: GuestSession = Depends(require_session_write),
    key: Optional[str] = Header(None, alias="Idempotency-Key"),
    match: Optional[str] = Header(None, alias="If-Match"), session: Session = Depends(get_v1_session),
):
    def archive(game, state):
        game.archived = True
    return mutate_game(session, guest, game_id, key, match, f"POST /v1/games/{game_id}/archives", payload, archive)


@router.post("/{game_id}/replays", status_code=201, response_model=GameView)
def replay_game(
    game_id: str, payload: EmptyCommand, guest: GuestSession = Depends(require_session_write),
    key: Optional[str] = Header(None, alias="Idempotency-Key"),
    match: Optional[str] = Header(None, alias="If-Match"), session: Session = Depends(get_v1_session),
):
    def apply(original):
        game = domain(create_game, session, guest.principal_id, GameCreate(
            name=original.name, start_date=original.requested_start, end_date=original.end_date,
            initial_cash=format(original.initial_cash, ".8f"), release_id=original.release_id,
        ))
        return game, game_view(session, game), 201, f"/v1/games/{game.id}"
    return command(session, guest.principal_id, key, f"POST /v1/games/{game_id}/replays",
                   payload, match, game_id, apply)


def queue_payload(market, state, payload):
    return market.queue(
        state, payload.instrument_id, payload.action,
        quantity=Decimal(payload.quantity) if payload.quantity is not None else None,
        notional=Decimal(payload.notional) if payload.notional is not None else None,
        close_all=payload.close_all,
    )


@router.post("/{game_id}/orders", status_code=201, response_model=OrderView)
def post_order(
    game_id: str, payload: OrderCreate, guest: GuestSession = Depends(require_session_write),
    key: Optional[str] = Header(None, alias="Idempotency-Key"),
    match: Optional[str] = Header(None, alias="If-Match"), session: Session = Depends(get_v1_session),
):
    def apply(game):
        state = load_state(game)
        if len(state.orders) >= 1000:
            raise Problem(409, "ORDER_LIMIT_REACHED", "Game order capacity reached")
        updated = domain(queue_payload, load_market(session, game.release_id), state, payload)
        save_state(session, game, updated, state)
        game.revision += 1
        session.add(game)
        order = wire(updated.orders[-1])
        order.update(fill=None, revision=game.revision)
        return game, order, 201, f"/v1/games/{game.id}/orders/{updated.orders[-1].id}"
    return command(session, guest.principal_id, key, f"POST /v1/games/{game_id}/orders",
                   payload, match, game_id, apply)


@router.post("/{game_id}/orders/{order_id}/cancellations", response_model=GameView)
def cancel_order(
    game_id: str, order_id: str, payload: EmptyCommand, guest: GuestSession = Depends(require_session_write),
    key: Optional[str] = Header(None, alias="Idempotency-Key"),
    match: Optional[str] = Header(None, alias="If-Match"), session: Session = Depends(get_v1_session),
):
    return mutate_game(
        session, guest, game_id, key, match, f"POST /v1/games/{game_id}/orders/{order_id}/cancellations",
        payload, lambda game, state: load_market(session, game.release_id).cancel(state, order_id),
    )


@router.get("/{game_id}/orders", response_model=Page[OrderView])
def list_orders(
    game_id: str, status: Optional[Literal["pending", "filled", "rejected", "cancelled"]] = None,
    cursor: Optional[str] = Query(None, max_length=1024), limit: int = Query(50, ge=1, le=100),
    principal: Principal = Depends(get_current_principal), session: Session = Depends(get_v1_session),
):
    game = owned_game(session, principal.id, game_id)
    state = load_state(game)
    scope = cursor_scope("game-orders", game=game.id, revision=game.revision, status=status)
    items = [order_view(order, state) for order in state.orders if status is None or order.status == status]
    return response(sized_page(items, scope, sequence_after(cursor, scope), limit), game)


def order_view(order, state):
    result = wire(order)
    result["fill"] = wire(next((fill for fill in state.fills if fill.order_id == order.id), None))
    return result


@router.get("/{game_id}/orders/{order_id}", response_model=OrderView)
def get_order(game_id: str, order_id: str, principal: Principal = Depends(get_current_principal),
              session: Session = Depends(get_v1_session)):
    game = owned_game(session, principal.id, game_id)
    state = load_state(game)
    order = next((order for order in state.orders if order.id == order_id), None)
    if order is None:
        raise Problem(404, "ORDER_NOT_FOUND", "Order not found")
    return response(order_view(order, state), game)


@router.post("/{game_id}/order-previews", response_model=PreviewView)
@decimal_calculation
def preview_order(
    game_id: str, payload: OrderCreate, guest: GuestSession = Depends(require_session_write),
    session: Session = Depends(get_v1_session),
):
    game = owned_game(session, guest.principal_id, game_id)
    if game.archived or active_operation(session, game.id):
        raise Problem(409, "GAME_NOT_TRADABLE", "Game cannot accept orders")
    state, market = load_state(game), load_market(session, game.release_id)
    domain(queue_payload, market, state, payload)
    bar = market.bars.get((game.cursor, payload.instrument_id))
    if bar is None or bar.close is None or bar.close <= 0:
        raise Problem(409, "MARKET_DATA_UNAVAILABLE", "Visible closing quote is unavailable")
    quantity = Decimal(payload.quantity) if payload.quantity else None
    if payload.notional:
        quantity = domain(notional_quantity, Decimal(payload.notional), bar.close)
    if payload.close_all:
        side = "long" if payload.action == "sell" else "short"
        quantity = state.book.quantity(payload.instrument_id, side)
    return response(dict(
        non_binding=True, as_of=game.cursor, price=bar.close, quantity=quantity,
        estimated_notional=domain(money, quantity * bar.close),
        execution="next_session_open", revision=game.revision,
    ), game)


@router.get("/{game_id}/portfolio", response_model=PortfolioView)
def get_portfolio(game_id: str, principal: Principal = Depends(get_current_principal),
                  session: Session = Depends(get_v1_session)):
    game = owned_game(session, principal.id, game_id)
    state = load_state(game)
    return response(dict(as_of=state.cursor, revision=game.revision,
                         **wire(state.snapshots[-1].portfolio)), game)


@router.get("/{game_id}/positions", response_model=Page[PositionView])
def get_positions(
    game_id: str, cursor: Optional[str] = Query(None, max_length=1024), limit: int = Query(50, ge=1, le=100),
    principal: Principal = Depends(get_current_principal), session: Session = Depends(get_v1_session),
):
    game = owned_game(session, principal.id, game_id)
    scope = cursor_scope("game-positions", game=game.id, revision=game.revision)
    return response(sized_page(load_state(game).snapshots[-1].positions, scope,
                               sequence_after(cursor, scope), limit), game)


def read_audit(session, principal, game_id, model, name, cursor, limit):
    game = owned_game(session, principal.id, game_id)
    state = load_state(game)
    ceiling = {
        GameFill: len(state.fills), GameLedger: len(state.book.ledger), GameEvent: len(state.events),
    }[model]
    scope = cursor_scope(name, game=game.id)
    rows, more = audit_page(session, model, game.id, sequence_after(cursor, scope), limit, ceiling)
    return response(dict(
        items=[json.loads(row.body_json) for row in rows],
        next_cursor=encode_cursor(scope, str(rows[-1].sequence)) if more else None,
    ), game)


@router.get("/{game_id}/trades", response_model=Page[FillView])
def get_trades(
    game_id: str, cursor: Optional[str] = Query(None, max_length=1024), limit: int = Query(50, ge=1, le=100),
    principal: Principal = Depends(get_current_principal), session: Session = Depends(get_v1_session),
):
    return read_audit(session, principal, game_id, GameFill, "game-trades", cursor, limit)


@router.get("/{game_id}/ledger", response_model=Page[LedgerView])
def get_ledger(
    game_id: str, cursor: Optional[str] = Query(None, max_length=1024), limit: int = Query(50, ge=1, le=100),
    principal: Principal = Depends(get_current_principal), session: Session = Depends(get_v1_session),
):
    return read_audit(session, principal, game_id, GameLedger, "game-ledger", cursor, limit)


@router.get("/{game_id}/events", response_model=Page[EventView])
def get_events(
    game_id: str, cursor: Optional[str] = Query(None, max_length=1024), limit: int = Query(50, ge=1, le=100),
    principal: Principal = Depends(get_current_principal), session: Session = Depends(get_v1_session),
):
    return read_audit(session, principal, game_id, GameEvent, "game-events", cursor, limit)


def start_operation(session, game, payload, kind):
    state, market = load_state(game), load_market(session, game.release_id)
    if kind == "margin_resolution":
        if state.status != "margin_call":
            raise Problem(409, "NO_MARGIN_CALL", "Game has no margin call")
        target = market.sessions[bisect_right(market.sessions, state.cursor)]
        effective = target
    else:
        if state.status != "ready":
            raise Problem(409, "GAME_NOT_ADVANCEABLE", "Game cannot advance")
        target = payload.to_date or domain(shifted_date, state.cursor, payload.by.count, payload.by.unit)
        effective = domain(market._target, state, target)
    operation = AdvanceOperation(
        game_id=game.id, kind=kind, requested_target=target, effective_target=effective,
        actual_cursor=game.cursor, game_revision=game.revision + 1, game_status=game.status,
        liquidate_all=payload.liquidate_all if kind == "margin_resolution" else False,
    )
    game.revision += 1
    session.add(game)
    session.add(operation)
    session.flush()
    return game, operation_view(operation, game), 202, f"/v1/games/{game.id}/advances/{operation.id}"


@router.post("/{game_id}/advances", status_code=202, response_model=OperationView)
def post_advance(
    game_id: str, payload: AdvanceCreate, guest: GuestSession = Depends(require_session_write),
    key: Optional[str] = Header(None, alias="Idempotency-Key"),
    match: Optional[str] = Header(None, alias="If-Match"), session: Session = Depends(get_v1_session),
):
    return command(session, guest.principal_id, key, f"POST /v1/games/{game_id}/advances",
                   payload, match, game_id, lambda game: start_operation(session, game, payload, "advance"))


@router.post("/{game_id}/margin-resolutions", status_code=202, response_model=OperationView)
def post_resolution(
    game_id: str, payload: MarginResolution, guest: GuestSession = Depends(require_session_write),
    key: Optional[str] = Header(None, alias="Idempotency-Key"),
    match: Optional[str] = Header(None, alias="If-Match"), session: Session = Depends(get_v1_session),
):
    return command(session, guest.principal_id, key, f"POST /v1/games/{game_id}/margin-resolutions",
                   payload, match, game_id,
                   lambda game: start_operation(session, game, payload, "margin_resolution"))


@router.get("/{game_id}/advances/{operation_id}", response_model=OperationView)
def get_operation(game_id: str, operation_id: str, principal: Principal = Depends(get_current_principal),
                  session: Session = Depends(get_v1_session)):
    game = owned_game(session, principal.id, game_id)
    operation = session.get(AdvanceOperation, operation_id)
    if operation is None or operation.game_id != game.id:
        raise Problem(404, "ADVANCE_NOT_FOUND", "Advance not found")
    return response(operation_view(operation, game), game)


def visible_range(game, start, end):
    end = end or game.cursor
    start = start or game.start_date
    if end > game.cursor or start > game.cursor:
        raise Problem(422, "FUTURE_DATA_FORBIDDEN", "Prices after the game cursor are forbidden")
    if start > end:
        raise Problem(422, "INVALID_DATE_RANGE", "Date range is invalid")
    return start, end


@router.get("/{game_id}/instruments", response_model=Page[InstrumentView])
def get_instruments(
    game_id: str, query: str = Query("", max_length=80),
    principal: Principal = Depends(get_current_principal), session: Session = Depends(get_v1_session),
):
    game = owned_game(session, principal.id, game_id)
    market = load_market(session, game.release_id)
    eligible = [key for key, coverage in market.coverage.items()
                if coverage.from_date <= game.cursor <= coverage.to_date]
    rows = session.exec(select(Instrument).where(Instrument.id.in_(eligible)).order_by(
        Instrument.symbol, Instrument.id,
    )).all()
    query = query.strip().casefold()
    return response(dict(items=[
        InstrumentView(**row.dict(exclude={"created_at"})).dict() for row in rows
        if query in row.symbol.casefold() or query in row.name.casefold()
    ], next_cursor=None), game)


@router.get("/{game_id}/quotes", response_model=QuotesView)
def get_quotes(
    game_id: str, instrument_ids: str = Query(..., min_length=1, max_length=800),
    principal: Principal = Depends(get_current_principal), session: Session = Depends(get_v1_session),
):
    game = owned_game(session, principal.id, game_id)
    identifiers = instrument_ids.split(",")
    if len(identifiers) > 20 or len(set(identifiers)) != len(identifiers):
        raise Problem(422, "INVALID_INSTRUMENT_SET", "One to twenty distinct instruments are required")
    market = load_market(session, game.release_id)
    items = []
    for instrument_id in identifiers:
        coverage = market.coverage.get(instrument_id)
        if coverage is None or not coverage.from_date <= game.cursor <= coverage.to_date:
            raise Problem(404, "INSTRUMENT_NOT_ELIGIBLE", "Instrument is not currently eligible")
        bar = market.bars.get((game.cursor, instrument_id))
        price = bar.close if bar else None
        items.append(dict(instrument_id=instrument_id, price=price, price_date=game.cursor,
                          as_of=game.cursor, currency="USD", price_basis="as_traded",
                          tradable=price is not None and price > 0 and not bar.unverified_terminal_event))
    return response(dict(items=items), game)


@router.get("/{game_id}/history", response_model=Page[HistoryPoint])
def get_history(
    game_id: str, instrument_id: str = Query(..., max_length=40),
    from_date: Optional[date] = None, to_date: Optional[date] = None,
    cursor: Optional[str] = Query(None, max_length=1024), limit: int = Query(250, ge=1, le=1000),
    principal: Principal = Depends(get_current_principal), session: Session = Depends(get_v1_session),
):
    game = owned_game(session, principal.id, game_id)
    start, end = visible_range(game, from_date, to_date)
    market = load_market(session, game.release_id)
    if instrument_id not in market.coverage:
        raise Problem(404, "INSTRUMENT_NOT_ELIGIBLE", "Instrument is not in the game release")
    scope = cursor_scope("game-history", game=game.id, instrument=instrument_id, start=str(start), end=str(end))
    after = sequence_after(cursor, scope)
    coverage = market.coverage[instrument_id]
    items = [dict(date=day, open=bar.open if bar else None, close=bar.close if bar else None)
             for day in market.sessions if start <= day <= end and coverage.from_date <= day <= coverage.to_date
             for bar in (market.bars.get((day, instrument_id)),)]
    return response(sized_page(items, scope, after, limit), game)


@router.get("/{game_id}/calendar", response_model=Page[date])
def get_calendar(
    game_id: str, from_date: Optional[date] = None, to_date: Optional[date] = None,
    cursor: Optional[str] = Query(None, max_length=1024), limit: int = Query(250, ge=1, le=1000),
    principal: Principal = Depends(get_current_principal), session: Session = Depends(get_v1_session),
):
    game = owned_game(session, principal.id, game_id)
    start, end = from_date or game.start_date, to_date or game.end_date
    if start > end or start < game.start_date or end > game.end_date:
        raise Problem(422, "INVALID_DATE_RANGE", "Calendar range exceeds game dates")
    market = load_market(session, game.release_id)
    scope = cursor_scope("game-calendar", game=game.id, start=str(start), end=str(end))
    return response(sized_page([day for day in market.sessions if start <= day <= end],
                               scope, sequence_after(cursor, scope), limit), game)


@router.get("/{game_id}/performance", response_model=Page[PerformancePoint])
def get_performance(
    game_id: str, from_date: Optional[date] = None, to_date: Optional[date] = None,
    cursor: Optional[str] = Query(None, max_length=1024), limit: int = Query(250, ge=1, le=1000),
    principal: Principal = Depends(get_current_principal), session: Session = Depends(get_v1_session),
):
    game = owned_game(session, principal.id, game_id)
    start, end = visible_range(game, from_date, to_date)
    state = load_state(game)
    scope = cursor_scope("game-performance", game=game.id, start=str(start), end=str(end))
    items = [dict(date=item.date, **wire(item.portfolio)) for item in state.snapshots if start <= item.date <= end]
    return response(sized_page(items, scope, sequence_after(cursor, scope), limit), game)
