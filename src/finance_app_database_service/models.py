from typing import Optional, List
from datetime import datetime
from sqlmodel import Field, SQLModel, Session, create_engine, Relationship

class Ticker(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ticker: str
    name: str
    exchange: str

class History(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    price_type: str
    datetime: datetime
    price: float
    ticker_name: str = Field(index=True)
