"""Atomic operator-only publication. No HTTP route, migration, or game flag write."""

import re
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session, select

from finance_app_database_service.catalog import insert_missing
from finance_app_database_service.contracts import content_digest, canonical_json
from finance_app_database_service.foundation_models import DatasetRelease, Instrument
from finance_app_database_service.game_market import load_market
from finance_app_database_service.game_models import CalendarBinding, MarketPublication, MarketRevision
from finance_app_database_service.market_publication import build_plan, check_review_window, require, utcnow
from finance_app_database_service.problems import Problem

PUBLICATION_LOCK = 726649233


def target_identity(engine, schema=None):
    """Expose only explicit, non-credential URL identity; reject query overrides."""
    url = engine.url
    dialect = url.get_backend_name()
    require(not url.query, "UNSUPPORTED_DATABASE_TARGET")
    if dialect == "postgresql":
        require(bool(url.host and url.database and url.username), "DATABASE_TARGET_NOT_EXPLICIT")
        schema = "public" if schema is None else schema
        require(isinstance(schema, str) and re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema) is not None,
                "INVALID_DATABASE_SCHEMA")
        identity = dict(driver=url.drivername, host=url.host, port=url.port or 5432,
                        database=url.database, username=url.username, schema=schema)
    elif dialect == "sqlite":
        require(schema in (None, "main"), "INVALID_DATABASE_SCHEMA")
        identity = dict(driver=url.drivername,
                        database=str(Path(url.database).resolve()) if url.database and url.database != ":memory:"
                        else ":memory:", schema="main")
    else:
        require(False, "UNSUPPORTED_DATABASE_TARGET")
    return identity


def target_sha256(engine, schema=None):
    return content_digest(canonical_json(target_identity(engine, schema)))


def validate_published_market(session, release_id):
    try:
        load_market(session, release_id)
    except Problem:
        require(False, "PUBLISHED_INPUT_INTEGRITY_ERROR")


def publish(engine, input_value, candidate, approval, key, expected_target, *, expected_schema=None, clock=None):
    identity = target_identity(engine, expected_schema)
    require(content_digest(canonical_json(identity)) == expected_target, "DATABASE_TARGET_MISMATCH")
    clock = clock or utcnow
    plan = build_plan(input_value, candidate, approval, key, now=clock())
    with Session(engine) as session, session.begin():
        dialect = engine.dialect.name
        if dialect == "postgresql":
            # Qualify ORM/Core tables too: pg_temp can otherwise shadow search_path.
            session.connection(execution_options={"schema_translate_map": {None: identity["schema"]}})
            actual = session.execute(text("SELECT current_database(), session_user, current_user, current_schema()")).one()
            require(tuple(actual[:3]) == (identity["database"], identity["username"], identity["username"]),
                    "DATABASE_IDENTITY_MISMATCH")
            require(actual[3] == identity["schema"], "DATABASE_SCHEMA_MISMATCH")
            session.execute(text(f'SET LOCAL search_path TO "{identity["schema"]}", pg_catalog, pg_temp'))
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            session.execute(text("SELECT pg_catalog.pg_advisory_xact_lock(:lock)"), {"lock": PUBLICATION_LOCK})
        else:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        now = clock()
        check_review_window(plan.reviewed_at, plan.expires_at, now)
        existing = session.get(DatasetRelease, plan.release_id)
        if existing is not None:
            receipt = session.get(MarketPublication, plan.release_id)
            require(existing.manifest_json == plan.manifest_json
                    and existing.sha256 == plan.release_id[4:]
                    and existing.qualification == "simulation_qualified" and existing.published_at is not None
                    and receipt is not None, "RELEASE_CONFLICT")
            require(receipt.input_sha256 == plan.input_sha256 and receipt.candidate_sha256 == plan.candidate_sha256
                    and receipt.approval_sha256 == plan.approval_sha256 and receipt.approval_json == plan.approval_json
                    and receipt.published_at == existing.published_at, "PUBLICATION_APPROVAL_CONFLICT")
            validate_published_market(session, plan.release_id)
            check_review_window(plan.reviewed_at, plan.expires_at, clock())
            return dict(status="already_published", execute=True, **plan.summary())
        for instrument_id, symbol in sorted(plan.instruments):
            instrument = session.exec(select(Instrument).where(Instrument.id == instrument_id).with_for_update()).first()
            require(instrument is not None and instrument.symbol == symbol, "INSTRUMENT_IDENTITY_CONFLICT")
            require(instrument.currency in (None, "USD")
                    and instrument.metadata_status in ("unverified", "verified"), "INSTRUMENT_METADATA_CONFLICT")
            if instrument.currency != "USD" or instrument.metadata_status != "verified":
                instrument.currency, instrument.metadata_status = "USD", "verified"
                session.add(instrument)
        for digest, kind, body in plan.revisions:
            insert_missing(session, MarketRevision, dict(id=digest, kind=kind, body_json=body), ["id"])
            row = session.get(MarketRevision, digest)
            require(row.kind == kind and row.body_json == body, "REVISION_CONFLICT")
        insert_missing(session, CalendarBinding, dict(
            calendar_id=plan.calendar_id, version=plan.calendar_version, revision_id=plan.calendar_revision_id,
        ), ["calendar_id", "version"])
        binding = session.get(CalendarBinding, (plan.calendar_id, plan.calendar_version))
        require(binding.revision_id == plan.calendar_revision_id, "CALENDAR_BINDING_CONFLICT")
        published_at = now.replace(tzinfo=None)
        session.add(DatasetRelease(
            id=plan.release_id, manifest_json=plan.manifest_json, sha256=plan.release_id[4:],
            qualification="simulation_qualified", created_at=published_at, published_at=published_at,
        ))
        session.flush()
        session.add(MarketPublication(
            release_id=plan.release_id, input_sha256=plan.input_sha256,
            candidate_sha256=plan.candidate_sha256, approval_sha256=plan.approval_sha256,
            approval_json=plan.approval_json, published_at=published_at,
        ))
        session.flush()
        validate_published_market(session, plan.release_id)
        check_review_window(plan.reviewed_at, plan.expires_at, clock())
    return dict(status="published", execute=True, **plan.summary())
