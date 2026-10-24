"""Preview-first operator publication. --execute requires a reviewed exact target."""

import argparse
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Tuple

from pydantic import StrictBool, root_validator, validator

from finance_app_database_service.contracts import (
    Contract, Digest, QualifiedCoverage, ReleaseManifest, canonical_json, content_digest,
)
from finance_app_database_service.market_documents import MAX_RELEASE_BYTES, MAX_REVISION_BYTES, ValidationDocument
from finance_app_database_service.market_qualification import (
    MAX_INPUT_BYTES, QualificationError, Text, invalid_constant, preflight, unique_object,
)

REVIEW_DOMAIN = b"finance-app/market-publication-review/v1\n"
MAX_APPROVAL_BYTES = 32 * 1024


class PublicationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise PublicationError(code)


class ReviewChecks(Contract):
    independent_calendar_authenticated: StrictBool
    currency_verified: StrictBool
    as_traded_prices_verified: StrictBool
    corporate_actions_verified: StrictBool
    terminal_events_reviewed: StrictBool
    instrument_identities_verified: StrictBool
    shortability_reviewed: StrictBool
    licensing_reviewed: StrictBool

    @root_validator
    def all_approved(cls, values):
        if len(values) != len(cls.__fields__) or any(value is not True for value in values.values()):
            raise ValueError("All review checks require explicit approval")
        return values


class ReviewBody(Contract):
    schema_version: Literal[1]
    policy_id: Literal["us-equity-offline-v1"]
    input_sha256: Digest
    candidate_sha256: Digest
    reviewer_id: Text
    review_reference: Text
    reviewed_at: datetime
    expires_at: datetime
    checks: ReviewChecks

    @validator("reviewed_at", "expires_at")
    def utc_timestamp(cls, value):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("Review timestamps must be timezone-aware UTC")
        return value


class SignedReview(Contract):
    review: ReviewBody
    signature: Digest


@dataclass(frozen=True)
class PublicationPlan:
    release_id: str
    manifest_json: str
    revisions: Tuple[Tuple[str, str, str], ...]
    instruments: Tuple[Tuple[str, str], ...]
    calendar_id: str
    calendar_version: str
    calendar_revision_id: str
    input_sha256: str
    candidate_sha256: str
    approval_sha256: str
    approval_json: str
    reviewed_at: datetime
    expires_at: datetime

    def summary(self):
        return dict(
            release_id=self.release_id, input_sha256=self.input_sha256,
            candidate_sha256=self.candidate_sha256, approval_sha256=self.approval_sha256,
            instrument_count=len(self.instruments), revision_count=len(self.revisions),
        )


def utcnow():
    return datetime.now(timezone.utc)


def check_review_window(reviewed_at, expires_at, now):
    require(now.tzinfo is not None and now.utcoffset() == timedelta(0), "INVALID_REVIEW_CLOCK")
    require(timedelta(0) < expires_at - reviewed_at <= timedelta(hours=24), "INVALID_REVIEW_WINDOW")
    require(reviewed_at <= now < expires_at, "REVIEW_NOT_CURRENT")


def parse_review_key(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None,
            "REVIEW_KEY_UNAVAILABLE")
    return bytes.fromhex(value)


def verify_review(value, key, now):
    require(isinstance(key, bytes) and len(key) == 32, "REVIEW_KEY_UNAVAILABLE")
    try:
        signed = SignedReview.parse_obj(value)
    except (ValueError, TypeError, RecursionError):
        raise PublicationError("INVALID_REVIEW") from None
    signature = hmac.new(key, REVIEW_DOMAIN + canonical_json(signed.review).encode("utf-8"),
                         hashlib.sha256).hexdigest()
    require(hmac.compare_digest(signature, signed.signature), "REVIEW_SIGNATURE_INVALID")
    check_review_window(signed.review.reviewed_at, signed.review.expires_at, now)
    return signed


def build_plan(input_value, candidate_value, approval_value, key, *, now=None):
    """Recompute the exact reviewed candidate; no DB or provider access."""
    signed = verify_review(approval_value, key, now if now is not None else utcnow())
    try:
        candidate_json = canonical_json(candidate_value)
    except (ValueError, TypeError, RecursionError):
        raise PublicationError("INVALID_CANDIDATE") from None
    require(len(candidate_json.encode("utf-8")) <= MAX_RELEASE_BYTES, "CANDIDATE_CAPACITY_EXCEEDED")
    require(content_digest(candidate_json) == signed.review.candidate_sha256, "CANDIDATE_APPROVAL_MISMATCH")
    candidate = preflight(input_value)
    require(canonical_json(candidate) == candidate_json, "CANDIDATE_RECOMPUTATION_MISMATCH")
    require(candidate["input_sha256"] == signed.review.input_sha256, "INPUT_APPROVAL_MISMATCH")
    revisions = {row["id"]: (row["id"], row["kind"], canonical_json(row["document"]))
                 for row in candidate["revisions"]}
    coverage = []
    for item in candidate["coverage"]:
        report = ValidationDocument(
            schema_version=1, instrument_id=item["instrument_id"], qualification="simulation_qualified",
            currency="USD", price_basis="as_traded", calendar_revision_id=candidate["calendar_revision_id"],
            bar_revision_id=item["bar_revision_id"], action_revision_id=item["action_revision_id"],
            source_snapshot_ids=item["source_snapshot_ids"],
            checks=["currency", "calendar", "price_basis", "corporate_actions", "terminal_events"],
        )
        body = canonical_json(report)
        digest = content_digest(body)
        revisions[digest] = (digest, "validation", body)
        coverage.append(QualifiedCoverage(
            **{field: item[field] for field in (
                "instrument_id", "from_date", "to_date", "shortable", "bar_revision_id",
                "action_revision_id", "source_snapshot_ids",
            )}, validation_report_sha256=digest,
        ))
    require(all(len(row[2].encode("utf-8")) <= MAX_REVISION_BYTES for row in revisions.values()),
            "REVISION_CAPACITY_EXCEEDED")
    require(sum(len(row[2].encode("utf-8")) for row in revisions.values()) <= MAX_RELEASE_BYTES,
            "RELEASE_CAPACITY_EXCEEDED")
    calendar = next(row["document"] for row in candidate["revisions"] if row["kind"] == "calendar")
    manifest = ReleaseManifest(
        schema_version=1, qualification="simulation_qualified", currency="USD", price_basis="as_traded",
        calendar_id=calendar["calendar_id"], calendar_version=calendar["calendar_version"],
        from_date=min(item.from_date for item in coverage), to_date=max(item.to_date for item in coverage),
        instruments=coverage,
    )
    manifest_json = canonical_json(manifest)
    approval_json = canonical_json(signed)
    return PublicationPlan(
        release_id="rel_" + content_digest(manifest_json), manifest_json=manifest_json,
        revisions=tuple(sorted(revisions.values())),
        instruments=tuple((item["instrument_id"], item["symbol"]) for item in candidate["coverage"]),
        calendar_id=calendar["calendar_id"], calendar_version=calendar["calendar_version"],
        calendar_revision_id=candidate["calendar_revision_id"],
        input_sha256=candidate["input_sha256"], candidate_sha256=content_digest(candidate_json),
        approval_sha256=content_digest(approval_json), approval_json=approval_json,
        reviewed_at=signed.review.reviewed_at, expires_at=signed.review.expires_at,
    )


def read_artifact(path, limit):
    with path.open("rb") as source:
        raw = source.read(limit + 1)
    require(len(raw) <= limit, "ARTIFACT_CAPACITY_EXCEEDED")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise PublicationError("INVALID_JSON") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--approval", required=True, type=Path)
    parser.add_argument("--execute", action="store_true", help="Publish atomically to the explicitly fenced database")
    parser.add_argument("--expected-target-sha256", help="Required for execution; never a credential")
    parser.add_argument("--expected-schema", help="PostgreSQL schema, default public; SQLite uses main")
    args = parser.parse_args(argv)
    try:
        require(not args.execute or re.fullmatch(r"[0-9a-f]{64}", args.expected_target_sha256 or "") is not None,
                "EXPECTED_TARGET_REQUIRED")
        key = parse_review_key(os.getenv("FINANCE_PUBLICATION_REVIEW_KEY"))
        input_value = read_artifact(args.input, MAX_INPUT_BYTES)
        candidate = read_artifact(args.candidate, MAX_RELEASE_BYTES)
        approval = read_artifact(args.approval, MAX_APPROVAL_BYTES)
        if not args.execute:
            plan = build_plan(input_value, candidate, approval, key)
            result = dict(status="preview", execute=False, **plan.summary())
        else:
            # Preview never imports DB configuration, opens a connection, or migrates.
            from sqlalchemy.exc import SQLAlchemyError

            try:
                from finance_app_database_service.database import engine
                from finance_app_database_service.publication_store import publish

                result = publish(engine, input_value, candidate, approval, key, args.expected_target_sha256,
                                 expected_schema=args.expected_schema)
            except SQLAlchemyError:
                raise PublicationError("DATABASE_PUBLICATION_FAILED") from None
            except (TypeError, ValueError) as error:
                if isinstance(error, (PublicationError, QualificationError)):
                    raise
                raise PublicationError("DATABASE_CONFIGURATION_INVALID") from None
        print(canonical_json(result))
        return 0
    except (PublicationError, QualificationError) as error:
        print(canonical_json(dict(status="blocked", code=error.code)))
    except OSError:
        print(canonical_json(dict(status="blocked", code="ARTIFACT_IO_ERROR")))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
