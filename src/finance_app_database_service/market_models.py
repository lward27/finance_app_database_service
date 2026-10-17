"""Additive ingestion tables; never overwrite the legacy adjusted price series."""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, Column, Numeric
from sqlmodel import Field, SQLModel


class MarketBackfill(SQLModel, table=True):
    __tablename__ = "market_backfill"
    name: str = Field(primary_key=True)
    config_json: str
    status: str = "running"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class MarketBackfillItem(SQLModel, table=True):
    __tablename__ = "market_backfill_item"
    backfill_name: str = Field(foreign_key="market_backfill.name", primary_key=True)
    ticker_id: int = Field(primary_key=True)
    ticker: str
    status: str = Field(default="pending", index=True)
    attempts: int = 0
    failures: int = 0
    error_code: Optional[str] = None
    snapshot_id: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class MarketBackfillThrottle(SQLModel, table=True):
    __tablename__ = "market_backfill_throttle"
    id: int = Field(default=1, primary_key=True)
    next_request_at: datetime = Field(default_factory=datetime.utcnow)
    rate_limit_streak: int = 0


class MarketSnapshot(SQLModel, table=True):
    __tablename__ = "market_snapshot"
    id: str = Field(primary_key=True)
    ticker_id: int = Field(index=True)
    ticker: str
    source: str = "yahoo_finance"
    source_version: str
    fetched_at: datetime
    price_basis: str = "yahoo_split_adjusted"
    dividend_basis: str = "yahoo_split_adjusted"
    exchange_timezone: str
    complete_through: date
    source_last_date: date
    row_count: int
    status: str = Field(default="loading", index=True)
    qualification: str = "source_only_not_simulation_qualified"


class MarketHistory(SQLModel, table=True):
    __tablename__ = "market_history"
    snapshot_id: str = Field(foreign_key="market_snapshot.id", primary_key=True)
    ts: date = Field(primary_key=True)
    open: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(), nullable=True))
    high: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(), nullable=True))
    low: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(), nullable=True))
    close: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(), nullable=True))
    adjusted_close: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(), nullable=True))
    volume: Optional[int] = Field(default=None, sa_column=Column(BigInteger(), nullable=True))
    dividend: Decimal = Field(sa_column=Column(Numeric(), nullable=False))
    split_ratio: Decimal = Field(sa_column=Column(Numeric(), nullable=False))
    capital_gain: Decimal = Field(sa_column=Column(Numeric(), nullable=False))
    subsequent_split_factor: Decimal = Field(sa_column=Column(Numeric(), nullable=False))
