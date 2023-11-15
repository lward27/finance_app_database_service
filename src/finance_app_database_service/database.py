from sqlmodel import SQLModel, create_engine, Session, select
from finance_app_database_service.models import Ticker
import csv
import os

engine = create_engine("postgresql://postgres:topsecretpassword@0.0.0.0:5432/testdb")

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)

def populate_tickers_in_db():
    session = Session(engine)
    tickers = []
    tickers = session.exec(select(Ticker).offset(0).limit(10)).all()
    if len(tickers) == 0:
        # populate tickers table
        filename = 'finance_app_database_service/tickers.csv'
        with open(filename, 'r') as csvfile:
            datareader = csv.reader(csvfile)
            next(datareader) # skip header
            for row in datareader:
                new_ticker = Ticker(
                    ticker=row[0],
                    name=row[1],
                    exchange=row[2]
                )
                session.add(new_ticker)
            session.commit()
    session.close()

