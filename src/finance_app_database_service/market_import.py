"""Pure validation for a full Yahoo history response, without guessing adjustments."""

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo


def decimal_value(value, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError("Financial values must be decimal strings")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("Invalid decimal")
    if not result.is_finite() or result < 0:
        raise ValueError("Invalid financial value")
    return result


def validate_export(payload, ticker):
    if (payload.get("schema_version") != 1 or payload.get("ticker") != ticker
            or payload.get("source") != "yahoo_finance"
            or payload.get("price_basis") != "yahoo_split_adjusted"
            or payload.get("dividend_basis") != "yahoo_split_adjusted"):
        raise ValueError("Unexpected history export contract")
    if not isinstance(payload.get("source_version"), str) or not payload["source_version"]:
        raise ValueError("Missing source version")
    tz = ZoneInfo(payload["exchange_timezone"])
    fetched = datetime.fromisoformat(payload["fetched_at"])
    if fetched.tzinfo is None:
        raise ValueError("Missing retrieval timezone")
    local_today = fetched.astimezone(tz).date()
    rows = payload["rows"]
    if not isinstance(rows, list) or not rows or len(rows) > 100000:
        raise ValueError("Invalid history row count")
    normalized = []
    previous = None
    for row in rows:
        ts = date.fromisoformat(row["date"])
        if (previous is not None and ts <= previous) or ts > local_today:
            raise ValueError("History dates must be unique, sorted, and not future")
        previous = ts
        values = {field: decimal_value(row[field], nullable=True)
                  for field in ("open", "high", "low", "close", "adjusted_close")}
        volume = row["volume"]
        if volume is not None and (type(volume) is not int or not 0 <= volume <= 2**63 - 1):
            raise ValueError("Invalid volume")
        actions = {field: decimal_value(row[field])
                   for field in ("dividend", "split_ratio", "capital_gain")}
        normalized.append({"ts": ts, **values, "volume": volume, **actions})
    factor = Decimal(1)
    for row in reversed(normalized):
        # A split on this session already applies to this session's prices.
        row["subsequent_split_factor"] = factor
        if row["split_ratio"]:
            factor *= row["split_ratio"]
    digest_payload = {k: v for k, v in payload.items() if k != "fetched_at"}
    digest = hashlib.sha256(json.dumps(
        digest_payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()).hexdigest()
    return {
        "source_version": payload["source_version"],
        "exchange_timezone": payload["exchange_timezone"],
        "fetched_at": fetched.astimezone(timezone.utc).replace(tzinfo=None),
        "complete_through": min(local_today - timedelta(days=1), normalized[-1]["ts"]),
        "source_last_date": normalized[-1]["ts"],
        "row_count": len(normalized), "content_hash": digest,
    }, normalized
