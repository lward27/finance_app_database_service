from fastapi import Depends, FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from finance_app_database_service.models import Ticker, History
from sqlmodel import Session, select
from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from finance_app_database_service.database import create_db_and_tables, populate_tickers_in_db, engine
from typing import List, Optional
from datetime import datetime, date


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def on_startup() -> None:
    create_db_and_tables()
    populate_tickers_in_db()

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

@app.get("/tickers/count")
async def count_tickers(*, session: Session = Depends(get_session)):
    count_value = session.exec(select(func.count(Ticker.id))).one()
    return count_value

@app.get("/tickers/update-status")
def get_tickers_update_status(*, session: Session = Depends(get_session)):
    rows = session.execute(
        text("""
            SELECT t.id, t.ticker,
                   COALESCE(MAX(p.ts), '1900-01-01') AS last_date
            FROM ticker t
            LEFT JOIN price_history p ON t.id = p.ticker_id
            GROUP BY t.id, t.ticker
        """)
    ).all()
    return [{"ticker_id": row.id, "ticker": row.ticker, "last_date": str(row.last_date)} for row in rows]

@app.post("/tickers", status_code=201)
async def save_ticker(*, session: Session = Depends(get_session), ticker: Ticker):
    session.add(ticker)
    session.commit()
    session.refresh(ticker)
    return ticker

@app.delete("/tickers/{ticker_id}")
def delete_ticker(ticker_id: int):
    with Session(engine) as session:
        ticker = session.get(Ticker, ticker_id)
        if not ticker:
            raise HTTPException(status_code=404, detail="Ticker not found")
        session.delete(ticker)
        session.commit()
        return {"ok": True}

@app.get("/history")
async def get_history(
    *,
    session: Session = Depends(get_session),
    ticker_name: str,
    from_date: Optional[date] = Query(default=None, description="Return history on/after this date"),
):
    ticker = session.exec(select(Ticker).where(Ticker.ticker == ticker_name)).first()
    if not ticker:
        raise HTTPException(status_code=404, detail="Ticker not found")
    query = select(History).where(History.ticker_id == ticker.id)
    if from_date is not None:
        query = query.where(History.ts >= from_date)
    history = session.exec(query).all()
    return history

@app.get("/history/last_date")
async def get_history_last_date(*, session: Session = Depends(get_session), ticker_name: str):
    ticker = session.exec(select(Ticker).where(Ticker.ticker == ticker_name)).first()
    if not ticker:
        return None
    history = session.exec(
        select(History).where(History.ticker_id == ticker.id).order_by(History.ts.desc())
    ).first()
    return history.ts if history else None

@app.post("/history", status_code=201)
async def save_history(*, session: Session = Depends(get_session), history: History):
    session.add(history)
    session.commit()
    session.refresh(history)
    return history

@app.post("/history/batch", status_code=201)
def save_history_batch(*, session: Session = Depends(get_session), history: List[History]):
    if not history:
        return "success"
    history_list = [hist.dict() for hist in history]
    stmt = pg_insert(History).values(history_list).on_conflict_do_nothing(
        index_elements=["ticker_id", "ts"]
    )
    session.execute(stmt)
    session.commit()
    return "success"

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
