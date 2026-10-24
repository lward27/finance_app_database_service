import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import func
from sqlmodel import Session, select

from finance_app_database_service.advance_worker import (
    LEASE_SECONDS, LeaseLost, checkpoint, claim, main, release_claim,
)
from finance_app_database_service.foundation_models import Principal
from finance_app_database_service.game_contracts import AdvanceCreate, GameCreate, MarginResolution
from finance_app_database_service.game_api import start_operation
from finance_app_database_service.game_market import load_market
from finance_app_database_service.game_models import (
    AdvanceOperation, Game, GameEvent, GameFill, GameLedger, GamePosting, GameSnapshot,
)
from finance_app_database_service.game_service import create_game
from finance_app_database_service.game_store import load_state, save_state
from finance_app_database_service.problems import Problem
from foundation_support import migrate, sqlite_engine
from game_support import seed_release
from test_simulation import A, DAYS

D = Decimal


class TestAdvanceWorker(unittest.TestCase):
    def setUp(self):
        self.engine = sqlite_engine()
        self.addCleanup(self.engine.dispose)
        migrate(self.engine)
        self.release_id = seed_release(self.engine)
        with Session(self.engine) as session:
            principal = Principal()
            session.add(principal)
            session.flush()
            game = create_game(session, principal.id, GameCreate(
                name="Worker fixture", start_date=DAYS[0], initial_cash="1000", release_id=self.release_id,
            ))
            session.commit()
            self.game_id = game.id

    def queue(self, *, quantity="10", action="buy"):
        with Session(self.engine) as session:
            game = session.get(Game, self.game_id)
            state = load_state(game)
            updated = load_market(session, game.release_id).queue(state, A, action, quantity=D(quantity))
            save_state(session, game, updated, state)
            game.revision += 1
            session.add(game)
            session.commit()

    def start(self, target=DAYS[-1], *, resolution=False):
        with Session(self.engine) as session:
            game = session.get(Game, self.game_id)
            payload = MarginResolution(liquidate_all=True) if resolution else AdvanceCreate(to_date=target)
            _, body, _, _ = start_operation(
                session, game, payload, "margin_resolution" if resolution else "advance",
            )
            session.commit()
            return body["id"]

    def state(self):
        with Session(self.engine) as session:
            return load_state(session.get(Game, self.game_id))

    def counts(self):
        with Session(self.engine) as session:
            return tuple(session.exec(select(func.count()).select_from(model)).one() for model in (
                GameFill, GameLedger, GamePosting, GameSnapshot, GameEvent,
            ))

    def test_each_checkpoint_is_one_session_and_new_worker_can_resume_after_release(self):
        self.queue()
        operation_id = self.start()
        leased_id, token = claim(self.engine)
        self.assertEqual(leased_id, operation_id)
        self.assertFalse(checkpoint(self.engine, leased_id, token))
        self.assertEqual(self.state().cursor, DAYS[1])
        self.assertEqual(len(self.state().fills), 1)
        release_claim(self.engine, leased_id, token)
        with self.assertRaises(LeaseLost):
            checkpoint(self.engine, leased_id, token)
        _, new_token = claim(self.engine)
        self.assertNotEqual(new_token, token)
        while not checkpoint(self.engine, leased_id, new_token):
            pass
        state = self.state()
        self.assertEqual(state.cursor, DAYS[-1])
        self.assertEqual(len(state.fills), 1)
        with Session(self.engine) as session:
            operation = session.get(AdvanceOperation, leased_id)
            self.assertEqual(operation.status, "completed")
            self.assertIsNone(operation.lease_token)

    def test_expired_claim_is_fenced_and_reclaimed_without_duplicate_fills_or_costs(self):
        self.queue(quantity="20")
        self.start()
        now = datetime(2030, 1, 1)
        operation_id, old_token = claim(self.engine, now=now)
        checkpoint(self.engine, operation_id, old_token, now=now)
        saved = self.state()
        _, new_token = claim(self.engine, now=now + timedelta(seconds=LEASE_SECONDS + 1))
        with self.assertRaises(LeaseLost):
            checkpoint(self.engine, operation_id, old_token, now=now + timedelta(seconds=LEASE_SECONDS + 2))
        self.assertEqual(self.state(), saved)
        checkpoint(self.engine, operation_id, new_token, now=now + timedelta(seconds=LEASE_SECONDS + 2))
        state = self.state()
        self.assertEqual(len(state.fills), 1)
        self.assertEqual(state.snapshots[-1].portfolio.margin_interest, D("0.65753425"))

    def test_unexpired_claim_cannot_be_stolen(self):
        self.start()
        now = datetime(2030, 1, 1)
        operation_id, token = claim(self.engine, now=now)
        self.assertIsNone(claim(self.engine, now=now + timedelta(seconds=1)))
        with self.assertRaises(LeaseLost):
            checkpoint(self.engine, operation_id, "not-the-token", now=now)

    def test_lease_expiring_during_computation_rolls_back_before_commit(self):
        self.queue()
        self.start()
        now = datetime(2030, 1, 1)
        operation_id, token = claim(self.engine, now=now)
        original, counts = self.state(), self.counts()
        with patch("finance_app_database_service.advance_worker.datetime") as clock:
            clock.utcnow.side_effect = [now, now + timedelta(seconds=LEASE_SECONDS + 1)]
            with self.assertRaises(LeaseLost):
                checkpoint(self.engine, operation_id, token)
        self.assertEqual(self.state(), original)
        self.assertEqual(self.counts(), counts)

    def test_crash_after_projection_rolls_back_entire_session_for_safe_retry(self):
        self.queue()
        self.start()
        operation_id, token = claim(self.engine)
        original, counts = self.state(), self.counts()

        def crash(session, game, updated, previous):
            save_state(session, game, updated, previous)
            session.flush()
            raise RuntimeError("simulated process failure before commit")

        with patch("finance_app_database_service.advance_worker.save_state", side_effect=crash):
            with self.assertRaises(RuntimeError):
                checkpoint(self.engine, operation_id, token)
        self.assertEqual(self.state(), original)
        self.assertEqual(self.counts(), counts)
        self.assertFalse(checkpoint(self.engine, operation_id, token))
        self.assertEqual(len(self.state().fills), 1)

    def test_expected_projection_error_rolls_back_savepoint_before_block_result(self):
        self.queue()
        self.start()
        operation_id, token = claim(self.engine)
        original, counts = self.state(), self.counts()

        def reject(session, game, updated, previous):
            save_state(session, game, updated, previous)
            session.flush()
            raise ValueError("simulated invalid projection")

        with patch("finance_app_database_service.advance_worker.save_state", side_effect=reject):
            self.assertTrue(checkpoint(self.engine, operation_id, token))
        self.assertEqual(self.state(), original)
        self.assertEqual(self.counts(), counts)
        with Session(self.engine) as session:
            operation = session.get(AdvanceOperation, operation_id)
            self.assertEqual((operation.status, operation.code), ("blocked_data", "INVALID_MARKET_DATA"))

    def test_data_failure_keeps_prior_checkpoint_and_clears_active_operation(self):
        self.queue(quantity="20")
        self.start()
        operation_id, token = claim(self.engine)
        checkpoint(self.engine, operation_id, token)
        original, counts = self.state(), self.counts()
        with patch("finance_app_database_service.advance_worker.load_market", side_effect=Problem(
            503, "QUALIFIED_INPUTS_UNAVAILABLE", "Missing data",
        )):
            self.assertTrue(checkpoint(self.engine, operation_id, token))
        self.assertEqual(self.state(), original)
        self.assertEqual(self.counts(), counts)
        with Session(self.engine) as session:
            operation = session.get(AdvanceOperation, operation_id)
            self.assertEqual(operation.actual_cursor, DAYS[1])
            self.assertEqual(operation.status, "blocked_data")
        # A new command can retry from the last committed cursor.
        self.start()
        leased_id, new_token = claim(self.engine)
        checkpoint(self.engine, leased_id, new_token)
        self.assertEqual(self.state().snapshots[-1].portfolio.margin_interest, D("0.65753425"))

    def test_margin_interruption_and_player_authorized_resolution_are_durable(self):
        self.queue(quantity="20")
        self.start()
        original_loader = load_market

        def gap(session, release_id):
            from finance_app_database_service.simulation import Bar, Simulator
            from finance_app_database_service.foundation_models import DatasetRelease
            from finance_app_database_service.foundation_api import release_manifest
            market = original_loader(session, release_id)
            bars = list(market.bars.values())
            bars = [Bar(A, DAYS[1], D("100"), D("60")) if bar.date == DAYS[1] else bar for bar in bars]
            return Simulator(release_manifest(session.get(DatasetRelease, release_id)), market.sessions, tuple(bars))

        operation_id, token = claim(self.engine)
        with patch("finance_app_database_service.advance_worker.load_market", side_effect=gap):
            self.assertTrue(checkpoint(self.engine, operation_id, token))
        self.assertEqual(self.state().status, "margin_call")
        self.assertEqual(self.state().cursor, DAYS[1])
        self.start(resolution=True)
        resolution_id, token = claim(self.engine)
        self.assertTrue(checkpoint(self.engine, resolution_id, token))
        self.assertEqual(self.state().cursor, DAYS[2])
        self.assertEqual(self.state().book.lots, ())
        self.assertEqual(self.state().fills[-1].action, "sell")
        self.assertEqual(len(self.state().fills), 2)

    def test_cli_preview_never_claims_or_mutates_and_execute_once_is_bounded(self):
        self.queue()
        operation_id = self.start()
        with patch("finance_app_database_service.advance_worker.default_engine", self.engine):
            with patch("builtins.print"):
                main([])
            with Session(self.engine) as session:
                self.assertEqual(session.get(AdvanceOperation, operation_id).attempts, 0)
            main(["--execute", "--once", "--max-checkpoints", "1"])
        self.assertEqual(self.state().cursor, DAYS[1])
        with Session(self.engine) as session:
            operation = session.get(AdvanceOperation, operation_id)
            self.assertEqual(operation.status, "queued")
            self.assertIsNone(operation.lease_token)

    def test_operation_progress_remains_pinned_after_later_advances(self):
        first = self.start(DAYS[1])
        operation_id, token = claim(self.engine)
        checkpoint(self.engine, operation_id, token)
        second = self.start(DAYS[2])
        operation_id, token = claim(self.engine)
        checkpoint(self.engine, operation_id, token)
        with Session(self.engine) as session:
            self.assertEqual(session.get(AdvanceOperation, first).actual_cursor, DAYS[1])
            self.assertEqual(session.get(AdvanceOperation, second).actual_cursor, DAYS[2])
