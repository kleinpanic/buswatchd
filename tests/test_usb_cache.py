import tempfile
import unittest

from context import buswatchd, identity, make_daemon, usb_event


class TestUsbCacheBounds(unittest.TestCase):
    """Regression: the add-event cache used to grow forever in a long-lived daemon."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.daemon = make_daemon(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_cache_is_bounded(self):
        for i in range(buswatchd.USB_CACHE_MAX_ENTRIES * 2):
            self.daemon._cache_usb(
                usb_event(sys_path=f"/sys/{i}", device_path=f"/dev/{i}", sys_name=f"n{i}")
            )
        self.assertLessEqual(len(self.daemon._usb_cache), buswatchd.USB_CACHE_MAX_ENTRIES)

    def test_lookup_finds_entry_by_any_key(self):
        ident = identity()
        self.daemon._cache_usb(
            usb_event(ident=ident, name="Keyboard", sys_path="/sys/a", device_path="/dev/a", sys_name="1-2")
        )
        for probe in (("/sys/a", "", ""), ("", "/dev/a", ""), ("", "", "1-2")):
            found_ident, found_name = self.daemon._lookup_usb_cache(*probe)
            self.assertEqual(found_ident, ident)
            self.assertEqual(found_name, "Keyboard")

    def test_lookup_refreshes_recency_so_hot_entries_survive(self):
        self.daemon._cache_usb(usb_event(name="Old", sys_path="/sys/keep", device_path="", sys_name=""))
        for i in range(buswatchd.USB_CACHE_MAX_ENTRIES - 1):
            self.daemon._cache_usb(usb_event(sys_path=f"/sys/f{i}", device_path="", sys_name=""))
            self.daemon._lookup_usb_cache("/sys/keep", "", "")

        self.daemon._cache_usb(usb_event(sys_path="/sys/overflow", device_path="", sys_name=""))
        _, name = self.daemon._lookup_usb_cache("/sys/keep", "", "")
        self.assertEqual(name, "Old")

    def test_empty_keys_are_not_cached(self):
        self.daemon._cache_usb(usb_event(sys_path="/sys/a", device_path="", sys_name=""))
        self.assertNotIn("", self.daemon._usb_cache)

    def test_remove_event_borrows_name_from_add(self):
        ident = identity()
        self.daemon._cache_usb(usb_event(ident=ident, name="Yubikey", sys_path="/sys/y", device_path="", sys_name="2-1"))
        self.daemon._handle_usb_remove(usb_event(action="remove", name="USB device", sys_path="/sys/y", device_path="", sys_name="2-1"))
        summaries = self.daemon.test_notifier.summaries()
        self.assertEqual(summaries, ["USB remove: Yubikey"])


if __name__ == "__main__":
    unittest.main()
