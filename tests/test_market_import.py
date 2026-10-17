import copy
import unittest
from datetime import date
from decimal import Decimal

from finance_app_database_service.market_import import validate_export


def export():
    rows = []
    for day, split, dividend in [
        ("2020-08-28", "0", "0.20"), ("2020-08-31", "4", "0"),
        ("2021-01-04", "0.5", "0"), ("2021-01-05", "0", "0"),
    ]:
        rows.append({
            "date": day, "open": "100", "high": "105", "low": "95",
            "close": "102", "adjusted_close": "98", "volume": 1000,
            "dividend": dividend, "split_ratio": split, "capital_gain": "0",
        })
    return {
        "schema_version": 1, "ticker": "AAPL", "source": "yahoo_finance",
        "source_version": "1.2.0", "fetched_at": "2021-01-05T18:00:00+00:00",
        "price_basis": "yahoo_split_adjusted", "dividend_basis": "yahoo_split_adjusted",
        "exchange_timezone": "America/New_York", "rows": rows,
    }


class TestExportValidation(unittest.TestCase):
    def test_actions_and_split_factors_include_future_splits_not_same_day(self):
        metadata, rows = validate_export(export(), "AAPL")
        self.assertEqual(metadata["complete_through"], date(2021, 1, 4))
        self.assertEqual([r["subsequent_split_factor"] for r in rows],
                         [Decimal("2"), Decimal("0.5"), Decimal("1"), Decimal("1")])
        self.assertEqual(rows[0]["dividend"], Decimal("0.20"))
        self.assertEqual(rows[1]["split_ratio"], Decimal("4"))
        self.assertEqual(rows[0]["adjusted_close"], Decimal("98"))

    def test_retrieval_timestamp_does_not_change_content_hash(self):
        first = export()
        second = copy.deepcopy(first)
        second["fetched_at"] = "2021-01-06T18:00:00+00:00"
        self.assertEqual(validate_export(first, "AAPL")[0]["content_hash"],
                         validate_export(second, "AAPL")[0]["content_hash"])

    def test_rejects_ambiguous_basis_wrong_symbol_duplicate_future_dates_and_floats(self):
        for field, value in [("price_basis", "raw"), ("ticker", "MSFT")]:
            payload = export()
            payload[field] = value
            with self.assertRaises(ValueError):
                validate_export(payload, "AAPL")
        for field, value in [("open", 100.0), ("dividend", None),
                             ("split_ratio", "-1"), ("volume", True),
                             ("close", "NaN"), ("volume", 2**64)]:
            payload = export()
            payload["rows"][0][field] = value
            with self.assertRaises(ValueError):
                validate_export(payload, "AAPL")
        for value in ("2021-01-06", "2020-08-31"):
            payload = export()
            payload["rows"][-1]["date"] = value
            with self.assertRaises(ValueError):
                validate_export(payload, "AAPL")

    def test_preserves_missing_prices_instead_of_fabricating_a_bar(self):
        payload = export()
        payload["rows"][0]["open"] = None
        _, rows = validate_export(payload, "AAPL")
        self.assertIsNone(rows[0]["open"])
