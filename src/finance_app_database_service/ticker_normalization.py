"""Pure ticker-symbol normalization module.

Provides trim/uppercase normalization and structural validation for ticker
symbols.  Invalid input raises ``TickerValidationError`` so that FastAPI can
convert it into a stable HTTP 422 response before any database access.
"""

import re


class TickerValidationError(ValueError):
    """Raised when a ticker symbol fails structural validation."""

    pass


# Reasonable ticker regex: starts with a letter, then up to 9 more letters,
# digits, dots, or hyphens.  Total length 1-10 characters.
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def normalize_ticker(symbol: str) -> str:
    """Normalize and validate a ticker symbol.

    Steps:
      1. Strip leading/trailing whitespace.
      2. Convert to uppercase.
      3. Validate against a structural regex.

    Args:
        symbol: Raw ticker symbol string.

    Returns:
        The normalized (trimmed and uppercased) ticker symbol.

    Raises:
        TickerValidationError: If the symbol is empty, whitespace-only, or
        structurally invalid.
    """
    if not isinstance(symbol, str):
        raise TickerValidationError("Ticker symbol must be a string")

    normalized = symbol.strip().upper()

    if not normalized:
        raise TickerValidationError("Ticker symbol cannot be empty")

    if not _TICKER_RE.match(normalized):
        raise TickerValidationError(
            f"Invalid ticker symbol: {normalized!r}"
        )

    return normalized
