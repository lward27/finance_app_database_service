"""Adopt existing legacy/import tables, or create their frozen baseline on a new DB."""

import sqlalchemy as sa
from alembic import op

revision = "0001_legacy_market"
down_revision = None
branch_labels = depends_on = None


def upgrade():
    metadata = sa.MetaData()
    sa.Table("ticker", metadata,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("ticker", sa.String, nullable=False),
             sa.Column("name", sa.String, nullable=False),
             sa.Column("exchange", sa.String, nullable=False),
             sa.Column("last_updated", sa.DateTime))
    sa.Table("price_history", metadata,
             sa.Column("ticker_id", sa.Integer, sa.ForeignKey("ticker.id"), primary_key=True),
             sa.Column("ts", sa.Date, primary_key=True),
             *[sa.Column(name, sa.Numeric) for name in ("open", "high", "low", "close")],
             sa.Column("volume", sa.BigInteger))
    sa.Table("scraper_run", metadata,
             sa.Column("id", sa.String, primary_key=True),
             sa.Column("run_key", sa.String, nullable=False),
             sa.Column("scheduled_for", sa.Date, nullable=False),
             sa.Column("mode", sa.String, nullable=False),
             sa.Column("shadow_mode", sa.Boolean, nullable=False),
             sa.Column("status", sa.String, nullable=False),
             *[sa.Column(name, sa.Integer, nullable=False)
               for name in ("queued", "processed", "failed", "dlq")],
             sa.Column("started_at", sa.DateTime, nullable=False),
             sa.Column("updated_at", sa.DateTime, nullable=False),
             sa.Column("completed_at", sa.DateTime),
             sa.Column("notes", sa.String),
             sa.Index("ix_scraper_run_run_key", "run_key", unique=True),
             sa.Index("ix_scraper_run_status", "status"))
    sa.Table("market_backfill", metadata,
             sa.Column("name", sa.String, primary_key=True),
             sa.Column("config_json", sa.String, nullable=False),
             sa.Column("status", sa.String, nullable=False),
             sa.Column("created_at", sa.DateTime, nullable=False),
             sa.Column("updated_at", sa.DateTime, nullable=False))
    sa.Table("market_backfill_item", metadata,
             sa.Column("backfill_name", sa.String, sa.ForeignKey("market_backfill.name"),
                       primary_key=True),
             sa.Column("ticker_id", sa.Integer, primary_key=True),
             sa.Column("ticker", sa.String, nullable=False),
             sa.Column("status", sa.String, nullable=False),
             sa.Column("attempts", sa.Integer, nullable=False),
             sa.Column("failures", sa.Integer, nullable=False),
             sa.Column("error_code", sa.String),
             sa.Column("snapshot_id", sa.String),
             sa.Column("updated_at", sa.DateTime, nullable=False),
             sa.Index("ix_market_backfill_item_status", "status"))
    sa.Table("market_backfill_throttle", metadata,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("next_request_at", sa.DateTime, nullable=False),
             sa.Column("rate_limit_streak", sa.Integer, nullable=False))
    sa.Table("market_snapshot", metadata,
             sa.Column("id", sa.String, primary_key=True),
             sa.Column("ticker_id", sa.Integer, nullable=False),
             *[sa.Column(name, sa.String, nullable=False)
               for name in ("ticker", "source", "source_version")],
             sa.Column("fetched_at", sa.DateTime, nullable=False),
             *[sa.Column(name, sa.String, nullable=False)
               for name in ("price_basis", "dividend_basis", "exchange_timezone")],
             sa.Column("complete_through", sa.Date, nullable=False),
             sa.Column("source_last_date", sa.Date, nullable=False),
             sa.Column("row_count", sa.Integer, nullable=False),
             sa.Column("status", sa.String, nullable=False),
             sa.Column("qualification", sa.String, nullable=False),
             sa.Index("ix_market_snapshot_ticker_id", "ticker_id"),
             sa.Index("ix_market_snapshot_status", "status"))
    sa.Table("market_history", metadata,
             sa.Column("snapshot_id", sa.String, sa.ForeignKey("market_snapshot.id"),
                       primary_key=True),
             sa.Column("ts", sa.Date, primary_key=True),
             *[sa.Column(name, sa.Numeric)
               for name in ("open", "high", "low", "close", "adjusted_close")],
             sa.Column("volume", sa.BigInteger),
             *[sa.Column(name, sa.Numeric, nullable=False)
               for name in ("dividend", "split_ratio", "capital_gain", "subsequent_split_factor")])
    # Existing tables are intentionally not altered, truncated, or reseeded.
    metadata.create_all(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("Retain legacy/import data; automatic baseline downgrade is disabled")
