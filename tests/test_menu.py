import unittest

from context import RecordingChildren, buswatchd


class FakeMenu(buswatchd.MenuChooser):
    """MenuChooser with a scripted view of PATH and the session type."""

    def __init__(self, preference, present, wayland=False, children=None):
        super().__init__(preference, children or RecordingChildren())
        self._present = set(present)
        self._wayland = wayland

    def _have(self, cmd):
        return cmd in self._present

    def _is_wayland(self):
        return self._wayland


class TestMenuSelection(unittest.TestCase):
    def test_auto_prefers_rofi(self):
        cmd = FakeMenu("auto", {"rofi", "wofi", "dmenu"}).build_cmd("p")
        self.assertEqual(cmd[0], "rofi")

    def test_auto_falls_back_to_dmenu(self):
        cmd = FakeMenu("auto", {"dmenu"}).build_cmd("p")
        self.assertEqual(cmd[0], "dmenu")

    def test_wayland_prefers_rofi_wayland(self):
        cmd = FakeMenu("auto", {"rofi-wayland", "rofi"}, wayland=True).build_cmd("p")
        self.assertEqual(cmd[0], "rofi-wayland")

    def test_wayland_falls_back_to_wofi_before_dmenu(self):
        cmd = FakeMenu("auto", {"wofi", "dmenu"}, wayland=True).build_cmd("p")
        self.assertEqual(cmd[0], "wofi")

    def test_x11_skips_wofi(self):
        cmd = FakeMenu("auto", {"wofi", "dmenu"}, wayland=False).build_cmd("p")
        self.assertEqual(cmd[0], "dmenu")

    def test_explicit_preference_is_not_substituted(self):
        self.assertIsNone(FakeMenu("wofi", {"rofi", "dmenu"}).build_cmd("p"))

    def test_no_menu_program_yields_none(self):
        self.assertIsNone(FakeMenu("auto", set()).build_cmd("p"))

    def test_prompt_is_passed_through(self):
        self.assertIn("USB device", FakeMenu("auto", {"dmenu"}).build_cmd("USB device"))


class TestMenuRun(unittest.TestCase):
    def test_options_are_fed_on_stdin_and_choice_is_stripped(self):
        children = RecordingChildren(stdout="Trust\n")
        menu = FakeMenu("auto", {"dmenu"}, children=children)

        self.assertEqual(menu.run("USB device", ["Trust", "Block", "Ignore"]), "Trust")

        _, stdin = children.calls[0]
        self.assertEqual(stdin, b"Trust\nBlock\nIgnore\n")

    def test_empty_selection_is_none(self):
        menu = FakeMenu("auto", {"dmenu"}, children=RecordingChildren(stdout="\n"))
        self.assertIsNone(menu.run("p", ["Trust"]))

    def test_no_menu_program_does_not_shell_out(self):
        children = RecordingChildren()
        menu = FakeMenu("auto", set(), children=children)
        self.assertIsNone(menu.run("p", ["Trust"]))
        self.assertEqual(children.calls, [])


if __name__ == "__main__":
    unittest.main()
