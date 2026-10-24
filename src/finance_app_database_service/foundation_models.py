"""Game foundations, managed by Alembic rather than API startup."""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import CheckConstraint, Column, Index, String, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def opaque_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class Principal(SQLModel, table=True):
    __tablename__ = "principal"
    __table_args__ = (
        UniqueConstraint("oidc_issuer", "oidc_subject", name="uq_principal_oidc"),
        CheckConstraint(
            "(kind = 'guest' AND oidc_issuer IS NULL AND oidc_subject IS NULL) OR "
            "(kind = 'registered' AND oidc_issuer IS NOT NULL AND oidc_subject IS NOT NULL)",
            name="ck_principal_identity",
        ),
    )
    id: str = Field(default_factory=lambda: opaque_id("prn"), primary_key=True)
    kind: str = "guest"
    oidc_issuer: Optional[str] = None
    oidc_subject: Optional[str] = None
    sessions_revoked_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class GuestSession(SQLModel, table=True):
    """Legacy table name; sessions may now belong to guests or registered users."""
    __tablename__ = "guest_session"
    id: str = Field(default_factory=lambda: opaque_id("ses"), primary_key=True)
    principal_id: str = Field(foreign_key="principal.id", index=True)
    token_hash: str = Field(sa_column=Column(String(64), nullable=False, unique=True))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime = Field(index=True)
    revoked_at: Optional[datetime] = None
    authenticated_at: Optional[datetime] = None


class GuestCreationWindow(SQLModel, table=True):
    __tablename__ = "guest_creation_window"
    __table_args__ = (CheckConstraint("used >= 0", name="ck_guest_creation_used"),)
    key_hash: str = Field(primary_key=True)
    used: int = 0
    expires_at: datetime = Field(index=True)


class Instrument(SQLModel, table=True):
    __tablename__ = "instrument"
    __table_args__ = (Index("ix_instrument_symbol_id", "symbol", "id"),)
    id: str = Field(default_factory=lambda: opaque_id("ins"), primary_key=True)
    symbol: str
    name: str
    exchange: str = Field(index=True)
    currency: Optional[str] = None
    metadata_status: str = "unverified"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class InstrumentAlias(SQLModel, table=True):
    __tablename__ = "instrument_alias"
    instrument_id: str = Field(foreign_key="instrument.id", primary_key=True)
    symbol: str = Field(primary_key=True, index=True)
    source: str = Field(default="legacy_catalog", primary_key=True)


class LegacyTickerMap(SQLModel, table=True):
    __tablename__ = "legacy_ticker_map"
    ticker_id: int = Field(foreign_key="ticker.id", primary_key=True)
    instrument_id: str = Field(foreign_key="instrument.id", index=True)


class Ruleset(SQLModel, table=True):
    __tablename__ = "ruleset"
    id: str = Field(primary_key=True)
    definition_json: str = Field(sa_column=Column(Text, nullable=False))
    sha256: str = Field(sa_column=Column(String(64), nullable=False))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DatasetRelease(SQLModel, table=True):
    __tablename__ = "dataset_release"
    __table_args__ = (
        CheckConstraint(
            "qualification IN ('pending', 'simulation_qualified')",
            name="ck_dataset_release_qualification",
        ),
        Index("ix_dataset_release_publication", "qualification", "published_at", "id"),
    )
    id: str = Field(primary_key=True)
    manifest_json: str = Field(sa_column=Column(Text, nullable=False))
    sha256: str = Field(sa_column=Column(String(64), nullable=False))
    qualification: str = "pending"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    published_at: Optional[datetime] = None
