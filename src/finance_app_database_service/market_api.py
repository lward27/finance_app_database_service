"""Read-only access to completed source snapshots and durable backfill progress."""

from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from finance_app_database_service.backfill import progress
from finance_app_database_service.database import engine
from finance_app_database_service.market_models import (
    MarketBackfill, MarketBackfillItem, MarketBackfillThrottle, MarketHistory, MarketSnapshot,
)

router = APIRouter(prefix="/market-data")


def get_market_session():
    with Session(engine) as session:
        yield session


@router.get("/backfills/{name}")
def get_backfill(name: str, session: Session = Depends(get_market_session)):
    job = session.get(MarketBackfill, name)
    if job is None:
        raise HTTPException(404, "Backfill not found")
    gate = session.get(MarketBackfillThrottle, 1)
    return {
        "name": name, "status": job.status, "counts": progress(session, name),
        "created_at": job.created_at, "updated_at": job.updated_at,
        "next_request_at": gate.next_request_at if gate else None,
    }


@router.get("/tickers/{ticker_id}/history")
def get_market_history(
    ticker_id: int,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    after: Optional[date] = None,
    snapshot_id: Optional[str] = None,
    limit: int = Query(default=250, ge=1, le=1000),
    session: Session = Depends(get_market_session),
):
    if from_date and to_date and from_date > to_date:
        raise HTTPException(422, "Invalid date range")
    query = select(MarketSnapshot).where(
        MarketSnapshot.ticker_id == ticker_id, MarketSnapshot.status == "complete"
    )
    if snapshot_id is not None:
        query = query.where(MarketSnapshot.id == snapshot_id)
    snapshot = session.exec(query.order_by(MarketSnapshot.fetched_at.desc()).limit(1)).first()
    if snapshot is None:
        raise HTTPException(404, "No completed source snapshot")
    query = select(MarketHistory).where(
        MarketHistory.snapshot_id == snapshot.id,
        MarketHistory.ts <= snapshot.complete_through,
    )
    if from_date:
        query = query.where(MarketHistory.ts >= from_date)
    if to_date:
        query = query.where(MarketHistory.ts <= to_date)
    if after:
        query = query.where(MarketHistory.ts > after)
    rows = session.exec(query.order_by(MarketHistory.ts).limit(limit + 1)).all()
    more = len(rows) > limit
    data = {
        "snapshot": snapshot.dict(),
        "items": [row.dict(exclude={"snapshot_id"}) for row in rows[:limit]],
        "next_after": rows[limit - 1].ts if more else None,
    }
    return JSONResponse(jsonable_encoder(data, custom_encoder={Decimal: str}))


@router.get("/backfills/{name}/items")
def get_backfill_items(
    name: str,
    status: Optional[str] = None,
    after_ticker_id: int = 0,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_market_session),
):
    if session.get(MarketBackfill, name) is None:
        raise HTTPException(404, "Backfill not found")
    query = select(MarketBackfillItem).where(
        MarketBackfillItem.backfill_name == name,
        MarketBackfillItem.ticker_id > after_ticker_id,
    )
    if status is not None:
        if status not in ("pending", "fetching", "retry", "complete", "no_data", "failed"):
            raise HTTPException(422, "Invalid item status")
        query = query.where(MarketBackfillItem.status == status)
    items = session.exec(query.order_by(MarketBackfillItem.ticker_id).limit(limit + 1)).all()
    return {
        "items": items[:limit],
        "next_after_ticker_id": items[limit - 1].ticker_id if len(items) > limit else None,
    }
