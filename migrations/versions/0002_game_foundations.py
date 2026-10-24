"""Identity, instrument mappings, immutable rules, and qualified-release contracts."""

import hashlib
import json
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "0002_game_foundations"
down_revision = "0001_legacy_market"
branch_labels = depends_on = None


# Frozen migration input. A rules change needs a new ID and migration.
RULES_V1 = {
    "id": "us-equity-margin-v1", "currency": "USD",
    "execution": "next_session_open", "order_type": "market",
    "time_in_force": "next_session", "fill_policy": "all_or_reject",
    "long_positions": True, "short_positions": True, "fractional_shares": True,
    "initial_margin_rate": "0.50", "maintenance_margin_rate": "0.30",
    "annual_margin_interest_rate": "0.08", "annual_short_borrow_rate": "0.03",
    "accrual": "actual_calendar_days/365",
    "short_proceeds": "restricted_by_fifo_lot", "cash_interest_rate": "0",
    "commission": "0", "slippage": "0", "settlement": "immediate_simulated",
    "lot_matching": "FIFO", "money_decimal_places": 8, "quantity_decimal_places": 8,
    "money_rounding": "ROUND_HALF_EVEN", "notional_quantity_rounding": "ROUND_DOWN",
    "margin_call": "pause_at_first_breaching_close",
    "dividends": "prior_close_entitlement_booked_on_ex_date",
    "additional_contributions": False, "max_advance_calendar_years": 10,
    "disclosures": [
        "Educational fixed rules, not Regulation T or a historical broker simulation.",
        "Shortability is supplied by validated releases; historical locates and recalls are not simulated.",
        "Short proceeds do not offset the debit balance used to calculate borrowing costs.",
        "Dividends are booked on the ex-date, not the historical payment date.",
        "Commissions, slippage, and cash interest are zero; settlement is immediate.",
        "Initial funding is the only contribution; bankruptcy creates no real-world debt.",
        "Legacy market exploration remains available, so historical play is not cheat-proof.",
    ],
}


def upgrade():
    op.create_table("principal",
                    sa.Column("id", sa.String, primary_key=True),
                    sa.Column("kind", sa.String, nullable=False),
                    sa.Column("oidc_issuer", sa.String),
                    sa.Column("oidc_subject", sa.String),
                    sa.Column("created_at", sa.DateTime, nullable=False),
                    sa.UniqueConstraint("oidc_issuer", "oidc_subject", name="uq_principal_oidc"),
                    sa.CheckConstraint(
                        "(kind = 'guest' AND oidc_issuer IS NULL AND oidc_subject IS NULL) OR "
                        "(kind = 'registered' AND oidc_issuer IS NOT NULL AND oidc_subject IS NOT NULL)",
                        name="ck_principal_identity"))
    op.create_table("guest_session",
                    sa.Column("id", sa.String, primary_key=True),
                    sa.Column("principal_id", sa.String, sa.ForeignKey("principal.id"), nullable=False),
                    sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
                    sa.Column("created_at", sa.DateTime, nullable=False),
                    sa.Column("expires_at", sa.DateTime, nullable=False),
                    sa.Column("revoked_at", sa.DateTime))
    op.create_index("ix_guest_session_principal_id", "guest_session", ["principal_id"])
    op.create_index("ix_guest_session_expires_at", "guest_session", ["expires_at"])
    op.create_table("guest_creation_window",
                    sa.Column("key_hash", sa.String, primary_key=True),
                    sa.Column("used", sa.Integer, nullable=False),
                    sa.Column("expires_at", sa.DateTime, nullable=False),
                    sa.CheckConstraint("used >= 0", name="ck_guest_creation_used"))
    op.create_index("ix_guest_creation_window_expires_at", "guest_creation_window", ["expires_at"])
    op.create_table("instrument",
                    sa.Column("id", sa.String, primary_key=True),
                    *[sa.Column(name, sa.String, nullable=False) for name in ("symbol", "name", "exchange")],
                    sa.Column("currency", sa.String),
                    sa.Column("metadata_status", sa.String, nullable=False),
                    sa.Column("created_at", sa.DateTime, nullable=False))
    op.create_index("ix_instrument_symbol_id", "instrument", ["symbol", "id"])
    op.create_index("ix_instrument_exchange", "instrument", ["exchange"])
    op.create_table("instrument_alias",
                    sa.Column("instrument_id", sa.String, sa.ForeignKey("instrument.id"), primary_key=True),
                    sa.Column("symbol", sa.String, primary_key=True),
                    sa.Column("source", sa.String, primary_key=True))
    op.create_index("ix_instrument_alias_symbol", "instrument_alias", ["symbol"])
    op.create_table("legacy_ticker_map",
                    sa.Column("ticker_id", sa.Integer, sa.ForeignKey("ticker.id"), primary_key=True),
                    sa.Column("instrument_id", sa.String, sa.ForeignKey("instrument.id"), nullable=False))
    op.create_index("ix_legacy_ticker_map_instrument_id", "legacy_ticker_map", ["instrument_id"])
    ruleset = op.create_table("ruleset",
                             sa.Column("id", sa.String, primary_key=True),
                             sa.Column("definition_json", sa.Text, nullable=False),
                             sa.Column("sha256", sa.String(64), nullable=False),
                             sa.Column("created_at", sa.DateTime, nullable=False))
    op.create_table("dataset_release",
                    sa.Column("id", sa.String, primary_key=True),
                    sa.Column("manifest_json", sa.Text, nullable=False),
                    sa.Column("sha256", sa.String(64), nullable=False),
                    sa.Column("qualification", sa.String, nullable=False),
                    sa.Column("created_at", sa.DateTime, nullable=False),
                    sa.Column("published_at", sa.DateTime),
                    sa.CheckConstraint(
                        "qualification IN ('pending', 'simulation_qualified')",
                        name="ck_dataset_release_qualification"))
    op.create_index("ix_dataset_release_publication", "dataset_release",
                    ["qualification", "published_at", "id"])
    canonical = json.dumps(RULES_V1, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    op.bulk_insert(ruleset, [{
        "id": RULES_V1["id"], "definition_json": canonical,
        "sha256": hashlib.sha256(canonical.encode()).hexdigest(), "created_at": datetime.utcnow(),
    }])
    install_immutability_guards()


def install_immutability_guards():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION finance_guard_ruleset() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'Rulesets are immutable' USING ERRCODE = '55000';
            END;
            $$
        """)
        op.execute("""
            CREATE TRIGGER ruleset_immutable BEFORE UPDATE OR DELETE ON ruleset
            FOR EACH ROW EXECUTE FUNCTION finance_guard_ruleset()
        """)
        op.execute("""
            CREATE FUNCTION finance_guard_published_release() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.published_at IS NOT NULL THEN
                    RAISE EXCEPTION 'Published releases are immutable' USING ERRCODE = '55000';
                END IF;
                IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
            END;
            $$
        """)
        op.execute("""
            CREATE TRIGGER published_release_immutable BEFORE UPDATE OR DELETE ON dataset_release
            FOR EACH ROW EXECUTE FUNCTION finance_guard_published_release()
        """)
    elif op.get_bind().dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(f"""
                CREATE TRIGGER ruleset_immutable_{operation.lower()}
                BEFORE {operation} ON ruleset BEGIN
                    SELECT RAISE(ABORT, 'Rulesets are immutable');
                END
            """)
            op.execute(f"""
                CREATE TRIGGER published_release_immutable_{operation.lower()}
                BEFORE {operation} ON dataset_release WHEN OLD.published_at IS NOT NULL BEGIN
                    SELECT RAISE(ABORT, 'Published releases are immutable');
                END
            """)
    else:
        raise RuntimeError("Only PostgreSQL and test SQLite are supported")


def downgrade():
    raise RuntimeError("Retain identity and market releases; automatic downgrade is disabled")
