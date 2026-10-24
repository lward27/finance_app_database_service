"""Isolated SQLite fixtures; never connect tests to the application's PG engine."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

from finance_app_database_service.api import create_app, get_session
from finance_app_database_service.catalog import bootstrap_catalog
from finance_app_database_service.contracts import ReleaseManifest, canonical_json, content_digest
from finance_app_database_service.foundation_models import DatasetRelease
from finance_app_database_service.market_api import get_market_session
from finance_app_database_service.models import Ticker
from finance_app_database_service.security import get_v1_session
from finance_app_database_service.settings import SecuritySettings

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://frontend.test"


def settings(**overrides):
    values = dict(
        allowed_origins=(ORIGIN,),
        service_token="test-only-service-credential-not-for-production",
        guest_rate_limit_key="test-only-rate-limit-key-not-for-production",
    )
    values.update(overrides)
    return SecuritySettings(**values)


def migrate(engine, revision="head"):
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def sqlite_engine(url="sqlite://", **kwargs):
    if url == "sqlite://":
        kwargs["poolclass"] = StaticPool
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 20}, **kwargs)

    @event.listens_for(engine, "connect")
    def configure(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    return engine


def test_app(engine, config=None):
    app = create_app(config or settings())
    app.router.on_startup.clear()

    def session():
        with Session(engine) as db:
            yield db

    for dependency in (get_session, get_market_session, get_v1_session):
        app.dependency_overrides[dependency] = session
    return app


def client(app):
    # Intentionally no lifespan context: tests never run production startup/bootstrap.
    return TestClient(app, base_url="https://api.test")


def seed_catalog(engine):
    with Session(engine) as db:
        db.add_all([
            Ticker(id=1, ticker="AAPL", name="Apple", exchange="NASDAQ"),
            Ticker(id=2, ticker="AAPL", name="Duplicate legacy Apple", exchange="NASDAQ"),
            Ticker(id=3, ticker="BRK-B", name="Berkshire", exchange="NYSE"),
        ])
        db.commit()
        bootstrap_catalog(db, execute=True, batch_size=2)


def manifest(instrument_id, **overrides):
    data = {
        "schema_version": 1, "qualification": "simulation_qualified",
        "currency": "USD", "price_basis": "as_traded",
        "calendar_id": "XNYS", "calendar_version": "test-calendar-v1",
        "from_date": "2020-01-02", "to_date": "2020-01-03",
        "instruments": [{
            "instrument_id": instrument_id, "from_date": "2020-01-02", "to_date": "2020-01-03",
            "bar_revision_id": "a" * 64, "action_revision_id": "b" * 64,
            "source_snapshot_ids": ["c" * 64], "validation_report_sha256": "d" * 64,
            "shortable": True,
        }],
    }
    data.update(overrides)
    return ReleaseManifest.parse_obj(data)


def release_row(value, **overrides):
    canonical = canonical_json(value)
    digest = content_digest(canonical)
    data = dict(
        id="rel_" + digest, manifest_json=canonical, sha256=digest,
        qualification="simulation_qualified",
    )
    data.update(overrides)
    return DatasetRelease(**data)
