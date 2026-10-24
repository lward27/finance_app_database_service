"""Decimal policy shared by API contracts and the future simulation engine."""

from decimal import Decimal, DecimalException, ROUND_DOWN, ROUND_HALF_EVEN, localcontext

from pydantic import constr

MoneyString = constr(strict=True, regex=r"^-?(?:0|[1-9][0-9]{0,19})(?:\.[0-9]{1,8})?$")
QuantityString = constr(strict=True, regex=r"^(?:0|[1-9][0-9]{0,19})(?:\.[0-9]{1,8})?$")
RateString = constr(strict=True, regex=r"^(?:0(?:\.[0-9]{1,8})?|1(?:\.0{1,8})?)$")
MONEY_STEP = Decimal("0.00000001")
QUANTITY_STEP = Decimal("0.00000001")


def money(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("A finite Decimal is required")
    if value.copy_abs() >= Decimal("1e20"):
        raise ValueError("Money exceeds NUMERIC(28,8)")
    with localcontext() as context:
        context.prec = 60
        result = value.quantize(MONEY_STEP, rounding=ROUND_HALF_EVEN)
    if abs(result) >= Decimal("1e20"):
        raise ValueError("Money exceeds NUMERIC(28,8)")
    return result


def notional_quantity(notional: Decimal, price: Decimal) -> Decimal:
    """Never spend more than the requested notional because of share rounding."""
    if any(not isinstance(v, Decimal) or not v.is_finite() or v <= 0
           for v in (notional, price)):
        raise ValueError("Positive finite Decimals are required")
    try:
        with localcontext() as context:
            context.prec = 60
            quotient = notional / price
            if quotient >= Decimal("1e20"):
                raise ValueError("Quantity exceeds NUMERIC(28,8)")
            return quotient.quantize(QUANTITY_STEP, rounding=ROUND_DOWN)
    except DecimalException:
        raise ValueError("Quantity exceeds decimal limits") from None
