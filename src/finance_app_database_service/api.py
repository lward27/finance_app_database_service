from datetime import datetime, date, timedelta
from typing import List, Optional

from fastapi import Depends, FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, select

from finance_app_database_service.database import (
    create_db_and_tables,
    populate_tickers_in_db,
    wait_for_database,
    engine,
)
from finance_app_database_service.models import Ticker, History, ScraperRun
from finance_app_database_service.ticker_normalization import (
    normalize_ticker,
    TickerValidationError,
)


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(TickerValidationError)
async def ticker_validation_exception_handler(request, exc: TickerValidationError):
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=422,
        content={"detail": "Invalid ticker symbol"},
    )


@app.on_event("startup")
def on_startup() -> None:
    wait_for_database()
    create_db_and_tables()
    populate_tickers_in_db()


def get_session():
    with Session(engine) as session:
        yield session


class ScraperRunStartRequest(BaseModel):
    run_key: str
    scheduled_for: date
    mode: str = "daily"
    shadow_mode: bool = False
    notes: Optional[str] = None


class ScraperRunProgressRequest(BaseModel):
    queued_delta: int = 0
    processed_delta: int = 0
    failed_delta: int = 0
    dlq_delta: int = 0


class ScraperRunCompleteRequest(BaseModel):
    status: str = "completed"


@app.get("/")
async def read_root():
    return {"Hello": "World"}


@app.get("/tickers", response_model=List[Ticker])
async def read_tickers(
    *,
    session: Session = Depends(get_session),
    offset: int = 0,
    limit: int = Query(default=100, lte=100),
):
    tickers = session.exec(select(Ticker).offset(offset).limit(limit)).all()
    return tickers


@app.get("/tickers/count")
async def count_tickers(*, session: Session = Depends(get_session)):
    count_value = session.exec(select(func.count(Ticker.id))).one()
    return count_value


def _fetch_ticker_status_rows(
    session: Session,
    target_date: date,
    offset: int,
    limit: int,
    stale_only: bool,
) -> list[dict]:
    rows = session.execute(
        text(
            """
            WITH latest AS (
                SELECT ticker_id, MAX(ts) AS last_date
                FROM price_history
                GROUP BY ticker_id
            )
            SELECT
                t.id,
                t.ticker,
                COALESCE(l.last_date, '1900-01-01'::date) AS last_date
            FROM ticker t
            LEFT JOIN latest l ON l.ticker_id = t.id
            WHERE (:stale_only = FALSE OR COALESCE(l.last_date, '1900-01-01'::date) < :target_date)
            ORDER BY t.ticker
            LIMIT :limit
            OFFSET :offset
            """
        ),
        {
            "target_date": target_date,
            "offset": offset,
            "limit": limit,
            "stale_only": stale_only,
        },
    ).all()

    return [
        {"ticker_id": row.id, "ticker": row.ticker, "last_date": str(row.last_date)}
        for row in rows
    ]


@app.get("/tickers/update-status")
def get_tickers_update_status(
    *,
    session: Session = Depends(get_session),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=5000),
    target_date: Optional[date] = Query(default=None),
    stale_only: bool = Query(default=False),
):
    comparison_date = target_date or date.today()
    return _fetch_ticker_status_rows(
        session=session,
        target_date=comparison_date,
        offset=offset,
        limit=limit,
        stale_only=stale_only,
    )


@app.get("/tickers/stale")
def get_stale_tickers(
    *,
    session: Session = Depends(get_session),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=5000),
    target_date: Optional[date] = Query(default=None),
):
    comparison_date = target_date or (date.today() - timedelta(days=1))
    return _fetch_ticker_status_rows(
        session=session,
        target_date=comparison_date,
        offset=offset,
        limit=limit,
        stale_only=True,
    )


@app.post("/tickers", status_code=201)
async def save_ticker(*, session: Session = Depends(get_session), ticker: Ticker):
    ticker.ticker = normalize_ticker(ticker.ticker)
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
    ticker_name = normalize_ticker(ticker_name)
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
    ticker_name = normalize_ticker(ticker_name)
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


@app.post("/scraper-runs/start", response_model=ScraperRun)
def start_scraper_run(
    *,
    session: Session = Depends(get_session),
    payload: ScraperRunStartRequest,
):
    existing = session.exec(
        select(ScraperRun).where(ScraperRun.run_key == payload.run_key)
    ).first()
    if existing:
        return existing

    now = datetime.utcnow()
    run = ScraperRun(
        run_key=payload.run_key,
        scheduled_for=payload.scheduled_for,
        mode=payload.mode,
        shadow_mode=payload.shadow_mode,
        status="running",
        started_at=now,
        updated_at=now,
        notes=payload.notes,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


@app.post("/scraper-runs/{run_id}/progress", response_model=ScraperRun)
def update_scraper_run_progress(
    run_id: str,
    *,
    session: Session = Depends(get_session),
    payload: ScraperRunProgressRequest,
):
    run = session.get(ScraperRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    run.queued = max(0, run.queued + payload.queued_delta)
    run.processed = max(0, run.processed + payload.processed_delta)
    run.failed = max(0, run.failed + payload.failed_delta)
    run.dlq = max(0, run.dlq + payload.dlq_delta)
    run.updated_at = datetime.utcnow()

    session.add(run)
    session.commit()
    session.refresh(run)
    return run


@app.post("/scraper-runs/{run_id}/complete", response_model=ScraperRun)
def complete_scraper_run(
    run_id: str,
    *,
    session: Session = Depends(get_session),
    payload: ScraperRunCompleteRequest,
):
    run = session.get(ScraperRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    now = datetime.utcnow()
    run.status = payload.status
    run.completed_at = now
    run.updated_at = now

    session.add(run)
    session.commit()
    session.refresh(run)
    return run


@app.get("/scraper-runs/{run_id}", response_model=ScraperRun)
def get_scraper_run(run_id: str, *, session: Session = Depends(get_session)):
    run = session.get(ScraperRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/scraper-runs", response_model=List[ScraperRun])
def list_scraper_runs(
    *,
    session: Session = Depends(get_session),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
):
    return session.exec(
        select(ScraperRun)
        .order_by(ScraperRun.started_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
