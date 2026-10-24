# Reviewed operator market publication

This CLI is the explicit write stage after the
[offline qualification preflight](market-qualification.md). It defaults to an
offline preview. No HTTP publication endpoint, automatic source qualification,
provider request, game-flag update, production deployment, or backfill change is
added. Implementation tests publish only synthetic fixtures in isolated databases.

## Review authority

A candidate is not approval. Before publication, a trusted operator must
authenticate the independent calendar, currency, as-traded prices, corporate
actions, terminal-event coverage, canonical listing identities, shortability
policy, and licensing of the actual retained artifacts.

The publisher requires an authenticated review envelope binding the exact
normalized input and candidate SHA-256 values. Its `ReviewBody` schema includes
`schema_version: 1`, `policy_id: "us-equity-offline-v1"`, `input_sha256`,
`candidate_sha256`, `reviewer_id`, `review_reference`, timezone-aware UTC
`reviewed_at` and `expires_at`, and all eight explicit true `ReviewChecks`.
Print the complete schema without any database access:

```sh
PYTHONPATH=src .venv/bin/python -c \
  'from finance_app_database_service.market_publication import SignedReview; print(SignedReview.schema_json(indent=2))'
```

False, missing, numeric, or string review flags are rejected. The review cannot
be future-dated or expired; its validity interval must be positive and at most
24 hours. Execution checks time again after obtaining the database lock and
immediately before commit. There is no automatic approval or signing CLI.

The envelope has two fields: `review`, the complete body, and `signature`, the
lowercase hex HMAC-SHA256 of:

```python
REVIEW_DOMAIN + canonical_json(ReviewBody.parse_obj(review)).encode("utf-8")
```

`REVIEW_DOMAIN` is the literal byte string
`b"finance-app/market-publication-review/v1\n"`. The key is a separate, randomly
generated 32-byte secret. The verifier reads its 64-character hex representation
from `FINANCE_PUBLICATION_REVIEW_KEY`, supplied by the operator's secret manager.
Never put it in source, approval files, command arguments, screenshots, or logs.
Do not reuse the service or guest-session credential.

This is a **shared-secret MAC**, not a public-key signature or per-person sign-in.
Anyone holding this key can authenticate a review and assert `reviewer_id`.
Protect and distribute it through the existing operator trust process; enforce
separation of duties outside this CLI when required. The software verifies the
approval's integrity and current authority, not whether a human genuinely
checked an artifact. Never sign fabricated evidence or copy the synthetic test
approvals into an operational workflow.

Retain the original input, candidate, signed review, and independently
authenticated reference artifacts in the approved evidence store. No uploader
or evidence-retention service is added here. Rotation rejects new commands
authenticated with an old key; it does not rewrite historical receipts or
invalidate already published financial inputs.

## Offline preview

Supply all three immutable files. No database configuration is imported and no
connection or file write occurs:

```sh
PYTHONPATH=src .venv/bin/python -m finance_app_database_service.market_publication \
  --input /absolute/path/to/qualification-input.json \
  --candidate /absolute/path/to/candidate.json \
  --approval /absolute/path/to/signed-review.json
```

The CLI verifies the MAC and time window, reruns preflight from the original
archive/evidence, and requires the recomputed candidate to match exactly.
It then builds content-addressed validation reports and a qualified manifest.
Preview prints only `status: "preview"`, hashes, projected release ID, and counts.
It does not persist a playable release.

Input/candidate limits remain 64/32 MiB; approval files are limited to 32 KiB.
Duplicate JSON keys, nonfinite constants, malformed input, extra review fields,
changed input/candidate hashes, incomplete checks, or a missing key fail closed.
Failures return a sanitized `blocked` code and nonzero exit status.

## Explicit database target and migration gate

Run migration `0004_market_publication` through the separately approved migration
path before execution. It adds the immutable `market_publication` receipt table
and triggers. It does not publish data, rewrite existing releases, or invent
approvals for pre-existing releases. Automatic destructive downgrade is disabled.
The publisher cannot adopt an existing draft or an already published release
without its matching receipt.

Inspect the resolved, credential-free database target locally before approving
execution. This command reads configuration but does not connect:

```sh
PYTHONPATH=src .venv/bin/python - <<'PY'
from finance_app_database_service.contracts import canonical_json
from finance_app_database_service.database import engine
from finance_app_database_service.publication_store import target_identity, target_sha256
schema = None  # PostgreSQL defaults to public; specify an approved non-default schema here.
print(canonical_json({"target": target_identity(engine, schema), "sha256": target_sha256(engine, schema)}))
PY
```

The target fingerprint binds driver, explicit PostgreSQL host/port/database/role/schema
(or the resolved SQLite path), never passwords. Implicit PostgreSQL targets and
URL query overrides are rejected. Password rotation does not change the target.
This fingerprints configured connection identity, not DNS, proxy routing, or
server ownership. Use an approved connection path and a dedicated least-privilege
publisher role. It needs the relevant selects/inserts and metadata updates, not
DDL, ingestion writes, deletes, or immutable-record updates.
Execution also verifies the actual database/session/current role and current
schema, then pins and schema-qualifies table access within the transaction.
Non-default PostgreSQL schemas require the matching `--expected-schema` argument;
only lowercase simple identifiers up to 63 characters are supported. SQLite uses
`main`. Search-path or role overrides cannot silently redirect publication.

**Existing configuration precedence still applies:** `PGPASSWORD` causes the
explicit PG environment settings to take precedence over `DATABASE_URL`.
Inspect the actual resolved target; do not assume a shell override selected
the intended database. Never print the raw URL or environment to diagnose it.

An exact target fingerprint is a safety check, not permission. Obtain approval
for the concrete environment, migration, reviewed candidate, and publication
operation before running:

```sh
PYTHONPATH=src .venv/bin/python -m finance_app_database_service.market_publication \
  --input /absolute/path/to/qualification-input.json \
  --candidate /absolute/path/to/candidate.json \
  --approval /absolute/path/to/signed-review.json \
  --execute --expected-target-sha256 <reviewed-target-sha256>
```

No live publication is authorized or executed merely by implementing this CLI.

## Transaction, retries, and conflicts

PostgreSQL uses a transaction advisory lock independent of migration/backfill
locks and a five-second lock timeout. SQLite tests use `BEGIN IMMEDIATE`.
Within one transaction, the publisher:

1. Rechecks approval validity after locking.
2. Locks canonical instruments in sorted order, verifies exact symbols, and
   marks supported unverified metadata USD/verified. It never silently replaces
   another currency, merges listings, changes symbols, or creates instruments.
3. Inserts immutable calendar/bar/action/validation revisions, checking every
   conflicting ID against its exact expected kind and canonical body.
4. Inserts the calendar/version binding, rejecting any attempted rebinding.
5. Inserts the published manifest and immutable receipt containing the original
   approval envelope and input/candidate/approval hashes.
6. Loads the complete release through the existing game integrity adapter and
   rechecks expiry immediately before commit.

Any failure rolls back all new revisions, bindings, metadata changes, release,
and receipt. Output is `published` or `already_published`; it contains hashes and
counts, not financial rows, review content, credentials, or raw database errors.

An exact retry with the same still-current review returns the existing release
without a second receipt or timestamp change. A different approval cannot
rewrite a receipt, even if the financial manifest has the same content ID.
Expired reviews fail before retry admission. If outcome is uncertain, reconcile
the projected release ID and receipt through approved read-only access rather
than fabricating a new review.

Calendar conflicts require a genuinely new, reviewed immutable calendar version,
not deletion/rebinding. Revision conflicts require investigation, not overwrite.
Draft/review conflicts require operator reconciliation. No unpublish/delete path
or destructive data rollback is supplied. Retain all records during a code
rollback and manage game admission/workers through their separately approved
operational path.

The existing game reader remains a hash/reference validator for trusted stored
inputs, not a per-request MAC verifier. Migration preserves older published
inputs without retroactively authenticating them. No public route exposes the
private approval envelope.

## Validation and remaining launch gates

Focused tests:

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_market_publication.py -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_publication_postgres.py -v
```

The latter requires a disposable PostgreSQL `FINANCE_TEST_DATABASE_URL` ending
in `_test`, uses private schemas, and cleans them afterward. Tests cover MAC/
time/claim/hash rejection, offline preview, target fencing, no implicit schema
creation, immutable receipts, metadata/calendar/revision conflicts, transaction
rollback, concurrent duplicate/competing publication, and a synthetic release
played end-to-end through the API and worker.

Actual archived inputs, independently authenticated evidence, a trusted operator
review/key process, real publication approval, identity/recovery, frontend,
capacity/ingress/observability validation, and explicit deployment remain launch
gates. No real source has been qualified by these tests.
