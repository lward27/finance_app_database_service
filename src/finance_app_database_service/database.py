import logging
import os
import time
from importlib import resources

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import SQLModel, create_engine, Session, select

from finance_app_database_service.models import Ticker
import csv

log = logging.getLogger(__name__)


def _build_database_url() -> str:
    """Resolve DB connection using explicit PG env vars first, then DATABASE_URL."""
    host = os.getenv("PGHOST", "postgresql.apps-prod.svc.cluster.local")
    port = os.getenv("PGPORT", "5432")
    database = os.getenv("PGDATABASE", "finance_app")
    user = os.getenv("PGUSER", "finance_app_user")
    password = os.getenv("PGPASSWORD")
    fallback_database_url = os.getenv("DATABASE_URL")

    if password:
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"
    if fallback_database_url:
        return fallback_database_url
    return "postgresql://postgres:topsecretpassword@postgresql.apps-prod.svc.cluster.local:5432/testdb"


DATABASE_URL = _build_database_url()
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)


def wait_for_database(max_attempts: int = 30, base_delay_seconds: float = 1.0) -> None:
    """Block until Postgres is reachable, using exponential backoff."""
    for attempt in range(1, max_attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            log.info("Database connection established.")
            return
        except SQLAlchemyError as exc:
            if attempt >= max_attempts:
                raise
            delay = min(base_delay_seconds * (2 ** (attempt - 1)), 10.0)
            log.warning(
                "Database not ready (attempt %s/%s): %s. Retrying in %.1fs",
                attempt,
                max_attempts,
                exc,
                delay,
            )
            time.sleep(delay)

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)

def populate_tickers_in_db():
    with Session(engine) as session:
        existing = session.exec(select(Ticker).offset(0).limit(1)).all()
        if len(existing) > 0:
            return
        with resources.files("finance_app_database_service").joinpath("tickers.csv").open("r") as csvfile:
            datareader = csv.reader(csvfile)
            next(datareader)  # skip header
            for row in datareader:
                new_ticker = Ticker(
                    ticker=row[0],
                    name=row[1],
                    exchange=row[2],
                )
                session.add(new_ticker)
            session.commit()
