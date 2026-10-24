"""Durable, fenced advance worker. Preview by default; --execute authorizes work."""

import argparse
import logging
import time
from datetime import datetime, timedelta
from decimal import DecimalException
from uuid import uuid4

from sqlalchemy import and_, func, or_
from sqlmodel import Session, select

from finance_app_database_service.database import engine as default_engine
from finance_app_database_service.game_market import load_market
from finance_app_database_service.game_models import AdvanceOperation, Game
from finance_app_database_service.game_service import ACTIVE, lock_game
from finance_app_database_service.game_store import load_state, save_state
from finance_app_database_service.problems import Problem
from finance_app_database_service.simulation import SimulationError

log = logging.getLogger(__name__)
LEASE_SECONDS = 120


class LeaseLost(Exception):
    pass


def due(now):
    return or_(
        AdvanceOperation.status == "queued",
        and_(AdvanceOperation.status == "running", AdvanceOperation.lease_until <= now),
    )


def claim(engine, *, now=None):
    clock = now or datetime.utcnow()
    with Session(engine) as session:
        # Lock game before operation everywhere, including API commands.
        candidates = session.exec(select(AdvanceOperation.id, AdvanceOperation.game_id).where(
            due(clock),
        ).order_by(AdvanceOperation.created_at, AdvanceOperation.id).limit(20)).all()
        for operation_id, game_id in candidates:
            lock_game(session, game_id)
            operation = session.exec(select(AdvanceOperation).where(
                AdvanceOperation.id == operation_id, due(clock),
            ).execution_options(populate_existing=True)).first()
            if operation is None:
                session.rollback()
                continue
            operation.status, operation.lease_token = "running", uuid4().hex
            operation.lease_until = clock + timedelta(seconds=LEASE_SECONDS)
            operation.attempts += 1
            operation.updated_at = clock
            session.add(operation)
            token = operation.lease_token
            session.commit()
            return operation_id, token
    return None


def checkpoint(engine, operation_id, token, *, now=None):
    """Commit exactly one session, or only the terminal operation result.

    DB failures escape so the transaction rolls back and lease recovery can retry.
    A stale worker cannot commit after another worker claims an expired lease.
    """
    with Session(engine) as session:
        operation = session.get(AdvanceOperation, operation_id)
        if operation is None:
            raise LeaseLost()
        game_id = operation.game_id
        game = lock_game(session, game_id)
        session.expire(operation)
        clock = now or datetime.utcnow()
        if (
            operation.status != "running" or operation.lease_token != token
            or operation.lease_until is None or operation.lease_until <= clock
        ):
            raise LeaseLost()
        terminal = None
        try:
            # A failed projection must not leave a partial checkpoint behind even
            # when the operation's blocked-data result is committed separately.
            with session.begin_nested():
                state = load_state(game)
                if game.archived:
                    raise Problem(409, "GAME_ARCHIVED", "Archived games are frozen")
                market = load_market(session, game.release_id)
                if operation.kind == "margin_resolution":
                    result = market.resolve_margin(state, liquidate_all=operation.liquidate_all)
                    if result.outcome == "blocked_data":
                        raise SimulationError(result.code)
                    updated = result.state
                    terminal = "interrupted_margin" if updated.status == "margin_call" else "completed"
                else:
                    updated = market.step(state)
                    if updated.status == "margin_call":
                        terminal = "interrupted_margin"
                    elif updated.status in ("bankrupt", "completed") or updated.cursor >= operation.effective_target:
                        terminal = "completed"
                save_state(session, game, updated, state)
                session.flush()
        except (SimulationError, Problem) as exc:
            terminal = "blocked_data"
            operation.code = exc.code
        except (ValueError, DecimalException):
            terminal = "blocked_data"
            operation.code = "INVALID_MARKET_DATA"
        # Check again after input verification/computation, before any commit.
        clock = now or datetime.utcnow()
        if operation.lease_until <= clock:
            raise LeaseLost()
        game.revision += 1
        operation.actual_cursor, operation.game_revision = game.cursor, game.revision
        operation.game_status = game.status
        session.add(game)
        operation.updated_at = clock
        if terminal:
            operation.status = terminal
            operation.lease_token = operation.lease_until = None
        else:
            operation.lease_until = clock + timedelta(seconds=LEASE_SECONDS)
        session.add(operation)
        session.commit()
        return terminal is not None


def release_claim(engine, operation_id, token):
    with Session(engine) as session:
        operation = session.get(AdvanceOperation, operation_id)
        if operation is None:
            return
        lock_game(session, operation.game_id)
        session.expire(operation)
        if operation.status == "running" and operation.lease_token == token:
            operation.status, operation.lease_token, operation.lease_until = "queued", None, None
            session.add(operation)
            session.commit()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--once", action="store_true", help="Process at most one bounded claim")
    parser.add_argument("--max-checkpoints", type=int, default=25)
    args = parser.parse_args(argv)
    if not 1 <= args.max_checkpoints <= 1000:
        parser.error("--max-checkpoints must be between 1 and 1000")
    if not args.execute:
        with Session(default_engine) as session:
            count = session.exec(select(func.count()).select_from(AdvanceOperation).where(
                AdvanceOperation.status.in_(ACTIVE),
            )).one()
        print(f"Preview: {count} active advances. No claims or checkpoints written.")
        return
    while True:
        leased = claim(default_engine)
        if leased is None:
            if args.once:
                return
            time.sleep(2)
            continue
        operation_id, token = leased
        try:
            for _ in range(args.max_checkpoints):
                if checkpoint(default_engine, operation_id, token):
                    break
            else:
                release_claim(default_engine, operation_id, token)
        except LeaseLost:
            log.info("Advance lease changed; stale worker stopped")
        except Exception:
            # Do not log SQL parameters, payloads, credentials, or exception bodies.
            log.error("Advance checkpoint failed; transaction rolled back for lease recovery")
            if args.once:
                raise SystemExit(1) from None
            time.sleep(2)
        if args.once:
            return


if __name__ == "__main__":
    main()
