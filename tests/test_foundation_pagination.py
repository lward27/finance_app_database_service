import base64
import json
import unittest

from finance_app_database_service.pagination import cursor_scope, decode_cursor, encode_cursor
from finance_app_database_service.problems import Problem


class TestOpaqueCursors(unittest.TestCase):
    def test_round_trip_and_normalized_scope(self):
        scope = cursor_scope("instruments", query="AAPL", exchange="NASDAQ")
        self.assertEqual(scope, cursor_scope("instruments", exchange="NASDAQ", query="AAPL"))
        cursor = encode_cursor(scope, "AAPL", "ins_" + "a" * 32)
        self.assertEqual(decode_cursor(cursor, scope, 2), ["AAPL", "ins_" + "a" * 32])

    def test_rejects_invalid_shapes_types_versions_and_scopes(self):
        scope = cursor_scope("instruments")
        valid = {"v": 1, "scope": scope, "key": ["AAPL", "id"]}
        invalid = [
            [], None, {"v": 1}, {**valid, "v": True}, {**valid, "v": 2},
            {**valid, "scope": "other"}, {**valid, "extra": "unknown"},
            {**valid, "key": [1, "id"]}, {**valid, "key": ["AAPL"]},
            {**valid, "key": ["x" * 257, "id"]},
        ]
        for payload in invalid:
            cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
            with self.subTest(payload=payload), self.assertRaises(Problem) as caught:
                decode_cursor(cursor, scope, 2)
            self.assertEqual(caught.exception.code, "INVALID_CURSOR")

    def test_rejects_control_characters_and_unpaired_unicode(self):
        scope = cursor_scope("instruments")
        for key in ("\x00", "A\nB", "\ud800"):
            cursor = base64.urlsafe_b64encode(json.dumps({
                "v": 1, "scope": scope, "key": [key],
            }).encode()).decode()
            with self.subTest(key=repr(key)), self.assertRaises(Problem):
                decode_cursor(cursor, scope, 1)

    def test_rejects_unbounded_or_nonbase64_input(self):
        for cursor in ("", "a" * 1025, "**", "a", "===="):
            with self.subTest(cursor=cursor[:10]), self.assertRaises(Problem):
                decode_cursor(cursor, cursor_scope("instruments"), 1)
