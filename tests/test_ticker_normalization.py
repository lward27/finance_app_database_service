"""Unit tests for the pure ticker normalization module."""

import unittest

from finance_app_database_service.ticker_normalization import (
    normalize_ticker,
    TickerValidationError,
)


class TestNormalizeTicker(unittest.TestCase):
    """Tests for normalize_ticker covering valid and invalid inputs."""

    def test_valid_uppercase(self):
        self.assertEqual(normalize_ticker("AAPL"), "AAPL")

    def test_valid_lowercase(self):
        self.assertEqual(normalize_ticker("aapl"), "AAPL")

    def test_valid_mixed_case(self):
        self.assertEqual(normalize_ticker("GoOg"), "GOOG")

    def test_valid_with_whitespace(self):
        self.assertEqual(normalize_ticker("  MSFT  "), "MSFT")

    def test_valid_with_leading_trailing_newline(self):
        self.assertEqual(normalize_ticker("\nBRK.B\n"), "BRK.B")

    def test_valid_with_hyphen(self):
        self.assertEqual(normalize_ticker("  bf-a  "), "BF-A")

    def test_valid_single_letter(self):
        self.assertEqual(normalize_ticker("f"), "F")

    def test_valid_ten_chars(self):
        self.assertEqual(normalize_ticker("ABCDEFGHIJ"), "ABCDEFGHIJ")

    def test_empty_string_raises(self):
        with self.assertRaises(TickerValidationError):
            normalize_ticker("")

    def test_whitespace_only_raises(self):
        with self.assertRaises(TickerValidationError):
            normalize_ticker("   ")

    def test_none_raises(self):
        with self.assertRaises(TickerValidationError):
            normalize_ticker(None)  # type: ignore[arg-type]

    def test_invalid_characters_raises(self):
        with self.assertRaises(TickerValidationError):
            normalize_ticker("A@PL")

    def test_starts_with_digit_raises(self):
        with self.assertRaises(TickerValidationError):
            normalize_ticker("1AAPL")

    def test_too_long_raises(self):
        with self.assertRaises(TickerValidationError):
            normalize_ticker("ABCDEFGHIJK")

    def test_underscore_raises(self):
        with self.assertRaises(TickerValidationError):
            normalize_ticker("A_B")


if __name__ == "__main__":
    unittest.main()
