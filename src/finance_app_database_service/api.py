from fastapi import Depends, FastAPI, Query
from fastapi.responses import HTMLResponse
from finance_app_database_service.models import Ticker, History
from sqlmodel import Session, SQLModel, create_engine, select
from finance_app_database_service.database import create_db_and_tables, populate_tickers_in_db, engine
from typing import List, Optional
from datetime import datetime

create_db_and_tables()
populate_tickers_in_db()

app = FastAPI()

def get_session():
    with Session(engine) as session:
        yield session

@app.get("/")
async def read_root():
    return {"Hello": "World"}

@app.get("/tickers", response_model=List[Ticker])
async def read_tickers(*, session: Session = Depends(get_session), offset: int = 0, limit: int = Query(default=100, lte=100)):
    tickers = session.exec(select(Ticker).offset(offset).limit(limit)).all()
    return tickers

# Sample Datetime Format: 2023-10-19T00:00:00-00:00
@app.get("/history")
async def get_history(*, session: Session = Depends(get_session), ticker_name: str, datetime: datetime = "2013-10-19T00:00:00-00:00"):
    history = session.exec(select(History).where(History.ticker_name == ticker_name).where(History.datetime >= datetime)).all()
    return history

@app.post("/history", status_code=201)
async def save_history(*, session: Session = Depends(get_session), history: History):
    session.add(history)
    session.commit()
    session.refresh(history)
    return hero

@app.post("/history/batch", status_code=201)
def save_history_batch(*, session: Session = Depends(get_session), history: List[History]):
    history_list = [hist.dict() for hist in history]
    session.bulk_insert_mappings(History, history_list)
    session.commit()
    return "success" 

