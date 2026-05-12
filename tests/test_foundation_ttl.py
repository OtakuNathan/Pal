from __future__ import annotations

import unittest

from pal.foundation import BoundedTTLBuffer


class BoundedTTLBufferTests(unittest.TestCase):
    def test_upsert_dedupes_caps_and_preserves_newest_first(self) -> None:
        now = [0.0]
        buffer = BoundedTTLBuffer[str](capacity=2, clock=lambda: now[0])

        buffer.upsert("a", "old")
        now[0] += 1
        buffer.upsert("b", "middle")
        now[0] += 1
        buffer.upsert("a", "new")

        self.assertEqual(buffer.items(), [("a", "new"), ("b", "middle")])
        self.assertEqual(buffer.get("a"), "new")

        now[0] += 1
        buffer.upsert("c", "latest")
        self.assertEqual(buffer.items(), [("c", "latest"), ("a", "new")])
        self.assertEqual(buffer.get("b", None), None)

    def test_ttl_prunes_expired_entries(self) -> None:
        now = [0.0]
        buffer = BoundedTTLBuffer[str](capacity=5, ttl_seconds=10, clock=lambda: now[0])

        buffer.upsert("a", "value")
        self.assertEqual(buffer.values(), ["value"])

        now[0] = 10.0
        self.assertEqual(buffer.values(), [])

    def test_pop_removes_or_raises(self) -> None:
        buffer = BoundedTTLBuffer[str](capacity=5)
        buffer.upsert("a", "value")

        self.assertEqual(buffer.pop("a"), "value")
        self.assertEqual(buffer.pop("missing", None), None)
        with self.assertRaises(KeyError):
            buffer.pop("missing")


if __name__ == "__main__":
    unittest.main()
