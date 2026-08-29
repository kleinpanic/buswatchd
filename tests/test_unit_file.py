"""
The systemd unit's install target is easy to get wrong in a way nothing notices
until the next reboot, so it is asserted here rather than trusted.
"""

import unittest
from pathlib import Path

UNIT = Path(__file__).resolve().parent.parent / "systemd" / "buswatchd.service"


def directives(text):
    found = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "[", ";")):
            continue
        key, _, value = line.partition("=")
        found.setdefault(key.strip(), []).append(value.strip())
    return found


class TestUnitFile(unittest.TestCase):
    def setUp(self):
        self.d = directives(UNIT.read_text(encoding="utf-8"))

    def test_autostarts_from_default_target(self):
        # graphical-session.target is static: on a bare WM nothing activates it,
        # so installing into it means the daemon never starts after a reboot.
        self.assertEqual(self.d.get("WantedBy"), ["default.target"])

    def test_ordered_after_the_graphical_session(self):
        self.assertIn("graphical-session.target", self.d.get("After", []))

    def test_stops_with_the_graphical_session(self):
        self.assertIn("graphical-session.target", self.d.get("PartOf", []))

    def test_config_is_passed_explicitly(self):
        self.assertEqual(
            self.d.get("ExecStart"),
            ["%h/.local/bin/buswatchd --config %h/.config/buswatchd/config.json"],
        )

    def test_restarts_on_failure(self):
        self.assertEqual(self.d.get("Restart"), ["on-failure"])


if __name__ == "__main__":
    unittest.main()
