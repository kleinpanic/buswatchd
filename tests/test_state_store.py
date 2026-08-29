import json
import tempfile
import unittest
from pathlib import Path

from context import buswatchd, identity


class TestStateStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_trust_and_block_round_trip(self):
        store = buswatchd.StateStore(self.dir)
        ident = identity()

        self.assertFalse(store.is_trusted(ident))
        self.assertFalse(store.is_blocked(ident))

        store.mark_trusted(ident, {"name": "Widget"})
        self.assertTrue(store.is_trusted(ident))

        reloaded = buswatchd.StateStore(self.dir)
        self.assertTrue(reloaded.is_trusted(ident))
        self.assertEqual(reloaded.trusted[ident.key]["name"], "Widget")

    def test_trust_and_block_are_mutually_exclusive(self):
        store = buswatchd.StateStore(self.dir)
        ident = identity()

        store.mark_trusted(ident, {})
        store.mark_blocked(ident, {})
        self.assertTrue(store.is_blocked(ident))
        self.assertFalse(store.is_trusted(ident))

        store.mark_trusted(ident, {})
        self.assertTrue(store.is_trusted(ident))
        self.assertFalse(store.is_blocked(ident))

    def test_both_files_persist_after_a_single_mark(self):
        store = buswatchd.StateStore(self.dir)
        store.mark_blocked(identity(), {})
        self.assertTrue(store.trusted_path.exists())
        self.assertTrue(store.blocked_path.exists())

    def test_corrupt_state_file_is_tolerated(self):
        (self.dir / "trusted.json").write_text("{not json", encoding="utf-8")
        store = buswatchd.StateStore(self.dir)
        self.assertEqual(store.trusted, {})

    def test_non_dict_state_file_is_ignored(self):
        (self.dir / "blocked.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        store = buswatchd.StateStore(self.dir)
        self.assertEqual(store.blocked, {})

    def test_save_leaves_no_temp_file_behind(self):
        store = buswatchd.StateStore(self.dir)
        store.mark_trusted(identity(), {})
        self.assertEqual(list(self.dir.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
