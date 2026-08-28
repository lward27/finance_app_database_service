"""Endpoint tests proving invalid ticker input returns HTTP 422 before DB access."""

import asyncio
import unittest
from unittest.mock import MagicMock

from fastapi import Request, HTTPException

from finance_app_database_service.api import (
    ticker_validation_exception_handler,
    save_ticker,
    get_history,
    get_history_last_date,
)
from finance_app_database_service.ticker_normalization import TickerValidationError


class TestTickerValidationAtEndpoints(unittest.TestCase):
    """Verify that invalid ticker symbols trigger 422 and never reach the DB layer."""

    def test_exception_handler_returns_422_json(self):
        """The global handler returns the stable 422 JSON body."""
        request = MagicMock(spec=Request)
        exc = TickerValidationError("bad ticker")
        response = asyncio.run(ticker_validation_exception_handler(request, exc))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.body, b'{"detail":"Invalid ticker symbol"}')

    def test_save_ticker_invalid_raises_before_db(self):
        """POST /tickers raises TickerValidationError before touching the session."""
        async def _test():
            mock_session = MagicMock()
            ticker = MagicMock()
            ticker.ticker = "!!!BAD!!!"
            with self.assertRaises(TickerValidationError):
                await save_ticker(session=mock_session, ticker=ticker)
            mock_session.add.assert_not_called()
            mock_session.commit.assert_not_called()
        asyncio.run(_test())

    def test_get_history_invalid_raises_before_db(self):
        """GET /history raises TickerValidationError before querying the session."""
        async def _test():
            mock_session = MagicMock()
            with self.assertRaises(TickerValidationError):
                await get_history(session=mock_session, ticker_name="   ")
            mock_session.exec.assert_not_called()
        asyncio.run(_test())

    def test_get_history_last_date_invalid_raises_before_db(self):
        """GET /history/last_date raises TickerValidationError before querying."""
        async def _test():
            mock_session = MagicMock()
            with self.assertRaises(TickerValidationError):
                await get_history_last_date(session=mock_session, ticker_name="1INVALID")
            mock_session.exec.assert_not_called()
        asyncio.run(_test())

    def test_save_ticker_valid_reaches_db(self):
        """Valid tickers are normalized and allowed through to the DB layer."""
        async def _test():
            mock_session = MagicMock()
            ticker = MagicMock()
            ticker.ticker = "  aapl  "
            await save_ticker(session=mock_session, ticker=ticker)
            self.assertEqual(ticker.ticker, "AAPL")
            mock_session.add.assert_called_once_with(ticker)
            mock_session.commit.assert_called_once()
        asyncio.run(_test())

    def test_get_history_valid_reaches_db(self):
        """Valid tickers reach the DB layer (mock returns no rows -> 404 path)."""
        async def _test():
            mock_session = MagicMock()
            mock_session.exec.return_value.first.return_value = None
            with self.assertRaises(HTTPException) as ctx:
                await get_history(session=mock_session, ticker_name="msft")
            self.assertEqual(ctx.exception.status_code, 404)
        asyncio.run(_test())


if __name__ == "__main__":
    unittest.main()
