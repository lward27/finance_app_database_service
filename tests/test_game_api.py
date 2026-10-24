import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import func, text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select

from finance_app_database_service.advance_worker import claim, checkpoint
from finance_app_database_service.game_models import (
    AdvanceOperation, Game, GameCommand, GameEvent, GameFill, GameLedger, GameOrder, GamePosting,
)
from finance_app_database_service.game_store import load_state
from finance_app_database_service.game_contracts import GameView, OrderView, PortfolioView
from finance_app_database_service.problems import Problem
from foundation_support import ORIGIN, client, migrate, settings, sqlite_engine, test_app
from game_support import seed_release
from test_simulation import A, DAYS


class TestGameAPI(unittest.TestCase):
    def setUp(self):
        self.engine = sqlite_engine()
        migrate(self.engine)
        self.release_id = seed_release(self.engine)
        self.app = test_app(self.engine, settings(games_enabled=True))
        self.browser = client(self.app)
        self.addCleanup(self.browser.close)
        self.addCleanup(self.engine.dispose)
        self.guest = self.browser.post("/v1/guest-sessions", json={}, headers={"Origin": ORIGIN}).json()
        self.headers = {"Origin": ORIGIN, "X-CSRF-Token": self.guest["csrf_token"]}
        self.serial = 0

    def post(self, path, payload, *, match=None, key=None, browser=None):
        self.serial += 1
        headers = {**self.headers, "Idempotency-Key": key or f"command-{self.serial}"}
        if match is not None:
            headers["If-Match"] = match
        return (browser or self.browser).post(path, json=payload, headers=headers)

    def create(self, **overrides):
        payload = dict(name="Test game", start_date=str(DAYS[0]), initial_cash="1000",
                       release_id=self.release_id)
        payload.update(overrides)
        result = self.post("/v1/games", payload)
        self.assertEqual(result.status_code, 201, result.text)
        return result

    def advance(self, game_id, target):
        game = self.browser.get(f"/v1/games/{game_id}")
        result = self.post(f"/v1/games/{game_id}/advances", {"to_date": str(target)},
                           match=game.headers["etag"])
        self.assertEqual(result.status_code, 202, result.text)
        return result

    def work(self):
        operation_id, token = claim(self.engine)
        for _ in range(20):
            if checkpoint(self.engine, operation_id, token):
                return
        self.fail("Worker did not finish its bounded synthetic operation")

    def test_create_pins_inputs_normalizes_dates_and_funds_balanced_ledger(self):
        created = self.create(start_date="2020-01-04", end_date="2020-01-09")
        value = created.json()
        self.assertEqual(value["start_date"], "2020-01-06")
        self.assertEqual(value["requested_start"], "2020-01-04")
        self.assertEqual(value["initial_cash"], "1000.00000000")
        self.assertEqual(value["release_id"], self.release_id)
        self.assertEqual(created.headers["cache-control"], "no-store")
        game_id = value["id"]
        portfolio = self.browser.get(f"/v1/games/{game_id}/portfolio").json()
        self.assertEqual(portfolio["cash"], "1000.00000000")
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(GameLedger)).all()), 1)
            amounts = session.exec(select(GamePosting.amount)).all()
            self.assertEqual(sum(amounts), 0)
            load_state(session.get(Game, game_id))

    def test_duplicate_creation_returns_original_game_without_a_second_contribution(self):
        body = dict(name="Same", start_date=str(DAYS[0]), initial_cash="1000", release_id=self.release_id)
        first = self.post("/v1/games", body, key="create-once")
        second = self.post("/v1/games", body, key="create-once")
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(second.headers["etag"], first.headers["etag"])
        conflict = self.post("/v1/games", {**body, "initial_cash": "2000"}, key="create-once")
        self.assertEqual(conflict.status_code, 409)
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(func.count()).select_from(Game)).one(), 1)

    def test_public_response_contracts_and_openapi_use_decimal_strings(self):
        created = self.create()
        GameView.parse_obj(created.json())
        game_id = created.json()["id"]
        portfolio = self.browser.get(f"/v1/games/{game_id}/portfolio")
        PortfolioView.parse_obj(portfolio.json())
        order = self.post(f"/v1/games/{game_id}/orders", {
            "instrument_id": A, "action": "buy", "quantity": "1",
        }, match=created.headers["etag"])
        OrderView.parse_obj(order.json())
        schemas = self.browser.get("/openapi.json").json()["components"]["schemas"]
        self.assertEqual(schemas["PortfolioView"]["properties"]["equity"]["type"], "string")
        self.assertFalse(schemas["GameCreate"]["additionalProperties"])

    def test_advancing_filter_matches_visible_game_status(self):
        created = self.create()
        self.advance(created.json()["id"], DAYS[-1])
        advancing = self.browser.get("/v1/games", params={"status": "advancing"}).json()["items"]
        self.assertEqual(len(advancing), 1)
        self.assertEqual(advancing[0]["status"], "advancing")
        self.assertEqual(self.browser.get("/v1/games", params={"status": "ready"}).json()["items"], [])

    def test_audit_reads_are_capped_at_the_loaded_checkpoint(self):
        created = self.create()
        game_id = created.json()["id"]
        with Session(self.engine) as session:
            session.add(GameFill(game_id=game_id, sequence=1, body_json='{"date":"2099-01-01"}'))
            session.commit()
        self.assertEqual(self.browser.get(f"/v1/games/{game_id}/trades").json()["items"], [])

    def test_corrupt_checkpoints_fail_closed_without_reflecting_storage(self):
        created = self.create()
        game_id = created.json()["id"]
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE game SET state_json = :body WHERE id = :id"),
                               {"body": '{"private":"test-only-sensitive-value"}', "id": game_id})
        result = self.browser.get(f"/v1/games/{game_id}/portfolio")
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()["code"], "GAME_INTEGRITY_ERROR")
        self.assertNotIn("test-only-sensitive-value", result.text)
        self.assertEqual(result.headers["cache-control"], "no-store")

    def test_ownership_applies_to_every_game_subresource_and_operations(self):
        created = self.create()
        game_id = created.json()["id"]
        operation = self.advance(game_id, DAYS[1]).json()["id"]
        other = client(self.app)
        self.addCleanup(other.close)
        guest = other.post("/v1/guest-sessions", json={}, headers={"Origin": ORIGIN}).json()
        for suffix in ("", "/portfolio", "/positions", "/orders", "/orders/order_1",
                       "/trades", "/ledger", "/events", "/performance", "/instruments",
                       f"/quotes?instrument_ids={A}", f"/history?instrument_id={A}", "/calendar",
                       f"/advances/{operation}"):
            result = other.get(f"/v1/games/{game_id}{suffix}")
            self.assertEqual(result.status_code, 404, (suffix, result.text))
        headers = {"Origin": ORIGIN, "X-CSRF-Token": guest["csrf_token"],
                   "Idempotency-Key": "not-owned", "If-Match": created.headers["etag"]}
        result = other.post(f"/v1/games/{game_id}/orders", json={
            "instrument_id": A, "action": "buy", "quantity": "1",
        }, headers=headers)
        self.assertEqual(result.status_code, 404)
        self.assertEqual(other.get("/v1/games").json()["items"], [])

    def test_commands_require_origin_csrf_idempotency_and_revision(self):
        created = self.create()
        path = f"/v1/games/{created.json()['id']}/orders"
        payload = {"instrument_id": A, "action": "buy", "quantity": "1"}
        result = self.browser.post(path, json=payload)
        self.assertEqual(result.status_code, 403)
        result = self.browser.post(path, json=payload, headers=self.headers)
        self.assertEqual(result.status_code, 422)
        result = self.post(path, payload)
        self.assertEqual(result.status_code, 428)
        result = self.post(path, payload, match='"stale:0"')
        self.assertEqual(result.status_code, 412)
        self.assertEqual(result.headers["etag"], created.headers["etag"])
        self.assertEqual(result.headers["cache-control"], "no-store")

    def test_order_retries_resolve_before_revision_and_advance_conflicts(self):
        created = self.create()
        game_id, match = created.json()["id"], created.headers["etag"]
        path = f"/v1/games/{game_id}/orders"
        body = {"instrument_id": A, "action": "buy", "notional": "100"}
        first = self.post(path, body, key="one-order", match=match)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["status"], "pending")
        self.assertIsNone(first.json()["fill"])
        self.advance(game_id, DAYS[-1])
        repeated = self.post(path, body, key="one-order", match=match)
        self.assertEqual(repeated.json(), first.json())
        self.assertEqual(repeated.headers["etag"], first.headers["etag"])
        current = self.browser.get(f"/v1/games/{game_id}")
        blocked = self.post(path, body, match=current.headers["etag"])
        self.assertEqual(blocked.json()["code"], "ADVANCE_IN_PROGRESS")
        self.work()
        order = self.browser.get(f"{path}/{first.json()['id']}").json()
        self.assertEqual(order["status"], "filled")
        self.assertEqual(order["fill"]["date"], str(DAYS[1]))
        self.assertEqual(order["fill"]["price"], "100.00000000")

    def test_no_future_prices_history_performance_or_preview_and_calendar_is_price_free(self):
        created = self.create()
        game_id = created.json()["id"]
        quotes = self.browser.get(f"/v1/games/{game_id}/quotes", params={"instrument_ids": A}).json()
        self.assertEqual(quotes["items"][0]["price_date"], str(DAYS[0]))
        for resource in ("history", "performance"):
            result = self.browser.get(f"/v1/games/{game_id}/{resource}", params={
                "instrument_id": A, "to_date": str(DAYS[-1]),
            })
            self.assertEqual(result.json()["code"], "FUTURE_DATA_FORBIDDEN")
        preview = self.browser.post(f"/v1/games/{game_id}/order-previews", headers=self.headers,
                                    json={"instrument_id": A, "action": "buy", "quantity": "1"})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["as_of"], str(DAYS[0]))
        self.assertTrue(preview.json()["non_binding"])
        self.assertEqual(self.browser.get(f"/v1/games/{game_id}/orders").json()["items"], [])
        calendar = self.browser.get(f"/v1/games/{game_id}/calendar").json()
        self.assertEqual(calendar["items"][-1], str(DAYS[-1]))
        self.assertNotIn("100", json.dumps(calendar))

    def test_strict_contracts_reject_floats_owner_clock_and_balance_injection(self):
        created = self.create()
        game_id = created.json()["id"]
        for body in (
            {"instrument_id": A, "action": "buy", "quantity": 1.5},
            {"instrument_id": A, "action": "buy", "quantity": "1", "owner_id": "other"},
            {"instrument_id": A, "action": "buy", "quantity": "1", "price": "0.01"},
        ):
            result = self.post(f"/v1/games/{game_id}/orders", body, match=created.headers["etag"])
            self.assertEqual(result.status_code, 422)
        for extra in ({"owner_id": "other"}, {"cash": "9999"}, {"cursor": str(DAYS[-1])}):
            result = self.post("/v1/games", dict(
                name="Bad", start_date=str(DAYS[0]), initial_cash="1000", **extra,
            ))
            self.assertEqual(result.status_code, 422)
        self.assertEqual(self.post("/v1/games", {
            "name": "Bad", "start_date": str(DAYS[0]), "initial_cash": 1000,
        }).status_code, 422)

    def test_cancellation_archive_rename_and_replay_preserve_audit(self):
        created = self.create()
        game_id = created.json()["id"]
        order = self.post(f"/v1/games/{game_id}/orders", {
            "instrument_id": A, "action": "buy", "quantity": "1",
        }, match=created.headers["etag"])
        cancelled = self.post(f"/v1/games/{game_id}/orders/order_1/cancellations", {},
                              match=order.headers["etag"])
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        replay = self.post(f"/v1/games/{game_id}/replays", {}, match=cancelled.headers["etag"])
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertNotEqual(replay.json()["id"], game_id)
        archived = self.post(f"/v1/games/{game_id}/archives", {}, match=cancelled.headers["etag"])
        self.assertEqual(archived.status_code, 200, archived.text)
        blocked = self.post(f"/v1/games/{game_id}/orders", {
            "instrument_id": A, "action": "buy", "quantity": "1",
        }, match=archived.headers["etag"])
        self.assertEqual(blocked.json()["code"], "GAME_ARCHIVED")
        self.assertEqual(self.browser.get(f"/v1/games/{game_id}/orders").json()["items"][0]["status"], "cancelled")

    def test_cursors_are_scoped_bounded_and_consistent_for_append_only_events(self):
        created = self.create()
        game_id = created.json()["id"]
        self.advance(game_id, DAYS[-1])
        self.work()
        page = self.browser.get(f"/v1/games/{game_id}/events", params={"limit": 2}).json()
        self.assertEqual(len(page["items"]), 2)
        self.assertIsNotNone(page["next_cursor"])
        next_page = self.browser.get(f"/v1/games/{game_id}/events", params={
            "limit": 2, "cursor": page["next_cursor"],
        }).json()
        self.assertEqual(next_page["items"][0]["sequence"], 3)
        bad = self.browser.get(f"/v1/games/{game_id}/ledger", params={"cursor": page["next_cursor"]})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(self.browser.get(f"/v1/games/{game_id}/events", params={"limit": 10000}).status_code, 422)

    def test_feature_flag_and_missing_verified_revisions_fail_closed(self):
        disabled = client(test_app(self.engine))
        self.addCleanup(disabled.close)
        self.assertEqual(disabled.get("/v1/games").json()["code"], "GAMES_DISABLED")
        with patch("finance_app_database_service.game_service.load_market", side_effect=Problem(
            503, "QUALIFIED_INPUTS_UNAVAILABLE", "Unavailable",
        )):
            result = self.post("/v1/games", dict(name="No data", start_date=str(DAYS[0]), initial_cash="1000"))
        self.assertEqual(result.status_code, 503)
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(func.count()).select_from(Game)).one(), 0)

    def test_audit_rows_are_database_immutable(self):
        created = self.create()
        game_id = created.json()["id"]
        self.advance(game_id, DAYS[-1])
        self.work()
        for table in ("game_ledger", "game_posting", "game_snapshot", "game_event", "game_command",
                      "qualified_market_revision", "qualified_calendar"):
            for operation in (f"DELETE FROM {table}", f"UPDATE {table} SET " + (
                "amount = amount" if table == "game_posting" else
                "response_json = response_json" if table == "game_command" else
                "revision_id = revision_id" if table == "qualified_calendar" else
                "body_json = body_json"
            )):
                with self.subTest(operation=operation), self.assertRaises(DBAPIError):
                    with self.engine.begin() as connection:
                        connection.execute(text(operation))
