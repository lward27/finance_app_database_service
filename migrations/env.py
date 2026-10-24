"""Use application PG configuration or an explicitly supplied test connection."""

from alembic import context
from sqlalchemy import text
from sqlmodel import SQLModel

from finance_app_database_service import foundation_models, game_models, identity_models, market_models, models
from finance_app_database_service.database import engine

config = context.config


def migrate(connection):
    with connection.begin():
        if connection.dialect.name == "postgresql":
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            # Independent of the long-lived Yahoo backfill singleton lock.
            connection.execute(text("SELECT pg_advisory_xact_lock(726649232)"))
        context.configure(connection=connection, target_metadata=SQLModel.metadata,
                          compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Online migrations are required to adopt existing legacy tables safely")
elif config.attributes.get("connection") is not None:
    migrate(config.attributes["connection"])
else:
    with engine.connect() as connection:
        migrate(connection)
