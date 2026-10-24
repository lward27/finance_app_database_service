"""Owned checkpoints, append-only game audit data, commands, and worker leases."""

import sqlalchemy as sa
from alembic import op

revision = "0003_persistent_games"
down_revision = "0002_game_foundations"
branch_labels = depends_on = None


def upgrade():
    op.create_table("qualified_market_revision",
                    sa.Column("id", sa.String, primary_key=True),
                    sa.Column("kind", sa.String, nullable=False),
                    sa.Column("body_json", sa.Text, nullable=False),
                    sa.CheckConstraint("kind IN ('calendar', 'bars', 'actions', 'validation')",
                                       name="ck_revision_kind"))
    op.create_table("qualified_calendar",
                    sa.Column("calendar_id", sa.String, primary_key=True),
                    sa.Column("version", sa.String, primary_key=True),
                    sa.Column("revision_id", sa.String, sa.ForeignKey("qualified_market_revision.id"), nullable=False))
    op.create_table("game",
                    sa.Column("id", sa.String, primary_key=True),
                    sa.Column("owner_id", sa.String, sa.ForeignKey("principal.id"), nullable=False),
                    sa.Column("name", sa.String, nullable=False),
                    sa.Column("release_id", sa.String, sa.ForeignKey("dataset_release.id"), nullable=False),
                    sa.Column("ruleset_id", sa.String, sa.ForeignKey("ruleset.id"), nullable=False),
                    *[sa.Column(name, sa.Date, nullable=False)
                      for name in ("requested_start", "start_date", "end_date", "cursor")],
                    sa.Column("initial_cash", sa.Numeric(28, 8), nullable=False),
                    sa.Column("status", sa.String, nullable=False),
                    sa.Column("archived", sa.Boolean, nullable=False),
                    sa.Column("revision", sa.Integer, nullable=False),
                    sa.Column("state_json", sa.Text, nullable=False),
                    sa.Column("state_sha256", sa.String, nullable=False),
                    sa.Column("created_at", sa.DateTime, nullable=False),
                    sa.CheckConstraint("revision >= 1", name="ck_game_revision"),
                    sa.CheckConstraint("initial_cash > 0", name="ck_game_capital"),
                    sa.CheckConstraint("start_date <= cursor AND cursor <= end_date AND start_date < end_date",
                                       name="ck_game_dates"),
                    sa.CheckConstraint("status IN ('ready', 'margin_call', 'completed', 'bankrupt')",
                                       name="ck_game_status"))
    op.create_index("ix_game_owner_id", "game", ["owner_id", "id"])
    for table in ("game_order", "game_ledger", "game_fill", "game_event"):
        extra = []
        if table == "game_order":
            extra = [sa.Column("order_id", sa.String, nullable=False), sa.Column("status", sa.String, nullable=False),
                     sa.UniqueConstraint("game_id", "order_id", name="uq_game_order_id")]
        elif table == "game_ledger":
            extra = [sa.Column("key", sa.String, nullable=False),
                     sa.UniqueConstraint("game_id", "key", name="uq_game_ledger_key")]
        op.create_table(table,
                        sa.Column("game_id", sa.String, sa.ForeignKey("game.id"), primary_key=True),
                        sa.Column("sequence", sa.Integer, primary_key=True),
                        *extra, sa.Column("body_json", sa.Text, nullable=False))
    op.create_table("game_posting",
                    sa.Column("game_id", sa.String, primary_key=True),
                    sa.Column("ledger_sequence", sa.Integer, primary_key=True),
                    sa.Column("sequence", sa.Integer, primary_key=True),
                    sa.Column("account", sa.String, nullable=False),
                    sa.Column("amount", sa.Numeric(28, 8), nullable=False),
                    sa.ForeignKeyConstraint(["game_id", "ledger_sequence"],
                                            ["game_ledger.game_id", "game_ledger.sequence"]))
    op.create_table("game_snapshot",
                    sa.Column("game_id", sa.String, sa.ForeignKey("game.id"), primary_key=True),
                    sa.Column("date", sa.Date, primary_key=True),
                    sa.Column("body_json", sa.Text, nullable=False))
    op.create_table("game_command",
                    sa.Column("principal_id", sa.String, sa.ForeignKey("principal.id"), primary_key=True),
                    sa.Column("key_hash", sa.String, primary_key=True),
                    sa.Column("request_sha256", sa.String, nullable=False),
                    sa.Column("response_json", sa.Text, nullable=False),
                    sa.Column("status_code", sa.Integer, nullable=False),
                    sa.Column("etag", sa.String, nullable=False),
                    sa.Column("location", sa.String),
                    sa.Column("created_at", sa.DateTime, nullable=False))
    op.create_index("ix_game_command_created_at", "game_command", ["created_at"])
    op.create_table("advance_operation",
                    sa.Column("id", sa.String, primary_key=True),
                    sa.Column("game_id", sa.String, sa.ForeignKey("game.id"), nullable=False),
                    sa.Column("kind", sa.String, nullable=False),
                    sa.Column("requested_target", sa.Date, nullable=False),
                    sa.Column("effective_target", sa.Date, nullable=False),
                    sa.Column("actual_cursor", sa.Date, nullable=False),
                    sa.Column("game_revision", sa.Integer, nullable=False),
                    sa.Column("game_status", sa.String, nullable=False),
                    sa.Column("liquidate_all", sa.Boolean, nullable=False),
                    sa.Column("status", sa.String, nullable=False),
                    sa.Column("code", sa.String),
                    sa.Column("lease_token", sa.String),
                    sa.Column("lease_until", sa.DateTime),
                    sa.Column("attempts", sa.Integer, nullable=False),
                    sa.Column("created_at", sa.DateTime, nullable=False),
                    sa.Column("updated_at", sa.DateTime, nullable=False),
                    sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'interrupted_margin', 'blocked_data', 'failed')",
                                       name="ck_advance_status"),
                    sa.CheckConstraint("kind IN ('advance', 'margin_resolution')", name="ck_advance_kind"),
                    sa.CheckConstraint("attempts >= 0 AND game_revision >= 1 AND actual_cursor <= effective_target",
                                       name="ck_advance_progress"),
                    sa.CheckConstraint(
                        "(status = 'running' AND lease_token IS NOT NULL AND lease_until IS NOT NULL) OR "
                        "(status <> 'running' AND lease_token IS NULL AND lease_until IS NULL)",
                        name="ck_advance_lease"))
    op.create_index("ix_advance_claim", "advance_operation", ["status", "lease_until", "created_at", "id"])
    condition = sa.text("status IN ('queued', 'running')")
    op.create_index("uq_advance_active_game", "advance_operation", ["game_id"], unique=True,
                    postgresql_where=condition, sqlite_where=condition)
    install_guards()


def install_guards():
    immutable = ("qualified_market_revision", "qualified_calendar", "game_ledger", "game_posting",
                 "game_fill", "game_snapshot", "game_event", "game_command")
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("""
            CREATE FUNCTION finance_guard_game_audit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'Game audit and qualified inputs are immutable' USING ERRCODE = '55000';
            END;
            $$
        """)
        for table in immutable:
            op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                       "FOR EACH ROW EXECUTE FUNCTION finance_guard_game_audit()")
    elif dialect == "sqlite":
        for table in immutable:
            for operation in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER {table}_immutable_{operation.lower()} BEFORE {operation} ON {table} "
                           "BEGIN SELECT RAISE(ABORT, 'Game audit and qualified inputs are immutable'); END")
    else:
        raise RuntimeError("Only PostgreSQL and test SQLite are supported")


def downgrade():
    raise RuntimeError("Retain games, ledgers, and qualified inputs; automatic downgrade is disabled")
