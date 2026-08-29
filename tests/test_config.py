"""
Regression tests: --config used to be half-honored — the config was read from the
given path but trusted.json/blocked.json were always written under XDG_CONFIG_HOME.
"""

import json
import tempfile
import unittest
from pathlib import Path

from context import buswatchd


class TestLoadConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, payload):
        p = self.dir / "config.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return p

    def test_loads_an_object(self):
        p = self._write({"log_level": "DEBUG"})
        self.assertEqual(buswatchd.load_config(p), {"log_level": "DEBUG"})

    def test_rejects_a_non_object(self):
        p = self._write([1, 2, 3])
        with self.assertRaises(RuntimeError):
            buswatchd.load_config(p)

    def test_rejects_invalid_json(self):
        p = self.dir / "config.json"
        p.write_text("{nope", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            buswatchd.load_config(p)


class TestResolveStateDir(unittest.TestCase):
    def test_defaults_to_the_config_files_directory(self):
        cfg_path = Path("/home/someone/.config/buswatchd/config.json")
        self.assertEqual(
            buswatchd.resolve_state_dir({}, cfg_path, None),
            Path("/home/someone/.config/buswatchd"),
        )

    def test_config_key_overrides_the_default(self):
        cfg_path = Path("/etc/buswatchd/config.json")
        self.assertEqual(
            buswatchd.resolve_state_dir({"state_dir": "/var/lib/buswatchd"}, cfg_path, None),
            Path("/var/lib/buswatchd"),
        )

    def test_cli_flag_wins_over_the_config_key(self):
        cfg_path = Path("/etc/buswatchd/config.json")
        self.assertEqual(
            buswatchd.resolve_state_dir({"state_dir": "/var/lib/buswatchd"}, cfg_path, "/tmp/state"),
            Path("/tmp/state"),
        )

    def test_tilde_is_expanded(self):
        resolved = buswatchd.resolve_state_dir({}, Path("/x/config.json"), "~/state")
        self.assertEqual(resolved, Path.home() / "state")

    def test_a_relocated_config_keeps_its_state_alongside_it(self):
        # The whole point of the fix: point --config elsewhere and the state follows.
        cfg_path = Path("/opt/testing/buswatchd.json")
        self.assertEqual(buswatchd.resolve_state_dir({}, cfg_path, None), Path("/opt/testing"))


if __name__ == "__main__":
    unittest.main()
