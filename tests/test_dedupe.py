import tempfile
import unittest

from context import identity, make_daemon


class TestDedupe(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _daemon(self, window_ms):
        return make_daemon(self._tmp.name, cfg={"usb": {"dedupe_window_ms": window_ms}})

    def test_repeat_within_window_is_suppressed(self):
        d = self._daemon(5000)
        args = ("add", identity(), "/sys/a", "/dev/a", "1-1")
        self.assertFalse(d._usb_should_suppress(*args))
        self.assertTrue(d._usb_should_suppress(*args))

    def test_zero_window_disables_dedupe(self):
        d = self._daemon(0)
        args = ("add", identity(), "/sys/a", "/dev/a", "1-1")
        self.assertFalse(d._usb_should_suppress(*args))
        self.assertFalse(d._usb_should_suppress(*args))

    def test_add_and_remove_are_tracked_separately(self):
        d = self._daemon(5000)
        ident = identity()
        self.assertFalse(d._usb_should_suppress("add", ident, "/sys/a", "/dev/a", "1-1"))
        self.assertFalse(d._usb_should_suppress("remove", ident, "/sys/a", "/dev/a", "1-1"))

    def test_identity_is_preferred_over_path(self):
        d = self._daemon(5000)
        ident = identity()
        # Same device, different sysfs path: identity should still dedupe it.
        self.assertFalse(d._usb_should_suppress("add", ident, "/sys/a", "/dev/a", "1-1"))
        self.assertTrue(d._usb_should_suppress("add", ident, "/sys/b", "/dev/b", "2-2"))

    def test_path_is_used_when_identity_is_missing(self):
        d = self._daemon(5000)
        self.assertFalse(d._usb_should_suppress("add", None, "/sys/a", "", ""))
        self.assertTrue(d._usb_should_suppress("add", None, "/sys/a", "", ""))
        self.assertFalse(d._usb_should_suppress("add", None, "/sys/b", "", ""))

    def test_recent_map_stays_bounded(self):
        d = self._daemon(1)
        for i in range(2000):
            d._usb_should_suppress("add", None, f"/sys/{i}", "", "")
        self.assertLessEqual(len(d._recent_usb), 1024)


if __name__ == "__main__":
    unittest.main()
