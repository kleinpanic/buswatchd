import tempfile
import unittest

from context import make_daemon


class TestDrmDiff(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.daemon = make_daemon(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _set(self, current, following):
        self.daemon._drm_status = dict(current)
        self.daemon._read_drm_status = lambda: dict(following)

    def test_no_change_yields_nothing(self):
        self._set({"card0-HDMI-A-1": "connected"}, {"card0-HDMI-A-1": "connected"})
        self.assertIsNone(self.daemon._diff_drm())

    def test_disconnect_is_detected(self):
        self._set({"card0-HDMI-A-1": "connected"}, {"card0-HDMI-A-1": "disconnected"})
        diff = self.daemon._diff_drm()
        self.assertEqual(diff.changes, {"card0-HDMI-A-1": ("connected", "disconnected")})

    def test_connect_is_detected(self):
        self._set({"card0-DP-1": "disconnected"}, {"card0-DP-1": "connected"})
        diff = self.daemon._diff_drm()
        self.assertEqual(diff.changes, {"card0-DP-1": ("disconnected", "connected")})

    def test_new_connector_is_reported_as_unknown_to_current(self):
        self._set({}, {"card0-DP-2": "connected"})
        diff = self.daemon._diff_drm()
        self.assertEqual(diff.changes, {"card0-DP-2": ("unknown", "connected")})

    def test_vanished_connector_is_reported_as_missing(self):
        self._set({"card0-DP-2": "connected"}, {})
        diff = self.daemon._diff_drm()
        self.assertEqual(diff.changes, {"card0-DP-2": ("connected", "missing")})

    def test_diff_updates_the_baseline(self):
        self._set({"card0-DP-1": "disconnected"}, {"card0-DP-1": "connected"})
        self.daemon._diff_drm()
        self.assertEqual(self.daemon._drm_status, {"card0-DP-1": "connected"})
        self.assertIsNone(self.daemon._diff_drm())

    def test_handler_notifies_on_connect(self):
        self._set({"card0-HDMI-A-1": "disconnected"}, {"card0-HDMI-A-1": "connected"})
        self.daemon._handle_drm_change()
        self.assertEqual(
            self.daemon.test_notifier.summaries(), ["Display connected: card0-HDMI-A-1"]
        )

    def test_handler_respects_the_config_toggle(self):
        daemon = make_daemon(self._tmp.name, cfg={"drm": {"notify_changes": False}})
        daemon._drm_status = {"card0-HDMI-A-1": "disconnected"}
        daemon._read_drm_status = lambda: {"card0-HDMI-A-1": "connected"}
        daemon._handle_drm_change()
        self.assertEqual(daemon.test_notifier.sent, [])


if __name__ == "__main__":
    unittest.main()
