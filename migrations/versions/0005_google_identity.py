"""Google login, generic browser sessions, guest claims, and identity audit."""

import sqlalchemy as sa
from alembic import op

revision = "0005_google_identity"
down_revision = "0004_market_publication"
branch_labels = depends_on = None


def upgrade():
    op.add_column("guest_session", sa.Column("authenticated_at", sa.DateTime))
    op.add_column("principal", sa.Column("sessions_revoked_at", sa.DateTime))
    op.create_table(
        "oidc_authentication",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("browser_hash", sa.String(64), nullable=False, unique=True),
        *[sa.Column(name, sa.String, nullable=False) for name in ("context_sha256", "intent", "status")],
        sa.Column("source_session_id", sa.String, sa.ForeignKey("guest_session.id")),
        sa.Column("source_token_hash", sa.String),
        sa.Column("source_principal_id", sa.String, sa.ForeignKey("principal.id")),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("consumed_at", sa.DateTime),
        sa.Column("verified_at", sa.DateTime),
        sa.Column("issuer", sa.String),
        sa.Column("subject", sa.String),
        sa.Column("completed_session_id", sa.String, sa.ForeignKey("guest_session.id")),
        sa.Column("failure_code", sa.String),
        sa.CheckConstraint("intent IN ('sign_in', 'guest_claim', 'reauthenticate')", name="ck_oidc_intent"),
        sa.CheckConstraint("status IN ('pending', 'exchanging', 'verified', 'completed', 'failed')",
                           name="ck_oidc_status"),
        sa.CheckConstraint("expires_at > created_at", name="ck_oidc_expiry"),
        sa.CheckConstraint("status NOT IN ('verified', 'completed') OR "
                           "(issuer IS NOT NULL AND subject IS NOT NULL AND verified_at IS NOT NULL)",
                           name="ck_oidc_verified"),
        sa.CheckConstraint("status <> 'completed' OR completed_session_id IS NOT NULL", name="ck_oidc_completed"),
    )
    op.create_index("ix_oidc_expiry", "oidc_authentication", ["expires_at", "status"])
    op.create_table(
        "guest_claim",
        sa.Column("guest_principal_id", sa.String, sa.ForeignKey("principal.id"), primary_key=True),
        sa.Column("id", sa.String, nullable=False),
        sa.Column("account_principal_id", sa.String, sa.ForeignKey("principal.id"), nullable=False),
        sa.Column("source_session_id", sa.String, sa.ForeignKey("guest_session.id"), nullable=False),
        sa.Column("account_session_id", sa.String, sa.ForeignKey("guest_session.id"), nullable=False),
        sa.Column("authentication_id", sa.String, sa.ForeignKey("oidc_authentication.id"), nullable=False),
        sa.Column("games_json", sa.Text, nullable=False),
        sa.Column("games_sha256", sa.String, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("id", name="uq_guest_claim_id"),
        sa.UniqueConstraint("authentication_id", name="uq_guest_claim_authentication"),
        sa.CheckConstraint("guest_principal_id <> account_principal_id", name="ck_guest_claim_distinct"),
    )
    op.create_table(
        "identity_audit",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("principal_id", sa.String, sa.ForeignKey("principal.id"), nullable=False),
        sa.Column("session_id", sa.String, sa.ForeignKey("guest_session.id"), nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("target_session_id", sa.String, sa.ForeignKey("guest_session.id")),
        sa.Column("authentication_id", sa.String, sa.ForeignKey("oidc_authentication.id")),
        sa.Column("affected_sessions", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.CheckConstraint("kind IN ('sign_in', 'guest_claim', 'sign_out', 'session_revoked', 'sessions_revoked')",
                           name="ck_identity_audit_kind"),
        sa.CheckConstraint("affected_sessions >= 0", name="ck_identity_audit_count"),
    )
    op.create_index("ix_identity_audit_principal", "identity_audit", ["principal_id", "id"])
    op.create_table(
        "identity_rate_window",
        sa.Column("key_hash", sa.String, primary_key=True),
        sa.Column("used", sa.Integer, nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.CheckConstraint("used >= 0", name="ck_identity_rate_used"),
    )
    op.create_index("ix_identity_rate_window_expires_at", "identity_rate_window", ["expires_at"])
    dialect = op.get_bind().dialect.name
    for table in ("guest_claim", "identity_audit"):
        if dialect == "postgresql":
            op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                       "FOR EACH ROW EXECUTE FUNCTION finance_guard_game_audit()")
        elif dialect == "sqlite":
            for operation in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER {table}_immutable_{operation.lower()} BEFORE {operation} ON {table} "
                           "BEGIN SELECT RAISE(ABORT, 'Identity audit is immutable'); END")
        else:
            raise RuntimeError("Only PostgreSQL and test SQLite are supported")


def downgrade():
    raise RuntimeError("Retain identities, sessions, and ownership audit; automatic downgrade is disabled")
