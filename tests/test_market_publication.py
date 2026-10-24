import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import func, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select

from finance_app_database_service import database, market_publication as publication, publication_store as store
from finance_app_database_service.advance_worker import checkpoint, claim
from finance_app_database_service.contracts import ReleaseManifest, canonical_json, content_digest
from finance_app_database_service.foundation_models import DatasetRelease, Instrument
from finance_app_database_service.game_market import load_market
from finance_app_database_service.game_models import CalendarBinding, Game, MarketPublication, MarketRevision
from finance_app_database_service.game_store import load_state
from finance_app_database_service.market_documents import ValidationDocument
from finance_app_database_service.problems import Problem
from foundation_support import ORIGIN, ROOT, client, migrate, settings, sqlite_engine, test_app
from publication_support import KEY, NOW, artifacts, seal, seed_instrument
from test_market_qualification import A, DAYS, bundle


class TestPublicationPlan(unittest.TestCase):
    def setUp(self):
        self.input, self.candidate, self.approval = artifacts()

    def plan(self, key=KEY):
        return publication.build_plan(self.input, self.candidate, self.approval, key, now=NOW)

    def blocked(self, code, key=KEY):
        with self.assertRaises(publication.PublicationError) as caught:
            self.plan(key)
        self.assertEqual(caught.exception.code, code)

    def test_plan_binds_manifest_reports_original_inputs_and_signed_review(self):
        before = copy.deepcopy((self.input, self.candidate, self.approval))
        plan = self.plan()
        self.assertEqual((self.input, self.candidate, self.approval), before)
        self.assertEqual(plan.release_id, "rel_" + content_digest(plan.manifest_json))
        self.assertEqual(plan.approval_sha256, content_digest(canonical_json(self.approval)))
        manifest = ReleaseManifest.parse_raw(plan.manifest_json)
        coverage = manifest.instruments[0]
        report = ValidationDocument.parse_raw(next(body for digest, kind, body in plan.revisions
                                                   if digest == coverage.validation_report_sha256))
        self.assertEqual(report.calendar_revision_id, plan.calendar_revision_id)
        self.assertEqual(report.source_snapshot_ids, [self.input["instruments"][0]["source_snapshot_id"]])
        self.assertEqual(report.bar_revision_id, coverage.bar_revision_id)
        self.assertEqual(len(plan.revisions), 4)

    def test_signature_and_independent_key_are_required(self):
        for key in (None, b"short", bytes.fromhex("cd" * 32)):
            self.blocked("REVIEW_SIGNATURE_INVALID" if key and len(key) == 32 else "REVIEW_KEY_UNAVAILABLE", key)
        self.approval["signature"] = "0" * 64
        self.blocked("REVIEW_SIGNATURE_INVALID")

    def test_every_review_check_is_an_explicit_true_boolean(self):
        original = copy.deepcopy(self.approval)
        for name in publication.ReviewChecks.__fields__:
            for value in (False, 1, "true"):
                self.approval = copy.deepcopy(original)
                self.approval["review"]["checks"][name] = value
                self.approval = seal(self.approval["review"])
                with self.subTest(name=name, value=value):
                    self.blocked("INVALID_REVIEW")
        self.approval = copy.deepcopy(original)
        del self.approval["review"]["checks"]["licensing_reviewed"]
        self.approval = seal(self.approval["review"])
        self.blocked("INVALID_REVIEW")

    def test_approval_cannot_be_reused_for_changed_candidates_or_inputs(self):
        self.candidate["coverage"][0]["shortable"] = True
        self.blocked("CANDIDATE_APPROVAL_MISMATCH")
        self.approval["review"]["candidate_sha256"] = content_digest(canonical_json(self.candidate))
        self.approval = seal(self.approval["review"])
        self.blocked("CANDIDATE_RECOMPUTATION_MISMATCH")
        self.input, self.candidate, self.approval = artifacts()
        self.approval["review"]["input_sha256"] = "0" * 64
        self.approval = seal(self.approval["review"])
        self.blocked("INPUT_APPROVAL_MISMATCH")

    def test_original_archive_is_rechecked_instead_of_trusting_a_saved_candidate(self):
        self.input["instruments"][0]["source_export"]["rows"][0]["close"] = "101"
        with self.assertRaises(ValueError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, "SOURCE_SNAPSHOT_MISMATCH")

    def test_review_time_is_aware_utc_current_and_bounded_to_24_hours(self):
        original = copy.deepcopy(self.approval)
        for field, value, code in (
            ("reviewed_at", NOW.replace(tzinfo=None).isoformat(), "INVALID_REVIEW"),
            ("expires_at", (NOW - timedelta(seconds=1)).isoformat(), "INVALID_REVIEW_WINDOW"),
            ("expires_at", (NOW + timedelta(hours=25)).isoformat(), "INVALID_REVIEW_WINDOW"),
            ("reviewed_at", (NOW + timedelta(minutes=1)).isoformat(), "REVIEW_NOT_CURRENT"),
        ):
            self.approval = copy.deepcopy(original)
            self.approval["review"][field] = value
            self.approval = seal(self.approval["review"])
            with self.subTest(field=field, value=value):
                self.blocked(code)
        self.approval = original
        with self.assertRaises(publication.PublicationError) as caught:
            publication.build_plan(self.input, self.candidate, self.approval, KEY, now=NOW + timedelta(hours=1))
        self.assertEqual(caught.exception.code, "REVIEW_NOT_CURRENT")

    def test_unknown_review_policy_and_unsigned_extra_fields_are_not_accepted(self):
        self.approval["review"]["policy_id"] = "skip-evidence"
        self.approval = seal(self.approval["review"])
        self.blocked("INVALID_REVIEW")
        self.input, self.candidate, self.approval = artifacts()
        self.approval["auto_publish"] = True
        self.blocked("INVALID_REVIEW")

    def test_review_key_configuration_and_revision_limits_fail_closed(self):
        self.assertEqual(publication.parse_review_key(KEY.hex().upper()), KEY)
        for value in (None, "", "not-a-key", "0" * 63):
            with self.assertRaises(publication.PublicationError):
                publication.parse_review_key(value)
        with patch.object(publication, "MAX_REVISION_BYTES", 1):
            self.blocked("REVISION_CAPACITY_EXCEEDED")


class TestPublicationStore(unittest.TestCase):
    def setUp(self):
        self.engine = sqlite_engine()
        self.addCleanup(self.engine.dispose)
        migrate(self.engine)
        seed_instrument(self.engine)
        self.input, self.candidate, self.approval = artifacts()

    def publish(self, **kwargs):
        return store.publish(self.engine, self.input, self.candidate, self.approval, KEY,
                             store.target_sha256(self.engine), clock=kwargs.pop("clock", lambda: NOW), **kwargs)

    def counts(self):
        with Session(self.engine) as session:
            return tuple(session.exec(select(func.count()).select_from(model)).one()
                         for model in (DatasetRelease, MarketPublication, MarketRevision, CalendarBinding))

    def assert_unpublished(self, expected=(0, 0, 0, 0)):
        self.assertEqual(self.counts(), expected)
        with Session(self.engine) as session:
            instrument = session.get(Instrument, A)
            self.assertEqual((instrument.currency, instrument.metadata_status), (None, "unverified"))

    def test_publish_is_atomic_qualified_loadable_and_exactly_replayable(self):
        first = self.publish()
        self.assertEqual(first["status"], "published")
        self.assertEqual(self.counts(), (1, 1, 4, 1))
        with Session(self.engine) as session:
            release = session.get(DatasetRelease, first["release_id"])
            receipt = session.get(MarketPublication, first["release_id"])
            self.assertEqual(receipt.approval_json, canonical_json(self.approval))
            self.assertEqual(receipt.published_at, release.published_at)
            self.assertEqual(load_market(session, release.id).release_id, release.id)
        second = self.publish()
        self.assertEqual(second, {**first, "status": "already_published"})
        self.assertEqual(self.counts(), (1, 1, 4, 1))

    def test_wrong_target_is_rejected_before_any_database_connection(self):
        with patch.object(self.engine, "connect", side_effect=AssertionError("must not connect")):
            with self.assertRaises(publication.PublicationError) as caught:
                store.publish(self.engine, self.input, self.candidate, self.approval, KEY, "0" * 64)
        self.assertEqual(caught.exception.code, "DATABASE_TARGET_MISMATCH")
        self.assert_unpublished()

    def test_partial_projection_failure_rolls_back_metadata_and_all_publication_rows(self):
        with patch.object(store, "load_market", side_effect=RuntimeError("synthetic pre-commit failure")):
            with self.assertRaises(RuntimeError):
                self.publish()
        self.assert_unpublished()
        self.assertEqual(self.publish()["status"], "published")

    def test_expiry_after_writes_rolls_back_instead_of_publishing(self):
        times = iter((NOW, NOW, NOW + timedelta(hours=1)))
        with self.assertRaises(publication.PublicationError) as caught:
            self.publish(clock=lambda: next(times))
        self.assertEqual(caught.exception.code, "REVIEW_NOT_CURRENT")
        self.assert_unpublished()

    def test_existing_revision_and_calendar_conflicts_are_never_overwritten(self):
        plan = publication.build_plan(self.input, self.candidate, self.approval, KEY, now=NOW)
        with Session(self.engine) as session:
            session.add(MarketRevision(id=plan.revisions[0][0], kind="bars", body_json="{}"))
            session.commit()
        with self.assertRaises(publication.PublicationError) as caught:
            self.publish()
        self.assertEqual(caught.exception.code, "REVISION_CONFLICT")
        self.assert_unpublished((0, 0, 1, 0))

    def test_calendar_version_cannot_be_rebound_and_failed_work_leaves_no_orphans(self):
        with Session(self.engine) as session:
            session.add(MarketRevision(id="f" * 64, kind="calendar", body_json="{}"))
            session.flush()
            session.add(CalendarBinding(calendar_id="XNYS", version="synthetic-v1", revision_id="f" * 64))
            session.commit()
        with self.assertRaises(publication.PublicationError) as caught:
            self.publish()
        self.assertEqual(caught.exception.code, "CALENDAR_BINDING_CONFLICT")
        self.assert_unpublished((0, 0, 1, 1))

    def test_missing_or_conflicting_metadata_is_not_silently_corrected(self):
        with Session(self.engine) as session:
            instrument = session.get(Instrument, A)
            instrument.currency = "EUR"
            session.add(instrument)
            session.commit()
        with self.assertRaises(publication.PublicationError) as caught:
            self.publish()
        self.assertEqual(caught.exception.code, "INSTRUMENT_METADATA_CONFLICT")
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        with Session(self.engine) as session:
            instrument = session.get(Instrument, A)
            self.assertEqual(instrument.currency, "EUR")
            instrument.currency, instrument.symbol = None, "OTHER"
            session.add(instrument)
            session.commit()
        with self.assertRaises(publication.PublicationError) as caught:
            self.publish()
        self.assertEqual(caught.exception.code, "INSTRUMENT_IDENTITY_CONFLICT")

    def test_existing_drafts_are_not_adopted_or_mutated(self):
        plan = publication.build_plan(self.input, self.candidate, self.approval, KEY, now=NOW)
        with Session(self.engine) as session:
            session.add(DatasetRelease(id=plan.release_id, manifest_json=plan.manifest_json,
                                       sha256=plan.release_id[4:], qualification="pending"))
            session.commit()
        with self.assertRaises(publication.PublicationError) as caught:
            self.publish()
        self.assertEqual(caught.exception.code, "RELEASE_CONFLICT")
        self.assert_unpublished((1, 0, 0, 0))

    def test_a_second_review_cannot_rewrite_a_publication_receipt(self):
        first = self.publish()
        self.approval["review"]["reviewer_id"] = "another-synthetic-reviewer"
        self.approval = seal(self.approval["review"])
        with self.assertRaises(publication.PublicationError) as caught:
            self.publish()
        self.assertEqual(caught.exception.code, "PUBLICATION_APPROVAL_CONFLICT")
        self.assertEqual(self.counts(), (1, 1, 4, 1))
        with Session(self.engine) as session:
            self.assertNotEqual(session.get(MarketPublication, first["release_id"]).approval_json,
                                canonical_json(self.approval))

    def test_database_guards_block_approval_update_and_delete(self):
        self.publish()
        for statement in ("UPDATE market_publication SET approval_json = '{}'", "DELETE FROM market_publication"):
            with self.subTest(statement=statement), self.assertRaises(DBAPIError):
                with self.engine.begin() as connection:
                    connection.execute(text(statement))

    def test_loader_integrity_failure_is_sanitized_and_atomic(self):
        with patch.object(store, "load_market", side_effect=Problem(503, "PRIVATE", "test-only-marker")):
            with self.assertRaises(publication.PublicationError) as caught:
                self.publish()
        self.assertEqual(caught.exception.code, "PUBLISHED_INPUT_INTEGRITY_ERROR")
        self.assert_unpublished()

    def test_published_synthetic_release_runs_through_owned_api_and_worker(self):
        release_id = self.publish()["release_id"]
        disabled = client(test_app(self.engine))
        self.addCleanup(disabled.close)
        self.assertEqual(disabled.post("/v1/games", json={}).status_code, 503)
        browser = client(test_app(self.engine, settings(games_enabled=True)))
        self.addCleanup(browser.close)
        guest = browser.post("/v1/guest-sessions", json={}, headers={"Origin": ORIGIN}).json()
        headers = {"Origin": ORIGIN, "X-CSRF-Token": guest["csrf_token"], "Idempotency-Key": "create"}
        created = browser.post("/v1/games", json={
            "name": "Synthetic publication smoke test", "start_date": DAYS[0],
            "initial_cash": "1000", "release_id": release_id,
        }, headers=headers)
        self.assertEqual(created.status_code, 201, created.text)
        game_id = created.json()["id"]
        order = browser.post(f"/v1/games/{game_id}/orders", json={
            "instrument_id": A, "action": "buy", "quantity": "1",
        }, headers={**headers, "Idempotency-Key": "buy", "If-Match": created.headers["etag"]})
        self.assertEqual(order.status_code, 201, order.text)
        advance = browser.post(f"/v1/games/{game_id}/advances", json={"to_date": DAYS[3]},
                               headers={**headers, "Idempotency-Key": "advance", "If-Match": order.headers["etag"]})
        self.assertEqual(advance.status_code, 202, advance.text)
        operation, token = claim(self.engine)
        for _ in range(10):
            if checkpoint(self.engine, operation, token):
                break
        else:
            self.fail("Synthetic worker did not finish")
        with Session(self.engine) as session:
            state = load_state(session.get(Game, game_id))
            self.assertEqual(state.status, "completed")
            self.assertEqual(state.book.cash, Decimal("600.4"))
            self.assertEqual(state.book.quantity(A, "long"), Decimal("2"))

    def test_concurrent_sqlite_publishers_commit_one_immutable_receipt(self):
        with tempfile.TemporaryDirectory(prefix="publication-concurrency-") as directory:
            engine = sqlite_engine("sqlite:///" + str(Path(directory) / "publication.sqlite"))
            try:
                migrate(engine)
                seed_instrument(engine)
                def publish(_):
                    return store.publish(engine, self.input, self.candidate, self.approval, KEY,
                                         store.target_sha256(engine), clock=lambda: NOW)["status"]
                with ThreadPoolExecutor(max_workers=4) as pool:
                    statuses = list(pool.map(publish, range(4)))
                self.assertEqual(statuses.count("published"), 1)
                self.assertEqual(statuses.count("already_published"), 3)
            finally:
                engine.dispose()


class TestPublicationTarget(unittest.TestCase):
    def target(self, url):
        class Target:
            pass
        target = Target()
        target.url = make_url(url)
        return target

    def test_fingerprint_excludes_password_but_pins_host_port_database_and_role(self):
        first = self.target("postgresql://operator:synthetic-password@localhost:15439/finance_test")
        identity = store.target_identity(first)
        self.assertNotIn("synthetic-password", canonical_json(identity))
        self.assertEqual(store.target_sha256(first), store.target_sha256(
            self.target("postgresql://operator:different-password@localhost:15439/finance_test")))
        for url in ("postgresql://other@localhost:15439/finance_test",
                    "postgresql://operator@other-host:15439/finance_test",
                    "postgresql://operator@localhost:5432/finance_test",
                    "postgresql://operator@localhost:15439/other_test"):
            self.assertNotEqual(store.target_sha256(first), store.target_sha256(self.target(url)))

    def test_implicit_targets_and_query_overrides_are_rejected(self):
        for url in ("postgresql:///finance_test", "postgresql://operator@localhost/finance_test?host=other-host",
                    "sqlite:///test.sqlite?uri=true"):
            with self.subTest(url=url), self.assertRaises(publication.PublicationError):
                store.target_sha256(self.target(url))

    def test_schema_is_pinned_and_invalid_or_injected_identifiers_are_rejected(self):
        target = self.target("postgresql://operator@localhost/finance_test")
        self.assertNotEqual(store.target_sha256(target), store.target_sha256(target, "other_schema"))
        for schema in ("", "a,b", 'public"; SELECT 1; --', "x" * 64):
            with self.subTest(schema=schema), self.assertRaises(publication.PublicationError):
                store.target_sha256(target, schema)


class TestPublicationCLI(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="publication-cli-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        values = artifacts(now=publication.utcnow())
        self.paths = [self.root / name for name in ("input.json", "candidate.json", "approval.json")]
        for path, value in zip(self.paths, values):
            path.write_text(canonical_json(value))
        self.args = [piece for flag, path in zip(("--input", "--candidate", "--approval"), self.paths)
                     for piece in (flag, str(path))]

    def run_cli(self, *extra, key=KEY.hex()):
        output = io.StringIO()
        with patch.dict(os.environ, {"FINANCE_PUBLICATION_REVIEW_KEY": key}), redirect_stdout(output):
            status = publication.main([*self.args, *extra])
        return status, json.loads(output.getvalue())

    def test_preview_is_offline_even_with_unusable_database_configuration(self):
        environment = os.environ.copy()
        environment.update(PYTHONPATH=str(ROOT / "src"), FINANCE_PUBLICATION_REVIEW_KEY=KEY.hex(),
                           DATABASE_URL="postgresql://unused@127.0.0.1:1/unreachable")
        result = subprocess.run([
            sys.executable, "-c", "import sys; from finance_app_database_service.market_publication import main; "
            "result=main(); assert 'sqlalchemy' not in sys.modules; raise SystemExit(result)", *self.args,
        ], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "preview")
        self.assertEqual(len(list(self.root.iterdir())), 3)

    def test_execution_needs_a_target_and_missing_keys_fail_closed(self):
        status, result = self.run_cli("--execute")
        self.assertEqual((status, result["code"]), (1, "EXPECTED_TARGET_REQUIRED"))
        status, result = self.run_cli(key="")
        self.assertEqual((status, result["code"]), (1, "REVIEW_KEY_UNAVAILABLE"))

    def test_malformed_duplicate_and_oversize_approval_is_sanitized(self):
        for raw in ('{"review":{},"review":{}}', '{"value":NaN}', "{test-only-sensitive-marker"):
            self.paths[2].write_text(raw)
            status, result = self.run_cli()
            self.assertEqual((status, result["code"]), (1, "INVALID_JSON"))
        with patch.object(publication, "MAX_APPROVAL_BYTES", 1):
            status, result = self.run_cli()
        self.assertEqual((status, result["code"]), (1, "ARTIFACT_CAPACITY_EXCEEDED"))

    def test_explicit_execution_uses_only_the_fenced_isolated_database(self):
        engine = sqlite_engine()
        self.addCleanup(engine.dispose)
        migrate(engine)
        seed_instrument(engine)
        with patch.object(database, "engine", engine):
            status, result = self.run_cli("--execute", "--expected-target-sha256", store.target_sha256(engine))
        self.assertEqual(status, 0, result)
        self.assertEqual(result["status"], "published")

    def test_unmigrated_database_fails_safely_without_implicit_schema_creation(self):
        engine = sqlite_engine()
        self.addCleanup(engine.dispose)
        with patch.object(database, "engine", engine):
            status, result = self.run_cli("--execute", "--expected-target-sha256", store.target_sha256(engine))
        self.assertEqual((status, result["code"]), (1, "DATABASE_PUBLICATION_FAILED"))
        self.assertEqual(inspect(engine).get_table_names(), [])
