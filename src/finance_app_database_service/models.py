from typing import Optional, List
from datetime import datetime, date
from decimal import Decimal
from sqlalchemy import BigInteger, Column, Numeric
from sqlmodel import Field, SQLModel, Session, create_engine

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
