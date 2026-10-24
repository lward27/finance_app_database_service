"""Immutable, content-bound operator approval receipts for market publication."""

import sqlalchemy as sa
from alembic import op

revision = "0004_market_publication"
down_revision = "0003_persistent_games"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "market_publication",
        sa.Column("release_id", sa.String, sa.ForeignKey("dataset_release.id"), primary_key=True),
        sa.Column("input_sha256", sa.String, nullable=False),
        sa.Column("candidate_sha256", sa.String, nullable=False),
        sa.Column("approval_sha256", sa.String, nullable=False),
        sa.Column("approval_json", sa.Text, nullable=False),
        sa.Column("published_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("approval_sha256", name="uq_market_publication_approval"),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("CREATE TRIGGER market_publication_immutable BEFORE UPDATE OR DELETE ON market_publication "
                   "FOR EACH ROW EXECUTE FUNCTION finance_guard_game_audit()")
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER market_publication_immutable_{operation.lower()} "
                       f"BEFORE {operation} ON market_publication "
                       "BEGIN SELECT RAISE(ABORT, 'Market publication receipts are immutable'); END")
    else:
        raise RuntimeError("Only PostgreSQL and test SQLite are supported")


def downgrade():
    raise RuntimeError("Retain publication approvals and releases; automatic downgrade is disabled")
