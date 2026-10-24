"""Browser-bound login state and immutable identity/ownership audit records."""

from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, Column, Index, String, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from finance_app_database_service.foundation_models import opaque_id


class OIDCAuthentication(SQLModel, table=True):
    __tablename__ = "oidc_authentication"
    __table_args__ = (
        CheckConstraint("intent IN ('sign_in', 'guest_claim', 'reauthenticate')", name="ck_oidc_intent"),
        CheckConstraint("status IN ('pending', 'exchanging', 'verified', 'completed', 'failed')",
                        name="ck_oidc_status"),
        CheckConstraint("expires_at > created_at", name="ck_oidc_expiry"),
        CheckConstraint("status NOT IN ('verified', 'completed') OR "
                        "(issuer IS NOT NULL AND subject IS NOT NULL AND verified_at IS NOT NULL)",
                        name="ck_oidc_verified"),
        CheckConstraint("status <> 'completed' OR completed_session_id IS NOT NULL", name="ck_oidc_completed"),
        Index("ix_oidc_expiry", "expires_at", "status"),
    )
    id: str = Field(default_factory=lambda: opaque_id("oid"), primary_key=True)
    state_hash: str = Field(sa_column=Column(String(64), nullable=False, unique=True))
    browser_hash: str = Field(sa_column=Column(String(64), nullable=False, unique=True))
    context_sha256: str
    source_session_id: Optional[str] = Field(default=None, foreign_key="guest_session.id")
    source_token_hash: Optional[str] = None
    source_principal_id: Optional[str] = Field(default=None, foreign_key="principal.id")
    intent: str
    status: str = "pending"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    consumed_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    issuer: Optional[str] = None
    subject: Optional[str] = None
    completed_session_id: Optional[str] = Field(default=None, foreign_key="guest_session.id")
    failure_code: Optional[str] = None


class GuestClaim(SQLModel, table=True):
    __tablename__ = "guest_claim"
    __table_args__ = (
        UniqueConstraint("id", name="uq_guest_claim_id"),
        UniqueConstraint("authentication_id", name="uq_guest_claim_authentication"),
        CheckConstraint("guest_principal_id <> account_principal_id", name="ck_guest_claim_distinct"),
    )
    guest_principal_id: str = Field(foreign_key="principal.id", primary_key=True)
    id: str = Field(default_factory=lambda: opaque_id("clm"))
    account_principal_id: str = Field(foreign_key="principal.id")
    source_session_id: str = Field(foreign_key="guest_session.id")
    account_session_id: str = Field(foreign_key="guest_session.id")
    authentication_id: str = Field(foreign_key="oidc_authentication.id")
    games_json: str = Field(sa_column=Column(Text, nullable=False))
    games_sha256: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class IdentityAudit(SQLModel, table=True):
    __tablename__ = "identity_audit"
    __table_args__ = (
        CheckConstraint("kind IN ('sign_in', 'guest_claim', 'sign_out', 'session_revoked', 'sessions_revoked')",
                        name="ck_identity_audit_kind"),
        CheckConstraint("affected_sessions >= 0", name="ck_identity_audit_count"),
        Index("ix_identity_audit_principal", "principal_id", "id"),
    )
    id: str = Field(default_factory=lambda: opaque_id("iae"), primary_key=True)
    principal_id: str = Field(foreign_key="principal.id")
    session_id: str = Field(foreign_key="guest_session.id")
    kind: str
    target_session_id: Optional[str] = Field(default=None, foreign_key="guest_session.id")
    authentication_id: Optional[str] = Field(default=None, foreign_key="oidc_authentication.id")
    affected_sessions: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)


class IdentityRateWindow(SQLModel, table=True):
    __tablename__ = "identity_rate_window"
    __table_args__ = (CheckConstraint("used >= 0", name="ck_identity_rate_used"),)
    key_hash: str = Field(primary_key=True)
    used: int = 0
    expires_at: datetime = Field(index=True)
