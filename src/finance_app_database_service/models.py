from typing import Optional
from datetime import datetime, date
from uuid import uuid4
from decimal import Decimal
from sqlalchemy import BigInteger, Column, Numeric
from sqlmodel import Field, SQLModel

class Ticker(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ticker: str
    name: str
    exchange: str
    last_updated: Optional[datetime] = None

class History(SQLModel, table=True):
    __tablename__ = "price_history"
    ticker_id: int = Field(foreign_key="ticker.id", primary_key=True)
    ts: date = Field(primary_key=True)
    open: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(), nullable=True))
    high: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(), nullable=True))
    low: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(), nullable=True))
    close: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(), nullable=True))
    volume: Optional[int] = Field(default=None, sa_column=Column(BigInteger(), nullable=True))


class ScraperRun(SQLModel, table=True):
    __tablename__ = "scraper_run"

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    run_key: str = Field(index=True, sa_column_kwargs={"unique": True})
    scheduled_for: date
    mode: str = Field(default="daily")
    shadow_mode: bool = Field(default=False)
    status: str = Field(default="running", index=True)
    queued: int = Field(default=0)
    processed: int = Field(default=0)
    failed: int = Field(default=0)
    dlq: int = Field(default=0)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    notes: Optional[str] = None
