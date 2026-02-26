from sqlmodel import SQLModel, create_engine, Session, select
from finance_app_database_service.models import Ticker
import csv
import os
from importlib import resources

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:topsecretpassword@postgresql.apps-prod.svc.cluster.local:5432/testdb",
)
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

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

