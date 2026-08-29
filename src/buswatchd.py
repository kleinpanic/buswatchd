#!/usr/bin/env python3
"""
buswatchd - udev-based hotplug notifier for USB + DRM (HDMI/DP).

- USB: watches subsystem=usb (add/remove), prefers DEVTYPE=usb_device
- DRM: watches subsystem=drm (change), diffs /sys/class/drm/*/status

Notifications:
- Prefers `notify-send` (libnotify) for portability across notification daemons
  (works with dunst, mako, etc. via the freedesktop notifications spec).
- Falls back to `dunstify` if `notify-send` is not available.

Interactive mode (USB add only):
- Uses notification actions:
  - default: Options (opens rofi/wofi/dmenu)
  - trust: Trust
  - block: Block (tries /sys/.../authorized=0)
  - ignore: Ignore
- With `notify-send`, actions are specified via -A/--action and the selected action
  NAME is output to stdout. If nothing is selected, stdout is empty / no match.

Threading:
- The udev poll loop is the main thread and must never block. Interactive prompts
  (which block for up to notify_timeout_ms, plus a menu) run on a dedicated prompt
  thread, so hotplug events are not dropped while a prompt is on screen and SIGTERM
  is honoured immediately.

State:
- <state_dir>/trusted.json
- <state_dir>/blocked.json
  where <state_dir> is --state-dir, else the config's "state_dir", else the
  directory containing the config file.

De-duplication:
- udev can emit multiple closely-spaced events for the same physical action.
- We debounce USB add/remove notifications by key (identity preferred) within a
  configurable window (default 1200ms).

Important:
- udev "remove" events often lack ID_* properties; we cache device metadata on "add"
  to still show meaningful "remove" notifications. That cache is bounded (LRU).
- Blocking a device writes /sys/.../authorized, which requires privileges a user
  service does not have by default. Enforcement is reported honestly: a device can
  be recorded as blocked without being deauthorized. See usb.block_enforcement.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

try:
    import pyudev
except ImportError as _e:  # pragma: no cover - depends on the host environment
    raise SystemExit(
        "buswatchd: missing required dependency 'pyudev' (%s).\n"
        "Install it with one of:\n"
        "  pip install --user pyudev\n"
        "  pacman -S python-pyudev      # Arch\n"
        "  apt install python3-pyudev   # Debian/Ubuntu" % _e
    )


LOG = logging.getLogger("buswatchd")

# Bound for the add-event metadata cache used to describe remove events.
USB_CACHE_MAX_ENTRIES = 512

# Bound for the queue of pending interactive prompts.
PROMPT_QUEUE_MAX = 16


@dataclass(frozen=True)
class UsbIdentity:
    vid: str
    pid: str
    serial: str

    @property
    def key(self) -> str:
        return f"{self.vid}:{self.pid}:{self.serial}"


@dataclass(frozen=True)
class UsbEvent:
    action: str  # add/remove
    ident: Optional[UsbIdentity]
    display_name: str
    sys_path: str
    device_path: str
    sys_name: str


@dataclass(frozen=True)
class DrmEvent:
    changes: Dict[str, Tuple[str, str]]  # connector -> (old, new)


@dataclass(frozen=True)
class EnforceResult:
    """Outcome of trying to actually deauthorize a USB device."""

    ok: bool
    detail: str


class StopFlag:
    def __init__(self) -> None:
        self._stop = threading.Event()

    def set(self) -> None:
        self._stop.set()

    def is_set(self) -> bool:
        return self._stop.is_set()


class ChildProcs:
    """
    Runs blocking child processes while keeping them terminable.

    Notification prompts and menus block for as long as the user leaves them open.
    Tracking the live children lets shutdown tear them down instead of waiting out
    the full notification timeout.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: set = set()
        self._closed = False

    def run(
        self,
        cmd: list[str],
        *,
        input_bytes: Optional[bytes] = None,
        capture_stdout: bool = False,
    ) -> Optional[str]:
        """
        Run cmd to completion. Returns stdout (text) when capture_stdout is set,
        "" on success otherwise, and None if the child could not be run.
        """
        with self._lock:
            if self._closed:
                return None

        try:
            p = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            LOG.warning("Failed to run %s: %s", cmd[0], e)
            return None

        with self._lock:
            if self._closed:
                _terminate(p)
                return None
            self._procs.add(p)

        try:
            out, _ = p.communicate(input=input_bytes)
        except Exception as e:
            LOG.warning("%s failed: %s", cmd[0], e)
            _terminate(p)
            return None
        finally:
            with self._lock:
                self._procs.discard(p)

        if not capture_stdout:
            return ""
        return (out or b"").decode("utf-8", errors="replace")

    def close(self) -> None:
        """Stop accepting new children and terminate anything still running."""
        with self._lock:
            self._closed = True
            procs = list(self._procs)
        for p in procs:
            _terminate(p)


def _terminate(p: "subprocess.Popen") -> None:
    try:
        if p.poll() is None:
            p.terminate()
    except Exception:
        pass


class PromptWorker:
    """
    Serializes interactive prompts onto a single background thread.

    One prompt at a time is deliberate: a burst of USB events should not stack up
    four rofi menus. Overflow is dropped loudly rather than queued forever.
    """

    def __init__(self, maxsize: int = PROMPT_QUEUE_MAX) -> None:
        self._q: "queue.Queue[Callable[[], None]]" = queue.Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._worker, name="prompt", daemon=True)

    def start(self) -> None:
        self._t.start()

    def submit(self, fn: Callable[[], None]) -> bool:
        if self._stop.is_set():
            return False
        try:
            self._q.put_nowait(fn)
            return True
        except queue.Full:
            LOG.warning("Prompt queue full (%d pending); dropping prompt", self._q.maxsize)
            return False

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._t.is_alive():
            self._t.join(timeout=timeout)

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                fn = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                fn()
            except Exception as e:
                LOG.warning("Interactive prompt failed: %s", e)
            finally:
                self._q.task_done()


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.trusted_path = state_dir / "trusted.json"
        self.blocked_path = state_dir / "blocked.json"
        self._lock = threading.Lock()
        self.trusted = self._load_map(self.trusted_path)
        self.blocked = self._load_map(self.blocked_path)

    def _load_map(self, p: Path) -> Dict[str, Dict[str, Any]]:
        try:
            if p.exists():
                with p.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
        except Exception as e:
            LOG.warning("Failed reading %s: %s", p, e)
        return {}

    def _save_map(self, p: Path, data: Dict[str, Dict[str, Any]]) -> None:
        tmp = p.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        tmp.replace(p)

    def mark_trusted(self, ident: UsbIdentity, meta: Dict[str, Any]) -> None:
        with self._lock:
            self.trusted[ident.key] = {"ts": int(time.time()), **meta}
            self.blocked.pop(ident.key, None)
            self._save_map(self.trusted_path, self.trusted)
            self._save_map(self.blocked_path, self.blocked)

    def mark_blocked(self, ident: UsbIdentity, meta: Dict[str, Any]) -> None:
        with self._lock:
            self.blocked[ident.key] = {"ts": int(time.time()), **meta}
            self.trusted.pop(ident.key, None)
            self._save_map(self.trusted_path, self.trusted)
            self._save_map(self.blocked_path, self.blocked)

    def is_trusted(self, ident: UsbIdentity) -> bool:
        with self._lock:
            return ident.key in self.trusted

    def is_blocked(self, ident: UsbIdentity) -> bool:
        with self._lock:
            return ident.key in self.blocked


class NotificationBackend:
    """
    CLI-based notification backend.

    Preference order:
      1) notify-send (libnotify)
      2) dunstify

    notify-send supports actions via -A/--action and prints the selected action
    NAME to stdout; dunstify uses -A name,label with -b to block.
    """

    def __init__(self, timeout_ms: int, children: ChildProcs) -> None:
        self.timeout_ms = int(timeout_ms)
        self.children = children
        self._notify_send = shutil.which("notify-send")
        self._dunstify = shutil.which("dunstify")

        if self._notify_send:
            self.kind = "notify-send"
        elif self._dunstify:
            self.kind = "dunstify"
        else:
            self.kind = "none"

        LOG.info("Notification backend: %s", self.kind)

    def available(self) -> bool:
        return self.kind != "none"

    def notify(self, summary: str, body: str) -> None:
        if self.kind == "notify-send":
            self.children.run(
                ["notify-send", "-a", "buswatchd", "-t", str(self.timeout_ms), summary, body]
            )
        elif self.kind == "dunstify":
            self.children.run(
                [
                    "dunstify",
                    "-a",
                    "buswatchd",
                    "-t",
                    str(self.timeout_ms),
                    "-c",
                    "device",
                    summary,
                    body,
                ]
            )
        else:
            LOG.error("No notification tool found (need notify-send or dunstify in PATH)")

    def prompt_actions(self, summary: str, body: str, actions: list[tuple[str, str]]) -> Optional[str]:
        """
        Show an actionable notification and return the chosen action key, or None.
        actions: list of (key, label)

        Blocks until the notification is actioned, dismissed or times out, so this
        must not be called from the udev poll loop.
        """
        if self.kind == "notify-send":
            cmd = ["notify-send", "-a", "buswatchd", "-t", str(self.timeout_ms)]
            for key, label in actions:
                cmd += ["-A", f"{key}={label}"]
            cmd += [summary, body]
        elif self.kind == "dunstify":
            cmd = ["dunstify", "-a", "buswatchd", "-t", str(self.timeout_ms), "-c", "device"]
            for key, label in actions:
                cmd += ["-A", f"{key},{label}"]
            cmd += ["-b", summary, body]
        else:
            LOG.error("No notification tool found (need notify-send or dunstify in PATH)")
            return None

        out = self.children.run(cmd, capture_stdout=True)
        if out is None:
            return None

        choice = out.strip()
        allowed = {k for k, _ in actions}
        return choice if choice in allowed else None


class Notifier:
    """
    Simple async notifier for non-interactive notifications.
    Interactive prompts are dispatched to the PromptWorker by the caller.
    """

    def __init__(self, timeout_ms: int, children: ChildProcs) -> None:
        self.backend = NotificationBackend(timeout_ms=timeout_ms, children=children)
        self._q: "queue.Queue[Tuple[str, str]]" = queue.Queue()
        self._t = threading.Thread(target=self._worker, name="notifier", daemon=True)
        self._t.start()

    def notify(self, summary: str, body: str) -> None:
        self._q.put((summary, body))

    def prompt_actions(self, summary: str, body: str, actions: list[tuple[str, str]]) -> Optional[str]:
        return self.backend.prompt_actions(summary, body, actions)

    def _worker(self) -> None:
        while True:
            summary, body = self._q.get()
            try:
                self.backend.notify(summary, body)
            finally:
                self._q.task_done()


class MenuChooser:
    """
    Prefer rofi, then wofi (if Wayland), then dmenu.
    """

    def __init__(self, preference: str, children: ChildProcs) -> None:
        self.preference = (preference or "auto").lower()
        self.children = children

    def _have(self, cmd: str) -> bool:
        return any(os.access(os.path.join(p, cmd), os.X_OK) for p in os.environ.get("PATH", "").split(os.pathsep))

    def _is_wayland(self) -> bool:
        return bool(os.environ.get("WAYLAND_DISPLAY"))

    def build_cmd(self, prompt: str) -> Optional[list[str]]:
        pref = self.preference
        is_wayland = self._is_wayland()

        def rofi_cmd() -> Optional[list[str]]:
            if is_wayland and self._have("rofi-wayland"):
                return ["rofi-wayland", "-dmenu", "-p", prompt]
            if self._have("rofi"):
                return ["rofi", "-dmenu", "-p", prompt]
            return None

        def wofi_cmd() -> Optional[list[str]]:
            if self._have("wofi"):
                return ["wofi", "--dmenu", "-p", prompt]
            return None

        def dmenu_cmd() -> Optional[list[str]]:
            if self._have("dmenu"):
                return ["dmenu", "-p", prompt]
            return None

        if pref == "rofi":
            return rofi_cmd()
        if pref == "wofi":
            return wofi_cmd()
        if pref == "dmenu":
            return dmenu_cmd()

        # auto
        cmd = rofi_cmd()
        if cmd:
            return cmd
        if is_wayland:
            cmd = wofi_cmd()
            if cmd:
                return cmd
        return dmenu_cmd()

    def run(self, prompt: str, options: list[str]) -> Optional[str]:
        cmd = self.build_cmd(prompt)
        if not cmd:
            LOG.warning("No menu program found (need rofi, wofi or dmenu in PATH)")
            return None
        out = self.children.run(
            cmd,
            input_bytes=("\n".join(options) + "\n").encode("utf-8"),
            capture_stdout=True,
        )
        if out is None:
            return None
        return out.strip() or None


class BusWatchd:
    def __init__(
        self,
        cfg: Dict[str, Any],
        state_dir: Path,
        children: Optional[ChildProcs] = None,
        prompts: Optional[PromptWorker] = None,
    ) -> None:
        self.cfg = cfg
        self.children = children if children is not None else ChildProcs()
        self.prompts = prompts if prompts is not None else PromptWorker()
        self.state = StateStore(state_dir)

        self._interactive = bool(cfg.get("interactive", True))
        self._timeout_ms = int(cfg.get("notify_timeout_ms", 30000))

        # USB debounce: suppress repeats within this window
        self._usb_dedupe_ms = int(cfg.get("usb", {}).get("dedupe_window_ms", 1200))
        self._recent_usb: Dict[str, float] = {}  # key -> last monotonic time

        # How hard to try when actually deauthorizing a blocked device.
        self._block_enforcement = str(cfg.get("usb", {}).get("block_enforcement", "direct")).lower()
        if self._block_enforcement not in {"direct", "pkexec", "sudo", "none"}:
            LOG.warning(
                "Unknown usb.block_enforcement %r; falling back to 'direct'",
                self._block_enforcement,
            )
            self._block_enforcement = "direct"

        self.notifier = Notifier(timeout_ms=self._timeout_ms, children=self.children)
        self.menu = MenuChooser(preference=str(cfg.get("menu_preference", "auto")), children=self.children)

        self._drm_status = self._read_drm_status()

        # Cache for remove events (which may not have usable properties).
        # Keyed by sys_path/device_path/sys_name -> stored event info from "add".
        # Bounded LRU: a long-lived daemon must not accumulate an entry per plug.
        self._usb_cache: "OrderedDict[str, Tuple[Optional[UsbIdentity], str]]" = OrderedDict()

    def start(self) -> None:
        self.prompts.start()
        LOG.info(
            "Interactive=%s, block enforcement=%s, dedupe=%dms",
            self._interactive,
            self._block_enforcement,
            self._usb_dedupe_ms,
        )

    def shutdown(self) -> None:
        self.prompts.stop()
        self.children.close()

    def _cache_put(self, key: str, value: Tuple[Optional[UsbIdentity], str]) -> None:
        if not key:
            return
        self._usb_cache[key] = value
        self._usb_cache.move_to_end(key)
        while len(self._usb_cache) > USB_CACHE_MAX_ENTRIES:
            self._usb_cache.popitem(last=False)

    def _cache_usb(self, ev: UsbEvent) -> None:
        value = (ev.ident, ev.display_name)
        for key in (ev.sys_path, ev.device_path, ev.sys_name):
            self._cache_put(key, value)

    def _lookup_usb_cache(self, sys_path: str, device_path: str, sys_name: str) -> Tuple[Optional[UsbIdentity], Optional[str]]:
        for k in (sys_path, device_path, sys_name):
            if k and k in self._usb_cache:
                ident, name = self._usb_cache[k]
                self._usb_cache.move_to_end(k)
                return ident, name
        return None, None

    def _usb_event_key(self, action: str, ident: Optional[UsbIdentity], sys_path: str, device_path: str, sys_name: str) -> str:
        # Prefer stable device identity; else fall back to sysfs paths/names.
        if ident is not None:
            return f"{action}:ident:{ident.key}"
        for k in (sys_path, device_path, sys_name):
            if k:
                return f"{action}:path:{k}"
        return f"{action}:unknown"

    def _usb_should_suppress(self, action: str, ident: Optional[UsbIdentity], sys_path: str, device_path: str, sys_name: str) -> bool:
        window_s = max(0.0, self._usb_dedupe_ms / 1000.0)
        if window_s <= 0:
            return False

        now = time.monotonic()
        key = self._usb_event_key(action, ident, sys_path, device_path, sys_name)
        last = self._recent_usb.get(key)
        if last is not None and (now - last) < window_s:
            return True

        self._recent_usb[key] = now

        # Cheap cleanup to keep the dict from growing without bound
        cutoff = now - (window_s * 8.0)
        if len(self._recent_usb) > 512:
            self._recent_usb = {k: t for k, t in self._recent_usb.items() if t >= cutoff}

        return False

    def _read_drm_status(self) -> Dict[str, str]:
        base = Path("/sys/class/drm")
        out: Dict[str, str] = {}
        if not base.exists():
            return out
        try:
            for p in base.iterdir():
                if not p.is_dir():
                    continue
                name = p.name
                if "-" not in name:
                    continue
                status_path = p / "status"
                if not status_path.exists():
                    continue
                try:
                    status = status_path.read_text(encoding="utf-8").strip()
                except Exception:
                    continue
                if status in {"connected", "disconnected", "unknown"}:
                    out[name] = status
        except Exception as e:
            LOG.debug("DRM status scan failed: %s", e)
        return out

    def _diff_drm(self) -> Optional[DrmEvent]:
        new = self._read_drm_status()
        changes: Dict[str, Tuple[str, str]] = {}

        for k, v in new.items():
            old = self._drm_status.get(k)
            if old is None:
                changes[k] = ("unknown", v)
            elif old != v:
                changes[k] = (old, v)

        for k, old in self._drm_status.items():
            if k not in new:
                changes[k] = (old, "missing")

        self._drm_status = new
        return DrmEvent(changes=changes) if changes else None

    def _usb_identity(self, dev: "pyudev.Device") -> Optional[UsbIdentity]:
        vid = dev.properties.get("ID_VENDOR_ID")
        pid = dev.properties.get("ID_MODEL_ID")

        if vid and pid:
            vid_s = str(vid).strip().lower()
            pid_s = str(pid).strip().lower()
        else:
            try:
                vid_b = dev.attributes.get("idVendor")
                pid_b = dev.attributes.get("idProduct")
                if not vid_b or not pid_b:
                    return None
                vid_s = vid_b.decode("utf-8", errors="replace").strip().lower()
                pid_s = pid_b.decode("utf-8", errors="replace").strip().lower()
            except Exception:
                return None

        serial = (
            dev.properties.get("ID_SERIAL_SHORT")
            or dev.properties.get("ID_SERIAL")
            or dev.properties.get("SERIAL_SHORT")
            or "noserial"
        )
        serial_s = str(serial).strip() or "noserial"
        return UsbIdentity(vid=vid_s, pid=pid_s, serial=serial_s)

    def _usb_name(self, dev: "pyudev.Device") -> str:
        vendor = dev.properties.get("ID_VENDOR_FROM_DATABASE") or dev.properties.get("ID_VENDOR") or ""
        product = dev.properties.get("ID_MODEL_FROM_DATABASE") or dev.properties.get("ID_MODEL") or ""
        name = f"{vendor} {product}".strip()
        return name if name else "USB device"

    def _enforce_block(self, sys_path: str) -> EnforceResult:
        """
        Try to actually deauthorize the device.

        A user service cannot write /sys/bus/usb/devices/*/authorized without help,
        so this reports what really happened instead of implying enforcement.
        """
        if self._block_enforcement == "none":
            return EnforceResult(False, "recorded only (enforcement disabled)")

        if not sys_path:
            return EnforceResult(False, "recorded only (no sysfs path)")

        auth_path = Path(sys_path) / "authorized"
        if not auth_path.exists():
            return EnforceResult(False, "recorded only (no authorized attribute)")

        try:
            auth_path.write_text("0", encoding="utf-8")
            return EnforceResult(True, "deauthorized")
        except PermissionError:
            LOG.info("Cannot write %s directly; trying %s", auth_path, self._block_enforcement)
        except Exception as e:
            LOG.warning("Failed writing %s: %s", auth_path, e)
            return EnforceResult(False, f"recorded only ({e})")

        helper = self._privileged_helper()
        if helper is None:
            LOG.warning(
                "Device recorded as blocked but NOT deauthorized: writing %s needs privileges. "
                "See README (usb.block_enforcement) to enable real enforcement.",
                auth_path,
            )
            return EnforceResult(False, "recorded only (needs root)")

        cmd = helper + ["sh", "-c", f"printf 0 > {shlex.quote(str(auth_path))}"]
        out = self.children.run(cmd)
        if out is None:
            return EnforceResult(False, f"recorded only ({self._block_enforcement} failed)")

        try:
            if auth_path.read_text(encoding="utf-8").strip() == "0":
                return EnforceResult(True, f"deauthorized via {self._block_enforcement}")
        except Exception:
            pass
        return EnforceResult(False, f"recorded only ({self._block_enforcement} did not take effect)")

    def _privileged_helper(self) -> Optional[list[str]]:
        if self._block_enforcement == "pkexec" and shutil.which("pkexec"):
            return ["pkexec"]
        if self._block_enforcement == "sudo" and shutil.which("sudo"):
            return ["sudo", "-n"]
        return None

    def _apply_block(self, ident: UsbIdentity, ev_name: str, sys_path: str, meta: Dict[str, Any]) -> None:
        self.state.mark_blocked(ident, meta)
        result = self._enforce_block(sys_path)
        summary = "USB blocked" if result.ok else "USB block recorded (not enforced)"
        self.notifier.notify(summary, f"{ev_name}\n{ident.key}\n{result.detail}")

    def _run_options_menu(self, prompt: str) -> Optional[str]:
        return self.menu.run(prompt, ["Trust", "Block", "Ignore"])

    def _handle_usb_add(self, ev: UsbEvent) -> None:
        if not self.cfg.get("usb", {}).get("notify_add", True):
            return

        # Cache early so remove event can still show something.
        self._cache_usb(ev)

        # Debounce duplicates
        if self._usb_should_suppress(ev.action, ev.ident, ev.sys_path, ev.device_path, ev.sys_name):
            return

        if ev.ident is None:
            self.notifier.notify(f"USB add: {ev.display_name}", f"{ev.sys_name}\n{ev.sys_path}")
            return

        ident = ev.ident
        meta = {"name": ev.display_name, "sys_path": ev.sys_path}

        # Auto-apply known state first.
        if self.state.is_blocked(ident):
            result = self._enforce_block(ev.sys_path)
            summary = "USB blocked" if result.ok else "USB block recorded (not enforced)"
            self.notifier.notify(
                f"{summary}: {ev.display_name}",
                f"{ident.key}\n(known blocked device) {result.detail}",
            )
            return

        if self.state.is_trusted(ident):
            self.notifier.notify(
                f"USB trusted: {ev.display_name}",
                f"{ident.key}\n(known trusted device)",
            )
            return

        summary = f"USB add: {ev.display_name}"
        body = f"{ident.key}\n{ev.sys_path}"

        if not self._interactive:
            self.notifier.notify(summary, body)
            return

        # The prompt blocks for up to notify_timeout_ms plus however long the menu
        # stays open. Run it off the poll loop so no udev events are missed.
        if not self.prompts.submit(lambda: self._prompt_usb_add(ev, ident, meta, summary, body)):
            self.notifier.notify(summary, body)

    def _prompt_usb_add(
        self,
        ev: UsbEvent,
        ident: UsbIdentity,
        meta: Dict[str, Any],
        summary: str,
        body: str,
    ) -> None:
        """Runs on the prompt thread, never on the udev poll loop."""
        actions = [("default", "Options"), ("trust", "Trust"), ("block", "Block"), ("ignore", "Ignore")]
        choice = self.notifier.prompt_actions(summary, body, actions)
        if not choice:
            return

        if choice == "trust":
            self.state.mark_trusted(ident, meta)
            self.notifier.notify("USB trusted", f"{ev.display_name}\n{ident.key}")
            return

        if choice == "block":
            self._apply_block(ident, ev.display_name, ev.sys_path, meta)
            return

        if choice == "default":
            selection = self._run_options_menu("USB device")
            if selection == "Trust":
                self.state.mark_trusted(ident, meta)
                self.notifier.notify("USB trusted", f"{ev.display_name}\n{ident.key}")
            elif selection == "Block":
                self._apply_block(ident, ev.display_name, ev.sys_path, meta)
            return

        # ignore -> nothing

    def _handle_usb_remove(self, ev: UsbEvent) -> None:
        if not self.cfg.get("usb", {}).get("notify_remove", True):
            return

        ident = ev.ident
        name = ev.display_name

        # Fill from cache if remove event is missing properties.
        cached_ident, cached_name = self._lookup_usb_cache(ev.sys_path, ev.device_path, ev.sys_name)
        if cached_name:
            name = cached_name
        if ident is None and cached_ident is not None:
            ident = cached_ident

        # Debounce duplicates (use cached ident if we got it)
        if self._usb_should_suppress(ev.action, ident, ev.sys_path, ev.device_path, ev.sys_name):
            return

        if ident is not None:
            body = f"{ident.key}\n{ev.sys_name}"
        else:
            body = f"{ev.sys_name}\n{ev.sys_path}"

        self.notifier.notify(f"USB remove: {name}", body)

    def _handle_drm_change(self) -> None:
        if not self.cfg.get("drm", {}).get("notify_changes", True):
            return

        diff = self._diff_drm()
        if not diff:
            return

        for connector, (old, new) in diff.changes.items():
            if old == new:
                continue
            if new == "connected":
                self.notifier.notify(f"Display connected: {connector}", f"{old} -> {new}")
            elif new == "disconnected":
                self.notifier.notify(f"Display disconnected: {connector}", f"{old} -> {new}")
            else:
                self.notifier.notify(f"Display change: {connector}", f"{old} -> {new}")

    def _make_usb_event(self, dev: "pyudev.Device", action: str) -> UsbEvent:
        sys_path = str(getattr(dev, "sys_path", "") or "")
        device_path = str(getattr(dev, "device_path", "") or "")
        sys_name = str(getattr(dev, "sys_name", "") or "")

        ident = self._usb_identity(dev)
        name = self._usb_name(dev)

        if action == "remove":
            _, cached_name = self._lookup_usb_cache(sys_path, device_path, sys_name)
            if cached_name:
                name = cached_name

        return UsbEvent(
            action=action,
            ident=ident,
            display_name=name,
            sys_path=sys_path,
            device_path=device_path,
            sys_name=sys_name,
        )

    def handle_device_event(self, dev: "pyudev.Device") -> None:
        action = str(getattr(dev, "action", None) or dev.properties.get("ACTION") or "").strip()
        subsystem = str(getattr(dev, "subsystem", None) or "").strip()

        if not action or not subsystem:
            return

        if subsystem == "usb":
            devtype = getattr(dev, "device_type", None) or dev.properties.get("DEVTYPE")

            # On remove, DEVTYPE/device_type may be missing. Still handle remove events.
            if devtype == "usb_device" or action == "remove":
                ev = self._make_usb_event(dev, action=action)
                if action == "add":
                    self._handle_usb_add(ev)
                elif action == "remove":
                    self._handle_usb_remove(ev)
                return

        if subsystem == "drm" and action == "change":
            self._handle_drm_change()
            return


def load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"Config must be a JSON object: {path}")
    return data


def resolve_state_dir(cfg: Dict[str, Any], cfg_path: Path, cli_state_dir: Optional[str]) -> Path:
    """
    --state-dir wins, then config "state_dir", then the config file's directory.

    Deriving from the config file keeps `--config somewhere/else.json` self
    consistent: trusted.json and blocked.json land next to the config they belong to.
    """
    if cli_state_dir:
        return Path(cli_state_dir).expanduser()
    from_cfg = cfg.get("state_dir")
    if from_cfg:
        return Path(str(from_cfg)).expanduser()
    return cfg_path.parent


def setup_logging(level: str) -> None:
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    logging.basicConfig(
        level=lvl,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="buswatchd - USB/HDMI hotplug notifier")
    ap.add_argument("--config", type=str, required=True, help="Path to config.json")
    ap.add_argument(
        "--state-dir",
        type=str,
        default=None,
        help="Directory for trusted.json/blocked.json (default: the config file's directory)",
    )
    args = ap.parse_args()

    cfg_path = Path(args.config).expanduser()
    cfg = load_config(cfg_path)

    setup_logging(cfg.get("log_level", "INFO"))
    LOG.info("Starting buswatchd")

    state_dir = resolve_state_dir(cfg, cfg_path, args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    LOG.info("State directory: %s", state_dir)

    daemon = BusWatchd(cfg, state_dir)

    stop = StopFlag()

    def _sig(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    ctx = pyudev.Context()
    mon = pyudev.Monitor.from_netlink(ctx)

    try:
        mon.filter_by(subsystem="usb")
        mon.filter_by(subsystem="drm")
    except Exception as e:
        # Running unfiltered would firehose every udev event through the handler.
        LOG.error("Failed to install udev subsystem filters: %s", e)
        return 1

    mon.start()
    daemon.start()

    try:
        while not stop.is_set():
            try:
                dev = mon.poll(timeout=1)
                if dev is None:
                    continue
                daemon.handle_device_event(dev)
            except Exception as e:
                LOG.warning("Error handling event: %s", e)
    finally:
        LOG.info("Stopping buswatchd")
        daemon.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
