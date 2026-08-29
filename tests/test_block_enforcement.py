"""
Regression tests: "Block" used to report success even though writing
/sys/.../authorized silently failed for an unprivileged user service.
"""

import os
import tempfile
import unittest
from pathlib import Path

from context import identity, make_daemon, usb_event


class TestEnforceBlock(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._dev = tempfile.TemporaryDirectory()
        self.dev_path = Path(self._dev.name)

    def tearDown(self):
        self._tmp.cleanup()
        self._dev.cleanup()

    def _daemon(self, mode="direct"):
        return make_daemon(self._tmp.name, cfg={"usb": {"block_enforcement": mode}})

    def test_writable_attribute_is_actually_deauthorized(self):
        auth = self.dev_path / "authorized"
        auth.write_text("1", encoding="utf-8")

        result = self._daemon()._enforce_block(str(self.dev_path))

        self.assertTrue(result.ok)
        self.assertEqual(auth.read_text(encoding="utf-8"), "0")

    def test_missing_attribute_reports_not_enforced(self):
        result = self._daemon()._enforce_block(str(self.dev_path))
        self.assertFalse(result.ok)
        self.assertIn("recorded only", result.detail)

    def test_empty_sys_path_reports_not_enforced(self):
        result = self._daemon()._enforce_block("")
        self.assertFalse(result.ok)
        self.assertIn("recorded only", result.detail)

    def test_none_mode_does_not_touch_sysfs(self):
        auth = self.dev_path / "authorized"
        auth.write_text("1", encoding="utf-8")

        result = self._daemon(mode="none")._enforce_block(str(self.dev_path))

        self.assertFalse(result.ok)
        self.assertEqual(auth.read_text(encoding="utf-8"), "1")

    @unittest.skipIf(os.geteuid() == 0, "root can write anything")
    def test_permission_denied_reports_needs_root(self):
        auth = self.dev_path / "authorized"
        auth.write_text("1", encoding="utf-8")
        auth.chmod(0o400)

        result = self._daemon()._enforce_block(str(self.dev_path))

        self.assertFalse(result.ok)
        self.assertIn("needs root", result.detail)

    def test_unknown_mode_falls_back_to_direct(self):
        self.assertEqual(self._daemon(mode="teleport")._block_enforcement, "direct")

    def test_block_is_recorded_even_when_it_cannot_be_enforced(self):
        daemon = self._daemon()
        ident = identity()

        daemon._apply_block(ident, "Sketchy Stick", str(self.dev_path), {})

        self.assertTrue(daemon.state.is_blocked(ident))
        self.assertEqual(daemon.test_notifier.summaries(), ["USB block recorded (not enforced)"])

    def test_enforced_block_says_so(self):
        (self.dev_path / "authorized").write_text("1", encoding="utf-8")
        daemon = self._daemon()

        daemon._apply_block(identity(), "Sketchy Stick", str(self.dev_path), {})

        self.assertEqual(daemon.test_notifier.summaries(), ["USB blocked"])

    def test_known_blocked_device_is_re_enforced_on_replug(self):
        daemon = self._daemon()
        ident = identity()
        daemon.state.mark_blocked(ident, {})
        (self.dev_path / "authorized").write_text("1", encoding="utf-8")

        daemon._handle_usb_add(usb_event(ident=ident, name="Sketchy Stick", sys_path=str(self.dev_path)))

        self.assertEqual((self.dev_path / "authorized").read_text(encoding="utf-8"), "0")
        self.assertEqual(daemon.test_prompts.submitted, [], "known device should not prompt")

    def test_known_trusted_device_is_not_prompted(self):
        daemon = self._daemon()
        ident = identity()
        daemon.state.mark_trusted(ident, {})

        daemon._handle_usb_add(usb_event(ident=ident, name="Keyboard"))

        self.assertEqual(daemon.test_prompts.submitted, [])
        self.assertEqual(daemon.test_notifier.summaries(), ["USB trusted: Keyboard"])


if __name__ == "__main__":
    unittest.main()
