"""One slow, restart-safe full-catalog import through yfinance_wrapper.

Defaults to a read-only plan. --execute is required to create tables or fetch data.
"""

import argparse
import hashlib
import json
import logging
import math
import os
import random
import signal
import threading
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen

from sqlalchemy import func, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import SQLModel, Session, select

from finance_app_database_service.database import engine
from finance_app_database_service.market_import import validate_export
from finance_app_database_service.market_models import (
    MarketBackfill, MarketBackfillItem, MarketBackfillThrottle,
    MarketHistory, MarketSnapshot,
)
from finance_app_database_service.models import Ticker

log = logging.getLogger(__name__)
TERMINAL = ("complete", "no_data", "failed")
LOCK_ID = 726649231
STOP = threading.Event()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def retry_after(value, now=None):
    if not value:
        return 0
    now = now or datetime.now(timezone.utc)
    try:
        result = float(value)
        return max(0, result) if math.isfinite(result) else 0
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - now).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


def fetch_export(base_url, ticker):
    url = base_url.rstrip("/") + "/history/full?" + urlencode({"ticker_name": ticker})
    with urlopen(url, timeout=120) as response:
        raw = response.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError("Export exceeds size limit")
        return json.loads(raw)


def progress(session, name):
    counts = session.exec(
        select(MarketBackfillItem.status, func.count())
        .where(MarketBackfillItem.backfill_name == name)
        .group_by(MarketBackfillItem.status)
    ).all()
    return dict(counts)


def initialize(session, name, config):
    job = session.get(MarketBackfill, name)
    encoded = json.dumps(config, sort_keys=True)
    if job is not None:
        if job.config_json != encoded:
            raise ValueError("Existing backfill configuration differs; use the original options")
        return job
    job = MarketBackfill(name=name, config_json=encoded)
    session.add(job)
    session.flush()
    # Capture membership once. Later catalog changes cannot skip or duplicate work.
    tickers = session.exec(select(Ticker).order_by(Ticker.id)).all()
    for ticker in tickers:
        session.add(MarketBackfillItem(
            backfill_name=name, ticker_id=ticker.id, ticker=ticker.ticker.strip().upper(),
        ))
    if session.get(MarketBackfillThrottle, 1) is None:
        session.add(MarketBackfillThrottle())
    session.commit()
    return job


def postpone(session, seconds, streak=None):
    gate = session.get(MarketBackfillThrottle, 1)
    gate.next_request_at = max(gate.next_request_at, utcnow() + timedelta(seconds=seconds))
    if streak is not None:
        gate.rate_limit_streak = streak
    session.add(gate)
    session.commit()


def wait_for_slot(session, interval):
    gate = session.get(MarketBackfillThrottle, 1)
    delay = max(0, (gate.next_request_at - utcnow()).total_seconds())
    if STOP.wait(delay):
        return False
    # Persist the reservation BEFORE making the network call, including on crashes.
    gate.next_request_at = utcnow() + timedelta(seconds=interval)
    session.add(gate)
    session.commit()
    return True


def save_export(session, item, payload, batch_size, write_pause, check_lock=lambda: None):
    metadata, rows = validate_export(payload, item.ticker)
    content_hash = metadata.pop("content_hash")
    snapshot_id = hashlib.sha256(f"{item.ticker_id}:{content_hash}".encode()).hexdigest()
    snapshot = session.get(MarketSnapshot, snapshot_id)
    if snapshot is None:
        snapshot = MarketSnapshot(
            id=snapshot_id, ticker_id=item.ticker_id, ticker=item.ticker, **metadata
        )
        session.add(snapshot)
        session.commit()
    if snapshot.status != "complete":
        insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
        for start in range(0, len(rows), batch_size):
            check_lock()
            values = [{"snapshot_id": snapshot_id, **row} for row in rows[start:start + batch_size]]
            session.execute(insert(MarketHistory).values(values).on_conflict_do_nothing(
                index_elements=["snapshot_id", "ts"]
            ))
            session.commit()
            if write_pause:
                STOP.wait(write_pause)
        count = session.exec(select(func.count()).select_from(MarketHistory).where(
            MarketHistory.snapshot_id == snapshot_id
        )).one()
        if count != snapshot.row_count:
            raise ValueError("Incomplete imported snapshot")
        snapshot.status = "complete"
        session.add(snapshot)
    item.snapshot_id = snapshot_id
    item.status = "complete"
    item.error_code = None
    item.updated_at = utcnow()
    session.add(item)
    session.commit()
    return snapshot


def process_item(session, item, config, check_lock=lambda: None):
    item.attempts += 1
    item.status = "fetching"
    item.updated_at = utcnow()
    session.add(item)
    session.commit()
    try:
        payload = fetch_export(config["wrapper_url"], item.ticker)
    except HTTPError as exc:
        if exc.code == 429:
            gate = session.get(MarketBackfillThrottle, 1)
            streak = gate.rate_limit_streak + 1
            delay = max(
                retry_after(exc.headers.get("Retry-After")),
                min(21600, 900 * 2 ** min(streak - 1, 5)),
            ) + random.uniform(0, 30)
            item.status, item.error_code = "retry", "upstream_rate_limited"
            session.add(item)
            postpone(session, delay, streak)
            log.warning("Rate limited; global cooldown %.0fs; ticker_id=%s", delay, item.ticker_id)
            return
        if exc.code == 404:
            item.status, item.error_code = "no_data", "no_upstream_history"
        elif exc.code in (408, 500, 502, 503, 504):
            item.failures += 1
            item.status = "failed" if item.failures >= config["max_failures"] else "retry"
            item.error_code = "upstream_unavailable"
            session.add(item)
            postpone(session, max(
                retry_after(exc.headers.get("Retry-After")),
                min(3600, 60 * 2 ** min(item.failures, 6)),
            ))
        else:
            item.status, item.error_code = "failed", f"upstream_http_{exc.code}"
    except (URLError, TimeoutError, OSError):
        item.failures += 1
        item.status = "failed" if item.failures >= config["max_failures"] else "retry"
        item.error_code = "transport_error"
        session.add(item)
        postpone(session, min(3600, 60 * 2 ** min(item.failures, 6)))
    except (ValueError, TypeError):
        item.status, item.error_code = "failed", "invalid_export"
    else:
        check_lock()
        try:
            snapshot = save_export(
                session, item, payload, config["batch_size"], config["write_pause"], check_lock
            )
        except (ValueError, KeyError, TypeError, OverflowError):
            session.rollback()
            item.status, item.error_code = "failed", "invalid_export"
        else:
            postpone(session, 0, streak=0)
            log.info("Imported ticker_id=%s ticker=%s rows=%s snapshot=%s",
                     item.ticker_id, item.ticker, snapshot.row_count, snapshot.id)
            return
    item.updated_at = utcnow()
    session.add(item)
    session.commit()
    log.info("Ticker result ticker_id=%s status=%s code=%s",
             item.ticker_id, item.status, item.error_code)


def assert_lock(connection):
    owned = connection.execute(text(
        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
        "AND pid=pg_backend_pid() AND objid=:key AND granted)"
    ), {"key": LOCK_ID}).scalar()
    if not owned:
        raise RuntimeError("Backfill advisory lock was lost")


def run(database_engine, name, config, max_tickers=None):
    # One lock across ALL named backfills. Keep this connection for the run.
    with database_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock:
        if not lock.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_ID}).scalar():
            raise RuntimeError("Another market backfill already owns the upstream budget")
        try:
            SQLModel.metadata.create_all(database_engine, tables=[
                model.__table__ for model in (
                    MarketBackfill, MarketBackfillItem, MarketBackfillThrottle,
                    MarketSnapshot, MarketHistory,
                )
            ])
            with Session(database_engine) as session:
                job = initialize(session, name, config)
                processed = 0
                while not STOP.is_set():
                    # A lost lock connection must abort, never silently reconnect.
                    assert_lock(lock)
                    item = session.exec(select(MarketBackfillItem).where(
                        MarketBackfillItem.backfill_name == name,
                        MarketBackfillItem.status.notin_(TERMINAL),
                    ).order_by(MarketBackfillItem.ticker_id).limit(1)).first()
                    if item is None:
                        counts = progress(session, name)
                        job.status = ("completed_with_gaps" if
                                      counts.get("failed", 0) or counts.get("no_data", 0)
                                      else "complete")
                        job.updated_at = utcnow()
                        session.add(job)
                        session.commit()
                        log.info("Backfill finished name=%s counts=%s", name, json.dumps(counts))
                        return 0
                    size = lock.execute(text(
                        "SELECT pg_database_size(current_database())"
                    )).scalar()
                    if size >= config["max_database_bytes"]:
                        raise RuntimeError("Database size safety ceiling reached; backfill paused")
                    if not wait_for_slot(session, config["interval_seconds"]):
                        break
                    assert_lock(lock)
                    process_item(session, item, config, lambda: assert_lock(lock))
                    job.updated_at = utcnow()
                    session.add(job)
                    session.commit()
                    if item.status in TERMINAL:
                        processed += 1
                        counts = progress(session, name)
                        total = sum(counts.get(state, 0) for state in TERMINAL)
                        if total % config["ticker_batch_size"] == 0:
                            postpone(session, config["batch_pause_seconds"])
                        if max_tickers is not None and processed >= max_tickers:
                            return 0
                return 130
        finally:
            if not lock.invalidated:
                try:
                    lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_ID})
                except SQLAlchemyError:
                    log.warning("Lock connection is unavailable during cleanup")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--name", default="yahoo-full-history-v1")
    parser.add_argument("--wrapper-url", default=os.getenv(
        "YFINANCE_SERVICE_URL", "http://yfinance-wrapper.apps-prod.svc.cluster.local:8090"
    ))
    parser.add_argument("--interval-seconds", type=float, default=60)
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--ticker-batch-size", type=int, default=10)
    parser.add_argument("--batch-pause-seconds", type=float, default=300)
    parser.add_argument("--write-pause", type=float, default=0.25)
    parser.add_argument("--max-failures", type=int, default=5)
    parser.add_argument("--max-tickers", type=int)
    parser.add_argument("--max-database-bytes", type=int, default=6 * 1024**3)
    args = parser.parse_args()
    if (not math.isfinite(args.interval_seconds) or args.interval_seconds < 60
            or not 1 <= args.batch_size <= 1000 or args.ticker_batch_size < 1
            or not math.isfinite(args.batch_pause_seconds) or args.batch_pause_seconds < 0
            or not math.isfinite(args.write_pause) or args.write_pause < 0
            or args.max_failures < 1 or args.max_database_bytes < 1
            or (args.max_tickers is not None and args.max_tickers < 1)):
        parser.error("Invalid backfill limits (minimum Yahoo interval is 60 seconds)")
    parsed = urlparse(args.wrapper_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        parser.error("Wrapper URL must be HTTP(S), without embedded credentials")
    config = {key: value for key, value in vars(args).items()
              if key not in ("execute", "name", "max_tickers")}
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not args.execute:
        with Session(engine) as session:
            count = session.exec(select(func.count()).select_from(Ticker)).one()
        print(json.dumps({"mode": "plan", "ticker_records": count, "config": config,
                          "minimum_request_hours": count * args.interval_seconds / 3600,
                          "writes": False, "upstream_requests": 0}))
        return 0
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    return run(engine, args.name, config, args.max_tickers)


if __name__ == "__main__":
    raise SystemExit(main())
