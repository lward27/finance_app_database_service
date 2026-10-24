import unittest
from decimal import Decimal

from pydantic import BaseModel, ValidationError

from finance_app_database_service.financial import MoneyString, money, notional_quantity
from finance_app_database_service.settings import SecuritySettings


class MoneyContract(BaseModel):
    amount: MoneyString


class TestFinancialPolicy(unittest.TestCase):
    def test_financial_json_values_must_be_strings_with_bounded_precision(self):
        for value in (1, 1.25, "NaN", "Infinity", "1e3", "01.00", "1.000000001", "9" * 21):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                MoneyContract(amount=value)
        self.assertEqual(MoneyContract(amount="-100.125").json(), '{"amount": "-100.125"}')

    def test_money_rounding_is_half_even_at_eight_decimal_places(self):
        self.assertEqual(money(Decimal("1.000000005")), Decimal("1.00000000"))
        self.assertEqual(money(Decimal("1.000000015")), Decimal("1.00000002"))

    def test_notional_conversion_rounds_down_without_exceeding_budget(self):
        quantity = notional_quantity(Decimal("100"), Decimal("3"))
        self.assertEqual(quantity, Decimal("33.33333333"))
        self.assertLessEqual(quantity * Decimal("3"), Decimal("100"))

    def test_financial_calculations_reject_floats_nonfinite_values_and_overflow(self):
        for value in (1.5, Decimal("NaN"), Decimal("Infinity"), Decimal("1e20"), Decimal("1e1000")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                money(value)
        for notional, price in (
            (Decimal("1"), Decimal("0")), (Decimal("-1"), Decimal("1")),
            (1.0, Decimal("1")), (Decimal("1e30"), Decimal("1")),
            (Decimal("1e1000"), Decimal("1")),
        ):
            with self.subTest(notional=notional, price=price), self.assertRaises(ValueError):
                notional_quantity(notional, price)

    def test_credentialed_origins_are_explicit_and_secure(self):
        for origin in ("*", "https://*.test", "http://example.test",
                       "https://site.test/", "https://user:password@site.test",
                       "https://site.test?query=x", "https://site.test#fragment"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                SecuritySettings(allowed_origins=(origin,))
        self.assertEqual(SecuritySettings().allowed_origins, ())
        SecuritySettings(allowed_origins=("https://frontend.test", "http://localhost:3000"))
