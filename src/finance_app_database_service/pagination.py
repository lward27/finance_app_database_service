"""Bounded opaque keyset cursors, scoped to the endpoint's normalized filters."""

import base64
import binascii
import hashlib
import json

from finance_app_database_service.problems import Problem


def cursor_scope(resource: str, **filters) -> str:
    body = json.dumps([resource, filters], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()[:24]


def encode_cursor(scope: str, *key: str) -> str:
    body = json.dumps({"v": 1, "scope": scope, "key": key}, separators=(",", ":"))
    return base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, scope: str, key_size: int) -> list[str]:
    try:
        if not 1 <= len(cursor) <= 1024:
            raise ValueError()
        body = json.loads(base64.b64decode(
            cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True,
        ))
        if (
            not isinstance(body, dict) or set(body) != {"v", "scope", "key"}
            or type(body["v"]) is not int or body["v"] != 1 or body["scope"] != scope
            or not isinstance(body["key"], list) or len(body["key"]) != key_size
            or any(not isinstance(v, str) or len(v) > 256 for v in body["key"])
        ):
            raise ValueError()
        for value in body["key"]:
            value.encode("utf-8")
            if any(ord(character) < 32 for character in value):
                raise ValueError()
        return body["key"]
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise Problem(422, "INVALID_CURSOR", "Cursor does not match this request") from None
