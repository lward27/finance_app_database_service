"""Explicit, resumable catalog bootstrap. Never merge symbols or call Yahoo."""

import argparse
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from finance_app_database_service.database import engine
from finance_app_database_service.foundation_models import Instrument, InstrumentAlias, LegacyTickerMap
from finance_app_database_service.models import Ticker


def insert_missing(session: Session, model, values, key_columns):
    dialect = session.get_bind().dialect.name
    insert = {"postgresql": pg_insert, "sqlite": sqlite_insert}.get(dialect)
    if insert is None:
        raise RuntimeError("Only PostgreSQL and test SQLite are supported")
    return session.execute(
        insert(model).values(values).on_conflict_do_nothing(index_elements=key_columns)
    )


def bootstrap_catalog(session: Session, execute: bool = False, batch_size: int = 250) -> dict:
    if not 1 <= batch_size <= 1000:
        raise ValueError("Batch size must be between 1 and 1000")
    missing = select(Ticker).outerjoin(
        LegacyTickerMap, LegacyTickerMap.ticker_id == Ticker.id
    ).where(LegacyTickerMap.ticker_id.is_(None))
    count = session.exec(select(func.count()).select_from(missing.subquery())).one()
    result = {"execute": execute, "unmapped_tickers": count, "mapped": 0}
    if not execute:
        return result
    while True:
        tickers = session.exec(missing.order_by(Ticker.id).limit(batch_size)).all()
        if not tickers:
            return result
        for ticker in tickers:
            instrument_id = "ins_" + uuid5(
                NAMESPACE_URL, f"urn:finance:legacy-ticker:{ticker.id}"
            ).hex
            insert_missing(session, Instrument, {
                "id": instrument_id, "symbol": ticker.ticker.strip().upper(),
                "name": ticker.name, "exchange": ticker.exchange.strip().upper(),
                "currency": None, "metadata_status": "unverified",
                "created_at": datetime.utcnow(),
            }, ["id"])
            insert_missing(session, InstrumentAlias, {
                "instrument_id": instrument_id, "symbol": ticker.ticker.strip().upper(),
                "source": "legacy_catalog",
            }, ["instrument_id", "symbol", "source"])
            inserted = insert_missing(session, LegacyTickerMap, {
                "ticker_id": ticker.id, "instrument_id": instrument_id,
            }, ["ticker_id"])
            result["mapped"] += inserted.rowcount
        session.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Write mappings; default is read-only")
    parser.add_argument("--batch-size", type=int, default=250)
    args = parser.parse_args()
    with Session(engine) as session:
        print(bootstrap_catalog(session, args.execute, args.batch_size))


if __name__ == "__main__":
    main()
