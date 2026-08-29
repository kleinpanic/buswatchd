"""Shared test helpers: load src/buswatchd.py as a module and build test doubles."""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "buswatchd.py"


def _load():
    if "buswatchd" in sys.modules:
        return sys.modules["buswatchd"]
    spec = importlib.util.spec_from_file_location("buswatchd", _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["buswatchd"] = mod
    spec.loader.exec_module(mod)
    return mod


buswatchd = _load()

# Keep expected warnings out of the test output.
buswatchd.LOG.addHandler(logging.NullHandler())
buswatchd.LOG.propagate = False


class FakeNotifier:
    """Records notifications instead of shelling out to notify-send."""

    def __init__(self, *args, **kwargs) -> None:
        self.sent = []
        self.prompts = []
        self.prompt_result = None

    def notify(self, summary, body) -> None:
        self.sent.append((summary, body))

    def prompt_actions(self, summary, body, actions):
        self.prompts.append((summary, body, actions))
        return self.prompt_result

    def summaries(self):
        return [s for s, _ in self.sent]


class RecordingPrompts:
    """PromptWorker stand-in that records submissions without running them."""

    def __init__(self, accept: bool = True) -> None:
        self.submitted = []
        self.accept = accept
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self, timeout: float = 2.0) -> None:
        self.stopped = True

    def submit(self, fn) -> bool:
        self.submitted.append(fn)
        return self.accept

    def run_pending(self) -> None:
        pending, self.submitted = self.submitted, []
        for fn in pending:
            fn()


class RecordingChildren:
    """ChildProcs stand-in that records commands and replays canned stdout."""

    def __init__(self, stdout: str = "") -> None:
        self.calls = []
        self.stdout = stdout
        self.closed = False

    def run(self, cmd, *, input_bytes=None, capture_stdout=False):
        self.calls.append((list(cmd), input_bytes))
        return self.stdout if capture_stdout else ""

    def close(self) -> None:
        self.closed = True


def make_daemon(state_dir, cfg=None, notifier=None, prompts=None, children=None):
    """Build a BusWatchd wired to test doubles, with no real subprocesses."""
    cfg = {} if cfg is None else cfg
    notifier = FakeNotifier() if notifier is None else notifier
    prompts = RecordingPrompts() if prompts is None else prompts
    children = RecordingChildren() if children is None else children

    real_notifier = buswatchd.Notifier
    buswatchd.Notifier = lambda **kw: notifier
    try:
        daemon = buswatchd.BusWatchd(cfg, Path(state_dir), children=children, prompts=prompts)
    finally:
        buswatchd.Notifier = real_notifier

    daemon.test_notifier = notifier
    daemon.test_prompts = prompts
    daemon.test_children = children
    return daemon


def usb_event(action="add", ident=None, name="Test Device", sys_path="/sys/x", device_path="/devices/x", sys_name="1-1"):
    return buswatchd.UsbEvent(
        action=action,
        ident=ident,
        display_name=name,
        sys_path=sys_path,
        device_path=device_path,
        sys_name=sys_name,
    )


def identity(vid="1234", pid="5678", serial="ABC"):
    return buswatchd.UsbIdentity(vid=vid, pid=pid, serial=serial)
