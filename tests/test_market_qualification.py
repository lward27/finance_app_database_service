"""Synthetic comparisons only. These fixtures do not qualify any real source."""

import copy
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import ROUND_UP, localcontext
from pathlib import Path
from unittest.mock import patch

from finance_app_database_service.contracts import canonical_json, content_digest
from finance_app_database_service.market_documents import ActionsDocument, BarsDocument, CalendarDocument
from finance_app_database_service.market_import import validate_export
from finance_app_database_service import market_qualification as qualification

ROOT = Path(__file__).resolve().parents[1]
A = "ins_" + "a" * 32
B = "ins_" + "b" * 32
DAYS = ["2020-08-28", "2020-08-31", "2020-09-01", "2020-09-02", "2020-09-03"]


def refresh_snapshot(value, index=0):
    item = value["instruments"][index]
    item["source_snapshot_id"] = validate_export(item["source_export"], item["symbol"])[0]["content_hash"]


def bundle():
    source = dict(
        schema_version=1, ticker="AAPL", source="yahoo_finance", source_version="synthetic-test-only",
        fetched_at="2020-09-03T18:00:00+00:00", price_basis="yahoo_split_adjusted",
        dividend_basis="yahoo_split_adjusted", exchange_timezone="America/New_York",
        rows=[dict(date=day, open="100", high="105", low="95", close="102", adjusted_close="98",
                   volume=1000, dividend=dividend, split_ratio=split, capital_gain="0")
              for day, dividend, split in zip(DAYS, ["0.2", "0", "0.1", "0", "0"], ["0", "4", "0", "0.5", "2"])],
    )
    citation = dict(provider="Synthetic independent fixture", reference="test-only evidence, not real qualification")
    evidence = dict(
        instrument_id=A, symbol="AAPL", currency="USD", price_basis="as_traded",
        exchange_timezone="America/New_York", from_date=DAYS[0], to_date=DAYS[3],
        terminal_events="none_in_coverage",
        currency_reference=citation, prices_reference=citation, actions_reference=citation,
        terminal_events_reference=citation,
        bars=[dict(date=day, open=opening, close=closing) for day, opening, closing in [
            (DAYS[0], "400", "408"), (DAYS[1], "100", "102"),
            (DAYS[2], "100", "102"), (DAYS[3], "200", "204"),
        ]],
        actions=[
            dict(date=DAYS[0], dividend_per_prior_share="0.8"),
            dict(date=DAYS[1], split_numerator="4", split_denominator="1"),
            dict(date=DAYS[2], dividend_per_prior_share="0.1"),
            dict(date=DAYS[3], split_numerator="1", split_denominator="2"),
        ],
    )
    value = dict(
        schema_version=1,
        calendar=dict(schema_version=1, calendar_id="XNYS", calendar_version="synthetic-v1", sessions=DAYS),
        calendar_reference=citation,
        instruments=[dict(instrument_id=A, symbol="AAPL", from_date=DAYS[0], to_date=DAYS[3],
                          shortable=False, source_snapshot_id="0" * 64, source_export=source, evidence=evidence)],
    )
    # Break shared fixture references so mutations cannot accidentally alter two claims.
    value = json.loads(json.dumps(value))
    refresh_snapshot(value)
    return value


class TestMarketQualification(unittest.TestCase):
    def blocked(self, value, code):
        before = copy.deepcopy(value)
        with self.assertRaises(qualification.QualificationError) as caught:
            qualification.preflight(value)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(value, before)

    def test_reconstructs_split_prices_prior_share_dividends_and_reverse_splits(self):
        value = bundle()
        before = copy.deepcopy(value)
        candidate = qualification.preflight(value)
        self.assertEqual(value, before)
        self.assertEqual(candidate["qualification"], "pending")
        self.assertEqual(candidate["status"], "ready_for_operator_review")
        self.assertEqual(len(candidate["revisions"]), 3)
        self.assertEqual({row["kind"] for row in candidate["revisions"]}, {"calendar", "bars", "actions"})
        for row in candidate["revisions"]:
            contract = {"calendar": CalendarDocument, "bars": BarsDocument, "actions": ActionsDocument}[row["kind"]]
            document = contract.parse_obj(row["document"])
            self.assertEqual(row["id"], content_digest(canonical_json(document)))
        coverage = candidate["coverage"][0]
        self.assertEqual(coverage["source_snapshot_ids"], [value["instruments"][0]["source_snapshot_id"]])
        self.assertEqual(coverage["evidence_sha256"],
                         content_digest(canonical_json(qualification.ReferenceEvidence.parse_obj(
                             value["instruments"][0]["evidence"]))))
        bars = next(row["document"]["bars"] for row in candidate["revisions"] if row["kind"] == "bars")
        self.assertEqual([bar["open"] for bar in bars],
                         ["400.00000000", "100.00000000", "100.00000000", "200.00000000"])
        self.assertNotIn(DAYS[4], [bar["date"] for bar in bars])
        actions = next(row["document"]["actions"] for row in candidate["revisions"] if row["kind"] == "actions")
        self.assertEqual(actions[0]["dividend_per_prior_share"], "0.8")
        self.assertEqual(actions[3]["split_denominator"], "2")
        self.assertNotIn("validation_report_sha256", coverage)

    def test_deterministic_despite_ambient_decimal_precision_and_rounding(self):
        value = bundle()
        first = qualification.preflight(value)
        with localcontext() as context:
            context.prec = 2
            context.rounding = ROUND_UP
            second = qualification.preflight(value)
        self.assertEqual(first, second)

    def test_no_actions_is_a_valid_case_without_fabricated_events(self):
        value = bundle()
        item = value["instruments"][0]
        for row in item["source_export"]["rows"]:
            row["dividend"], row["split_ratio"] = "0", "0"
        item["evidence"]["actions"] = []
        for row in item["evidence"]["bars"]:
            row["open"], row["close"] = "100", "102"
        refresh_snapshot(value)
        candidate = qualification.preflight(value)
        self.assertEqual(next(row["document"]["actions"] for row in candidate["revisions"]
                              if row["kind"] == "actions"), [])

    def test_adjusted_close_is_not_used_as_an_execution_price(self):
        value = bundle()
        first = qualification.preflight(value)
        value["instruments"][0]["source_export"]["rows"][0]["adjusted_close"] = "999"
        refresh_snapshot(value)
        second = qualification.preflight(value)
        self.assertEqual(first["coverage"][0]["bar_revision_id"], second["coverage"][0]["bar_revision_id"])
        self.assertNotEqual(first["input_sha256"], second["input_sha256"])

    def test_changed_raw_export_cannot_reuse_a_snapshot_identity(self):
        value = bundle()
        value["instruments"][0]["source_export"]["rows"][0]["open"] = "101"
        self.blocked(value, "SOURCE_SNAPSHOT_MISMATCH")

    def test_reference_prices_must_match_every_open_and_close(self):
        for field in ("open", "close"):
            value = bundle()
            value["instruments"][0]["evidence"]["bars"][1][field] = "103"
            with self.subTest(field=field):
                self.blocked(value, "PRICE_BASIS_MISMATCH")

    def test_financial_strings_currency_basis_and_terminal_claims_are_strict(self):
        for key, replacement in (("currency", "EUR"), ("price_basis", "yahoo_split_adjusted"),
                                 ("terminal_events", "unknown"), ("instrument_id", "AAPL")):
            value = bundle()
            value["instruments"][0]["evidence"][key] = replacement
            with self.subTest(key=key):
                self.blocked(value, "INVALID_QUALIFICATION_INPUT")
        for replacement in (100, 100.0, "NaN", "1e2"):
            value = bundle()
            value["instruments"][0]["evidence"]["bars"][0]["open"] = replacement
            with self.subTest(replacement=replacement):
                self.blocked(value, "INVALID_QUALIFICATION_INPUT")
        value = bundle()
        value["instruments"][0]["shortable"] = "false"
        self.blocked(value, "INVALID_QUALIFICATION_INPUT")
        value = bundle()
        value["auto_qualify"] = True
        self.blocked(value, "INVALID_QUALIFICATION_INPUT")

    def test_malformed_raw_values_and_envelopes_fail_closed(self):
        for field, replacement in (("open", 100.0), ("dividend", None), ("close", "NaN"),
                                   ("split_ratio", "-1"), ("close", "1e999999999")):
            value = bundle()
            value["instruments"][0]["source_export"]["rows"][0][field] = replacement
            with self.subTest(field=field, replacement=replacement):
                self.blocked(value, "INVALID_SOURCE_EXPORT")
        for field, replacement in (("schema_version", True), ("rows", {}), ("exchange_timezone", "Unknown/Zone"),
                                   ("fetched_at", "not a timestamp")):
            value = bundle()
            value["instruments"][0]["source_export"][field] = replacement
            with self.subTest(field=field):
                self.blocked(value, "SOURCE_CAPACITY_EXCEEDED" if field == "rows" else "INVALID_SOURCE_EXPORT")
        value = bundle()
        del value["instruments"][0]["source_export"]["rows"][0]["close"]
        self.blocked(value, "INVALID_SOURCE_EXPORT")

    def test_missing_zero_and_inconsistent_ohlc_are_never_filled(self):
        for field, replacement, code in (("open", None, "MISSING_OR_ZERO_PRICE"),
                                         ("close", "0", "MISSING_OR_ZERO_PRICE"),
                                         ("low", "104", "INVALID_SOURCE_OHLC"),
                                         ("high", None, "MISSING_OR_ZERO_PRICE")):
            value = bundle()
            value["instruments"][0]["source_export"]["rows"][0][field] = replacement
            refresh_snapshot(value)
            with self.subTest(field=field):
                self.blocked(value, code)

    def test_actions_must_be_complete_exact_sorted_unique_and_supported(self):
        for change in ("missing", "ratio", "dividend", "zero_ratio", "duplicate", "unordered", "terminal"):
            value = bundle()
            actions = value["instruments"][0]["evidence"]["actions"]
            if change == "missing":
                actions.pop(0)
            elif change == "ratio":
                actions[1]["split_numerator"] = "2"
            elif change == "dividend":
                actions[0]["dividend_per_prior_share"] = "0.2"
            elif change == "zero_ratio":
                actions[1]["split_denominator"] = "0"
            elif change == "duplicate":
                actions.append(copy.deepcopy(actions[-1]))
            elif change == "unordered":
                actions.reverse()
            else:
                actions[0]["unverified_terminal_event"] = True
            code = {"duplicate": "INVALID_REFERENCE_ACTIONS", "unordered": "INVALID_REFERENCE_ACTIONS",
                    "terminal": "UNSUPPORTED_TERMINAL_EVENT"}.get(change, "CORPORATE_ACTION_MISMATCH")
            with self.subTest(change=change):
                self.blocked(value, code)

    def test_unexpected_actions_and_actions_outside_the_calendar_fail(self):
        value = bundle()
        item = value["instruments"][0]
        item["source_export"]["rows"][2]["dividend"] = "0"
        refresh_snapshot(value)
        self.blocked(value, "CORPORATE_ACTION_MISMATCH")
        value = bundle()
        value["instruments"][0]["evidence"]["actions"][-1]["date"] = DAYS[4]
        self.blocked(value, "ACTION_OFF_CALENDAR")

    def test_simultaneous_actions_and_capital_gains_are_not_guessed(self):
        for field, replacement, code in (("dividend", "1", "SIMULTANEOUS_ACTIONS_UNSUPPORTED"),
                                         ("capital_gain", "1", "UNSUPPORTED_CAPITAL_GAIN")):
            value = bundle()
            value["instruments"][0]["source_export"]["rows"][1][field] = replacement
            refresh_snapshot(value)
            self.blocked(value, code)

    def test_complete_through_excludes_the_retrieval_session(self):
        value = bundle()
        item = value["instruments"][0]
        item["to_date"] = item["evidence"]["to_date"] = DAYS[4]
        item["evidence"]["bars"].append(dict(date=DAYS[4], open="100", close="102"))
        self.blocked(value, "SOURCE_NOT_COMPLETE")

    def test_source_calendar_gaps_and_non_calendar_rows_are_not_silently_dropped(self):
        value = bundle()
        value["instruments"][0]["source_export"]["rows"].pop(2)
        refresh_snapshot(value)
        self.blocked(value, "SOURCE_CALENDAR_MISMATCH")
        value = bundle()
        value["calendar"]["sessions"].pop(2)
        item = value["instruments"][0]
        item["evidence"]["bars"].pop(2)
        item["evidence"]["actions"].pop(2)
        self.blocked(value, "SOURCE_CALENDAR_MISMATCH")

    def test_calendar_and_reference_bars_must_be_sorted_unique_and_exact(self):
        for change in ("unsorted", "duplicate", "missing_reference", "long_id"):
            value = bundle()
            if change == "unsorted":
                value["calendar"]["sessions"].reverse()
            elif change == "duplicate":
                value["calendar"]["sessions"].append(DAYS[-1])
            elif change == "long_id":
                value["calendar"]["calendar_id"] = "x" * 65
            else:
                value["instruments"][0]["evidence"]["bars"].pop(1)
            self.blocked(value, "REFERENCE_CALENDAR_MISMATCH" if change == "missing_reference" else "INVALID_CALENDAR")

    def test_source_and_evidence_identity_and_timezone_must_match(self):
        value = bundle()
        value["instruments"][0]["evidence"]["instrument_id"] = B
        self.blocked(value, "EVIDENCE_IDENTITY_MISMATCH")
        value = bundle()
        value["instruments"][0]["source_export"]["exchange_timezone"] = "UTC"
        refresh_snapshot(value)
        self.blocked(value, "SOURCE_TIMEZONE_MISMATCH")
        value = bundle()
        value["instruments"][0]["evidence"]["from_date"] = DAYS[1]
        self.blocked(value, "EVIDENCE_IDENTITY_MISMATCH")

    def test_obvious_yahoo_self_comparisons_and_missing_evidence_are_rejected(self):
        for name in ("calendar_reference", "currency_reference", "prices_reference",
                     "actions_reference", "terminal_events_reference"):
            value = bundle()
            container = value if name == "calendar_reference" else value["instruments"][0]["evidence"]
            container[name]["provider"] = "Yahoo Finance"
            with self.subTest(name=name):
                self.blocked(value, "NON_INDEPENDENT_EVIDENCE")
        value = bundle()
        del value["instruments"][0]["evidence"]["terminal_events_reference"]
        self.blocked(value, "INVALID_QUALIFICATION_INPUT")

    def test_coverage_must_contain_two_distinct_actual_sessions(self):
        value = bundle()
        item = value["instruments"][0]
        item["from_date"] = item["evidence"]["from_date"] = DAYS[3]
        self.blocked(value, "INVALID_COVERAGE")
        value = bundle()
        item = value["instruments"][0]
        item["from_date"] = item["evidence"]["from_date"] = "2020-08-29"
        self.blocked(value, "COVERAGE_OFF_CALENDAR")

    def test_duplicate_instruments_and_ambiguous_snapshot_mappings_are_rejected(self):
        value = bundle()
        value["instruments"].append(copy.deepcopy(value["instruments"][0]))
        self.blocked(value, "DUPLICATE_INSTRUMENT")
        value["instruments"][1]["instrument_id"] = B
        value["instruments"][1]["evidence"]["instrument_id"] = B
        self.blocked(value, "DUPLICATE_SOURCE_SNAPSHOT")

    def test_curated_multiple_instruments_can_have_different_coverage(self):
        value = bundle()
        second = copy.deepcopy(value["instruments"][0])
        second["instrument_id"] = second["evidence"]["instrument_id"] = B
        second["symbol"] = second["evidence"]["symbol"] = second["source_export"]["ticker"] = "OTHER"
        second["from_date"] = second["evidence"]["from_date"] = DAYS[1]
        second["evidence"]["bars"].pop(0)
        second["evidence"]["actions"].pop(0)
        value["instruments"].insert(0, second)
        refresh_snapshot(value, 0)
        candidate = qualification.preflight(value)
        self.assertEqual([item["instrument_id"] for item in candidate["coverage"]], [A, B])
        self.assertEqual(len(candidate["revisions"]), 5)

    def test_capacity_limits_reject_instead_of_truncating(self):
        for setting, limit, code in (
            ("MAX_INPUT_BYTES", 1, "INPUT_CAPACITY_EXCEEDED"),
            ("MAX_SOURCE_ROWS", 4, "SOURCE_CAPACITY_EXCEEDED"),
            ("MAX_REVISION_BYTES", 1, "REVISION_CAPACITY_EXCEEDED"),
            ("MAX_RELEASE_BYTES", 1, "CANDIDATE_CAPACITY_EXCEEDED"),
        ):
            with self.subTest(setting=setting), patch.object(qualification, setting, limit):
                self.blocked(bundle(), code)

    def test_price_reconstruction_uses_eight_place_half_even_rounding(self):
        for opening, expected in (("100.000000005", "100.00000000"),
                                  ("100.000000015", "100.00000002")):
            value = bundle()
            item = value["instruments"][0]
            item["source_export"]["rows"][1]["open"] = opening
            item["evidence"]["bars"][1]["open"] = expected
            refresh_snapshot(value)
            candidate = qualification.preflight(value)
            bars = next(row["document"]["bars"] for row in candidate["revisions"] if row["kind"] == "bars")
            self.assertEqual(bars[1]["open"], expected)

    def test_nonzero_dividends_cannot_silently_round_to_zero(self):
        value = bundle()
        item = value["instruments"][0]
        item["source_export"]["rows"][2]["dividend"] = "0.000000001"
        item["evidence"]["actions"][2]["dividend_per_prior_share"] = "0"
        refresh_snapshot(value)
        self.blocked(value, "CORPORATE_ACTION_MISMATCH")

    def test_reconstructed_prices_and_split_factors_are_bounded(self):
        value = bundle()
        row = value["instruments"][0]["source_export"]["rows"][0]
        for field in ("open", "high", "low", "close"):
            row[field] = "99000000000000000000"
        refresh_snapshot(value)
        self.blocked(value, "FINANCIAL_LIMIT_EXCEEDED")
        value = bundle()
        value["instruments"][0]["source_export"]["rows"][-1]["split_ratio"] = "99999999999999999999"
        refresh_snapshot(value)
        self.blocked(value, "INVALID_SPLIT_FACTOR")

    def test_universe_size_is_bounded_before_comparison(self):
        value = bundle()
        value["instruments"] *= 51
        self.blocked(value, "INVALID_QUALIFICATION_INPUT")


class TestMarketQualificationCLI(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="qualification-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.input = self.root / "input.json"
        self.output = self.root / "candidate.json"
        self.input.write_text(canonical_json(bundle()))

    def run_cli(self, *extra):
        output = io.StringIO()
        with redirect_stdout(output):
            status = qualification.main(["--input", str(self.input), *extra])
        return status, json.loads(output.getvalue())

    def test_default_is_read_only_summary_without_financial_rows(self):
        before = set(self.root.iterdir())
        status, result = self.run_cli()
        self.assertEqual(status, 0)
        self.assertEqual(set(self.root.iterdir()), before)
        self.assertFalse(result["output_written"])
        self.assertEqual(result["qualification"], "pending")
        self.assertNotIn("revisions", result)

    def test_explicit_output_is_private_content_addressed_and_never_overwritten(self):
        status, result = self.run_cli("--output", str(self.output))
        self.assertEqual(status, 0)
        body = self.output.read_text().rstrip("\n")
        self.assertEqual(content_digest(body), result["candidate_sha256"])
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(json.loads(body)["qualification"], "pending")
        before = self.output.read_bytes()
        status, result = self.run_cli("--output", str(self.output))
        self.assertEqual(status, 1)
        self.assertEqual(result["code"], "INPUT_OR_OUTPUT_ERROR")
        self.assertEqual(self.output.read_bytes(), before)

    def test_duplicate_json_nonfinite_and_malformed_inputs_are_sanitized(self):
        for raw in ('{"schema_version":1,"schema_version":1}', '{"value":NaN}',
                    '{"value":Infinity}', "{test-only-sensitive-marker", "[]", "\xff"):
            self.input.write_bytes(raw.encode("latin1"))
            with self.subTest(raw=raw):
                status, result = self.run_cli("--output", str(self.output))
                self.assertEqual(status, 1)
                self.assertIn(result["code"], ("INVALID_JSON", "INVALID_QUALIFICATION_INPUT"))
                self.assertNotIn("test-only-sensitive-marker", canonical_json(result))
                self.assertFalse(self.output.exists())

    def test_input_bound_and_io_errors_do_not_create_an_output(self):
        with patch.object(qualification, "MAX_INPUT_BYTES", 1):
            status, result = self.run_cli("--output", str(self.output))
        self.assertEqual(status, 1)
        self.assertEqual(result["code"], "INPUT_CAPACITY_EXCEEDED")
        self.input.unlink()
        status, result = self.run_cli("--output", str(self.output))
        self.assertEqual(status, 1)
        self.assertEqual(result["code"], "INPUT_OR_OUTPUT_ERROR")
        self.assertFalse(self.output.exists())

    def test_comparison_failure_does_not_write_a_candidate(self):
        value = bundle()
        value["instruments"][0]["evidence"]["bars"][0]["open"] = "999"
        self.input.write_text(canonical_json(value))
        status, result = self.run_cli("--output", str(self.output))
        self.assertEqual(status, 1)
        self.assertEqual(result["code"], "PRICE_BASIS_MISMATCH")
        self.assertFalse(self.output.exists())

    def test_excessive_json_nesting_is_blocked(self):
        self.input.write_text("[" * 2000 + "0" + "]" * 2000)
        status, result = self.run_cli("--output", str(self.output))
        self.assertEqual(status, 1)
        self.assertEqual(result["code"], "INVALID_JSON")
        self.assertFalse(self.output.exists())

    def test_module_runs_offline_without_database_dependencies(self):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        environment["DATABASE_URL"] = "postgresql://unused@127.0.0.1:1/not_a_database"
        result = subprocess.run([
            sys.executable, "-c",
            "import sys; from finance_app_database_service.market_qualification import main; "
            "assert 'sqlalchemy' not in sys.modules; raise SystemExit(main())",
            "--input", str(self.input),
        ], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["qualification"], "pending")
