"""Synthetic approvals for private test databases. Never real operator evidence."""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from sqlmodel import Session

from finance_app_database_service.contracts import canonical_json, content_digest
from finance_app_database_service.foundation_models import Instrument
from finance_app_database_service.market_publication import REVIEW_DOMAIN, ReviewBody, ReviewChecks
from finance_app_database_service.market_qualification import preflight
from test_market_qualification import A, bundle

KEY = bytes.fromhex("ab" * 32)
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


def seal(review, key=KEY):
    return dict(review=review, signature=hmac.new(
        key, REVIEW_DOMAIN + canonical_json(review).encode(), hashlib.sha256,
    ).hexdigest())


def artifacts(value=None, now=NOW):
    value = bundle() if value is None else value
    candidate = preflight(value)
    review = ReviewBody(
        schema_version=1, policy_id="us-equity-offline-v1",
        input_sha256=candidate["input_sha256"], candidate_sha256=content_digest(canonical_json(candidate)),
        reviewer_id="synthetic-test-reviewer", review_reference="test-only approval, not authenticated source evidence",
        reviewed_at=now, expires_at=now + timedelta(hours=1),
        checks={name: True for name in ReviewChecks.__fields__},
    )
    return value, candidate, seal(json.loads(review.json()))


def seed_instrument(engine):
    with Session(engine) as session:
        session.add(Instrument(id=A, symbol="AAPL", name="Synthetic listing",
                               exchange="TEST", currency=None, metadata_status="unverified"))
        session.commit()
