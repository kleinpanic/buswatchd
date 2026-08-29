"""
Config layering and dependency reporting: the machinery that lets a new release
change its settings without an existing install having to be touched.
"""

import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from context import buswatchd, make_daemon

REPO = Path(__file__).resolve().parent.parent


class TestMergeConfig(unittest.TestCase):
    def test_user_values_win(self):
        merged = buswatchd.merge_config({"a": 1}, {"a": 2})
        self.assertEqual(merged["a"], 2)

    def test_absent_keys_take_the_default(self):
        merged = buswatchd.merge_config({"a": 1, "b": 2}, {"a": 9})
        self.assertEqual(merged["b"], 2)

    def test_nested_sections_merge_rather_than_replace(self):
        merged = buswatchd.merge_config(
            {"usb": {"notify_add": True, "dedupe_window_ms": 1200}},
            {"usb": {"notify_add": False}},
        )
        self.assertFalse(merged["usb"]["notify_add"])
        self.assertEqual(merged["usb"]["dedupe_window_ms"], 1200)

    def test_defaults_are_not_mutated(self):
        defaults = {"usb": {"notify_add": True}}
        buswatchd.merge_config(defaults, {"usb": {"notify_add": False}})
        self.assertTrue(defaults["usb"]["notify_add"])

    def test_an_empty_config_yields_the_defaults(self):
        self.assertEqual(buswatchd.merge_config(buswatchd.DEFAULTS, {}), buswatchd.DEFAULTS)


class TestDriftDetection(unittest.TestCase):
    def test_unknown_top_level_key_is_reported(self):
        self.assertEqual(buswatchd.unknown_keys({"nonsense": 1}), ["nonsense"])

    def test_unknown_nested_key_is_reported_with_its_path(self):
        self.assertEqual(buswatchd.unknown_keys({"usb": {"nope": 1}}), ["usb.nope"])

    def test_a_typo_is_caught(self):
        self.assertEqual(buswatchd.unknown_keys({"notify_timout_ms": 1}), ["notify_timout_ms"])

    def test_a_complete_config_has_no_unknown_keys(self):
        shipped = json.loads((REPO / "config" / "config.json").read_text())
        self.assertEqual(buswatchd.unknown_keys(shipped), [])

    def test_missing_keys_are_reported_with_their_paths(self):
        missing = buswatchd.missing_keys({"usb": {"notify_add": True}})
        self.assertIn("usb.dedupe_window_ms", missing)
        self.assertIn("interactive", missing)

    def test_optional_settings_are_not_reported_as_missing(self):
        # state_dir defaults to None: worked out at runtime, never written out.
        self.assertNotIn("state_dir", buswatchd.missing_keys({}))

    def test_the_shipped_config_is_complete(self):
        shipped = json.loads((REPO / "config" / "config.json").read_text())
        self.assertEqual(buswatchd.missing_keys(shipped), [])


class TestFillMissing(unittest.TestCase):
    def test_existing_values_survive(self):
        filled = buswatchd.fill_missing({"notify_timeout_ms": 5000, "usb": {"notify_add": False}})
        self.assertEqual(filled["notify_timeout_ms"], 5000)
        self.assertFalse(filled["usb"]["notify_add"])

    def test_absent_settings_are_added(self):
        filled = buswatchd.fill_missing({})
        self.assertEqual(filled["usb"]["dedupe_window_ms"], 1200)

    def test_optional_settings_are_not_written_out(self):
        self.assertNotIn("state_dir", buswatchd.fill_missing({}))

    def test_unrecognized_keys_are_left_alone(self):
        self.assertEqual(buswatchd.fill_missing({"mine": 7})["mine"], 7)

    def test_result_is_complete(self):
        self.assertEqual(buswatchd.missing_keys(buswatchd.fill_missing({})), [])


class TestUpdateConfigCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self._tmp.name) / "config.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, argv_fn, path):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = argv_fn(path)
        return code, buf.getvalue()

    def test_missing_settings_are_written_and_values_preserved(self):
        self.cfg.write_text(json.dumps({"notify_timeout_ms": 5000}), encoding="utf-8")

        code, out = self._run(buswatchd.cmd_update_config, self.cfg)

        self.assertEqual(code, 0)
        written = json.loads(self.cfg.read_text())
        self.assertEqual(written["notify_timeout_ms"], 5000)
        self.assertEqual(written["usb"]["block_enforcement"], "direct")
        self.assertIn("added:", out)

    def test_the_previous_file_is_backed_up(self):
        self.cfg.write_text(json.dumps({"notify_timeout_ms": 5000}), encoding="utf-8")
        self._run(buswatchd.cmd_update_config, self.cfg)
        backup = self.cfg.with_suffix(".json.bak")
        self.assertTrue(backup.exists())
        self.assertEqual(json.loads(backup.read_text()), {"notify_timeout_ms": 5000})

    def test_running_twice_changes_nothing(self):
        self.cfg.write_text(json.dumps({}), encoding="utf-8")
        self._run(buswatchd.cmd_update_config, self.cfg)
        first = self.cfg.read_text()

        code, out = self._run(buswatchd.cmd_update_config, self.cfg)

        self.assertEqual(code, 0)
        self.assertEqual(self.cfg.read_text(), first)
        self.assertIn("already up to date", out)

    def test_unrecognized_keys_are_kept_and_flagged(self):
        self.cfg.write_text(json.dumps({"mine": 7}), encoding="utf-8")
        _, out = self._run(buswatchd.cmd_update_config, self.cfg)
        self.assertIn("mine", out)
        self.assertEqual(json.loads(self.cfg.read_text())["mine"], 7)

    def test_no_temp_file_is_left_behind(self):
        self.cfg.write_text(json.dumps({}), encoding="utf-8")
        self._run(buswatchd.cmd_update_config, self.cfg)
        self.assertEqual(list(self.cfg.parent.glob("*.tmp")), [])

    def test_diff_reports_drift_without_writing(self):
        self.cfg.write_text(json.dumps({"usb": {"notify_add": True}}), encoding="utf-8")
        before = self.cfg.read_text()

        code, out = self._run(buswatchd.cmd_diff_config, self.cfg)

        self.assertEqual(code, 0)
        self.assertIn("usb.dedupe_window_ms", out)
        self.assertEqual(self.cfg.read_text(), before)


class TestDaemonUsesDefaults(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_an_empty_config_still_produces_a_working_daemon(self):
        daemon = make_daemon(self._tmp.name, cfg={})
        self.assertTrue(daemon._interactive)
        self.assertEqual(daemon._usb_dedupe_ms, 1200)
        self.assertEqual(daemon._block_enforcement, "direct")

    def test_a_partial_config_only_overrides_what_it_names(self):
        daemon = make_daemon(self._tmp.name, cfg={"usb": {"dedupe_window_ms": 50}})
        self.assertEqual(daemon._usb_dedupe_ms, 50)
        self.assertTrue(daemon.cfg["usb"]["notify_add"])

    def test_a_config_from_before_a_setting_existed_still_works(self):
        # Exactly the shape of the config shipped before block_enforcement existed.
        legacy = {"usb": {"notify_add": True, "notify_remove": True}}
        daemon = make_daemon(self._tmp.name, cfg=legacy)
        self.assertEqual(daemon._block_enforcement, "direct")


class TestDependencyReporting(unittest.TestCase):
    def test_requirements_matches_the_version_the_code_requires(self):
        # Single source of truth: the pin exists in one place and is checked here.
        text = (REPO / "requirements.txt").read_text()
        match = re.search(r"^pyudev>=(\d+)\.(\d+)", text, re.MULTILINE)
        self.assertIsNotNone(match, "requirements.txt must pin pyudev with >=")
        self.assertEqual(tuple(int(g) for g in match.groups()), buswatchd.MIN_PYUDEV)

    def test_installed_pyudev_satisfies_the_minimum(self):
        version, raw = buswatchd.pyudev_version()
        self.assertGreaterEqual(version, buswatchd.MIN_PYUDEV, f"pyudev {raw} is too old")

    def test_version_parsing_tolerates_suffixes_and_short_versions(self):
        import pyudev

        real = pyudev.__version__
        try:
            for raw, expected in (("0.24.3", (0, 24)), ("0.24.3rc1", (0, 24)), ("1.0", (1, 0)), ("2", (2, 0))):
                pyudev.__version__ = raw
                self.assertEqual(buswatchd.pyudev_version()[0], expected, raw)
        finally:
            pyudev.__version__ = real

    def test_a_too_old_pyudev_fails_the_check(self):
        import pyudev

        real = pyudev.__version__
        buf = io.StringIO()
        try:
            pyudev.__version__ = "0.16.1"
            with redirect_stdout(buf):
                code = buswatchd.cmd_check_deps()
        finally:
            pyudev.__version__ = real
        self.assertEqual(code, 1, "an unsupported pyudev must fail the preflight")
        self.assertIn("TOO OLD", buf.getvalue())

    def test_install_hints_lead_with_the_local_package_manager(self):
        real = buswatchd._os_release_ids
        buswatchd._os_release_ids = lambda: ["debian"]
        try:
            self.assertEqual(buswatchd.install_hints()[0], "sudo apt install python3-pyudev")
        finally:
            buswatchd._os_release_ids = real

    def test_install_hints_follow_id_like_when_the_id_is_unknown(self):
        real = buswatchd._os_release_ids
        buswatchd._os_release_ids = lambda: ["someremix", "arch"]
        try:
            self.assertEqual(buswatchd.install_hints()[0], "sudo pacman -S python-pyudev")
        finally:
            buswatchd._os_release_ids = real

    def test_pip_is_always_offered_as_a_fallback(self):
        real = buswatchd._os_release_ids
        buswatchd._os_release_ids = lambda: ["plan9"]
        try:
            self.assertEqual(buswatchd.install_hints(), ["pip install --user -r requirements.txt"])
        finally:
            buswatchd._os_release_ids = real

    def test_check_deps_reports_success_on_this_machine(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = buswatchd.cmd_check_deps()
        self.assertEqual(code, 0)
        self.assertIn("pyudev:", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
