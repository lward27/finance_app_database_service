"""Persistent game checkpoints, append-only audit rows, and durable commands."""

from datetime import date, datetime
from datetime import date as Date
from decimal import Decimal
from typing import Optional

from sqlalchemy import CheckConstraint, Column, ForeignKeyConstraint, Index, Numeric, Text, UniqueConstraint, text
from sqlmodel import Field, SQLModel

from finance_app_database_service.foundation_models import opaque_id


class MarketRevision(SQLModel, table=True):
    __tablename__ = "qualified_market_revision"
    __table_args__ = (
        CheckConstraint("kind IN ('calendar', 'bars', 'actions', 'validation')", name="ck_revision_kind"),
    )
    id: str = Field(primary_key=True)
    kind: str
    body_json: str = Field(sa_column=Column(Text, nullable=False))


class CalendarBinding(SQLModel, table=True):
    __tablename__ = "qualified_calendar"
    calendar_id: str = Field(primary_key=True)
    version: str = Field(primary_key=True)
    revision_id: str = Field(foreign_key="qualified_market_revision.id")


class MarketPublication(SQLModel, table=True):
    __tablename__ = "market_publication"
    __table_args__ = (UniqueConstraint("approval_sha256", name="uq_market_publication_approval"),)
    release_id: str = Field(foreign_key="dataset_release.id", primary_key=True)
    input_sha256: str
    candidate_sha256: str
    approval_sha256: str
    approval_json: str = Field(sa_column=Column(Text, nullable=False))
    published_at: datetime


class Game(SQLModel, table=True):
    __tablename__ = "game"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_game_revision"),
        CheckConstraint("initial_cash > 0", name="ck_game_capital"),
        CheckConstraint("start_date <= cursor AND cursor <= end_date AND start_date < end_date",
                        name="ck_game_dates"),
        CheckConstraint("status IN ('ready', 'margin_call', 'completed', 'bankrupt')", name="ck_game_status"),
        Index("ix_game_owner_id", "owner_id", "id"),
    )
    id: str = Field(default_factory=lambda: opaque_id("gam"), primary_key=True)
    owner_id: str = Field(foreign_key="principal.id")
    name: str
    release_id: str = Field(foreign_key="dataset_release.id")
    ruleset_id: str = Field(foreign_key="ruleset.id")
    requested_start: date
    start_date: date
    end_date: date
    cursor: date
    initial_cash: Decimal = Field(sa_column=Column(Numeric(28, 8), nullable=False))
    status: str = "ready"
    archived: bool = False
    revision: int = 1
    state_json: str = Field(sa_column=Column(Text, nullable=False))
    state_sha256: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class GameOrder(SQLModel, table=True):
    __tablename__ = "game_order"
    __table_args__ = (UniqueConstraint("game_id", "order_id", name="uq_game_order_id"),)
    game_id: str = Field(foreign_key="game.id", primary_key=True)
    sequence: int = Field(primary_key=True)
    order_id: str
    status: str
    body_json: str = Field(sa_column=Column(Text, nullable=False))


class GameLedger(SQLModel, table=True):
    __tablename__ = "game_ledger"
    __table_args__ = (UniqueConstraint("game_id", "key", name="uq_game_ledger_key"),)
    game_id: str = Field(foreign_key="game.id", primary_key=True)
    sequence: int = Field(primary_key=True)
    key: str
    body_json: str = Field(sa_column=Column(Text, nullable=False))


class GamePosting(SQLModel, table=True):
    __tablename__ = "game_posting"
    __table_args__ = (
        ForeignKeyConstraint(
            ["game_id", "ledger_sequence"], ["game_ledger.game_id", "game_ledger.sequence"],
        ),
    )
    game_id: str = Field(primary_key=True)
    ledger_sequence: int = Field(primary_key=True)
    sequence: int = Field(primary_key=True)
    account: str
    amount: Decimal = Field(sa_column=Column(Numeric(28, 8), nullable=False))


class GameFill(SQLModel, table=True):
    __tablename__ = "game_fill"
    game_id: str = Field(foreign_key="game.id", primary_key=True)
    sequence: int = Field(primary_key=True)
    body_json: str = Field(sa_column=Column(Text, nullable=False))


class GameSnapshot(SQLModel, table=True):
    __tablename__ = "game_snapshot"
    game_id: str = Field(foreign_key="game.id", primary_key=True)
    date: Date = Field(primary_key=True)
    body_json: str = Field(sa_column=Column(Text, nullable=False))


class GameEvent(SQLModel, table=True):
    __tablename__ = "game_event"
    game_id: str = Field(foreign_key="game.id", primary_key=True)
    sequence: int = Field(primary_key=True)
    body_json: str = Field(sa_column=Column(Text, nullable=False))


class GameCommand(SQLModel, table=True):
    __tablename__ = "game_command"
    principal_id: str = Field(foreign_key="principal.id", primary_key=True)
    key_hash: str = Field(primary_key=True)
    request_sha256: str
    response_json: str = Field(sa_column=Column(Text, nullable=False))
    status_code: int
    etag: str
    location: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class AdvanceOperation(SQLModel, table=True):
    __tablename__ = "advance_operation"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'interrupted_margin', 'blocked_data', 'failed')",
            name="ck_advance_status",
        ),
        CheckConstraint("kind IN ('advance', 'margin_resolution')", name="ck_advance_kind"),
        CheckConstraint("attempts >= 0 AND game_revision >= 1 AND actual_cursor <= effective_target",
                        name="ck_advance_progress"),
        CheckConstraint(
            "(status = 'running' AND lease_token IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(status <> 'running' AND lease_token IS NULL AND lease_until IS NULL)",
            name="ck_advance_lease",
        ),
        Index("ix_advance_claim", "status", "lease_until", "created_at", "id"),
        Index("uq_advance_active_game", "game_id", unique=True,
              postgresql_where=text("status IN ('queued', 'running')"),
              sqlite_where=text("status IN ('queued', 'running')")),
    )
    id: str = Field(default_factory=lambda: opaque_id("adv"), primary_key=True)
    game_id: str = Field(foreign_key="game.id")
    kind: str = "advance"
    requested_target: date
    effective_target: date
    actual_cursor: date
    game_revision: int
    game_status: str
    liquidate_all: bool = False
    status: str = "queued"
    code: Optional[str] = None
    lease_token: Optional[str] = None
    lease_until: Optional[datetime] = None
    attempts: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
