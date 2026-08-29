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
  NAME is output to stdout.  (If nothing is selected, stdout is empty / no match.)

State:
- ~/.config/buswatchd/trusted.json
- ~/.config/buswatchd/blocked.json

De-duplication:
- udev can emit multiple closely-spaced events for the same physical action.
- We debounce USB add/remove notifications by key (identity preferred) within a
  configurable window (default 1200ms).

Important:
- udev "remove" events often lack ID_* properties; we cache device metadata on "add"
  to still show meaningful "remove" notifications.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


import pyudev


LOG = logging.getLogger("buswatchd")


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


class StopFlag:
    def __init__(self) -> None:
        self._stop = threading.Event()

    def set(self) -> None:
        self._stop.set()

    def is_set(self) -> bool:
        return self._stop.is_set()


class StateStore:
    def __init__(self, config_dir: Path) -> None:
        self.trusted_path = config_dir / "trusted.json"
        self.blocked_path = config_dir / "blocked.json"
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

    notify-send supports actions and prints selected action NAME to stdout.  :contentReference[oaicite:1]{index=1}
    """

    def __init__(self, timeout_ms: int) -> None:
        self.timeout_ms = int(timeout_ms)
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
            self._notify_via_notify_send(summary, body)
        elif self.kind == "dunstify":
            self._notify_via_dunstify(summary, body)
        else:
            LOG.error("No notification tool found (need notify-send or dunstify in PATH)")

    def prompt_actions(self, summary: str, body: str, actions: list[tuple[str, str]]) -> Optional[str]:
        """
        Show an actionable notification and return the chosen action key, or None.
        actions: list of (key, label)
        """
        if self.kind == "notify-send":
            return self._prompt_via_notify_send(summary, body, actions)
        if self.kind == "dunstify":
            return self._prompt_via_dunstify(summary, body, actions)
        LOG.error("No notification tool found (need notify-send or dunstify in PATH)")
        return None

    def _notify_via_notify_send(self, summary: str, body: str) -> None:
        cmd = [
            "notify-send",
            "-a",
            "buswatchd",
            "-t",
            str(self.timeout_ms),
            summary,
            body,
        ]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except Exception as e:
            LOG.warning("notify-send failed: %s", e)

    def _prompt_via_notify_send(self, summary: str, body: str, actions: list[tuple[str, str]]) -> Optional[str]:
        # notify-send: -A, --action=[NAME=]Text (repeatable). Selected NAME printed to stdout. :contentReference[oaicite:2]{index=2}
        cmd = ["notify-send", "-a", "buswatchd", "-t", str(self.timeout_ms)]
        for key, label in actions:
            # NAME=Text
            cmd += ["-A", f"{key}={label}"]
        cmd += [summary, body]

        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        except Exception as e:
            LOG.warning("notify-send (actions) failed: %s", e)
            return None

        out = p.stdout.decode("utf-8", errors="replace").strip()
        allowed = {k for k, _ in actions}
        return out if out in allowed else None

    def _notify_via_dunstify(self, summary: str, body: str) -> None:
        cmd = ["dunstify", "-a", "buswatchd", "-t", str(self.timeout_ms), "-c", "device", summary, body]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except Exception as e:
            LOG.warning("dunstify failed: %s", e)

    def _prompt_via_dunstify(self, summary: str, body: str, actions: list[tuple[str, str]]) -> Optional[str]:
        # dunstify: -A name,label and -b blocks; stdout is action name.
        cmd = ["dunstify", "-a", "buswatchd", "-t", str(self.timeout_ms), "-c", "device"]
        for key, label in actions:
            cmd += ["-A", f"{key},{label}"]
        cmd += ["-b", summary, body]

        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        except Exception as e:
            LOG.warning("dunstify (actions) failed: %s", e)
            return None

        out = p.stdout.decode("utf-8", errors="replace").strip()
        allowed = {k for k, _ in actions}
        return out if out in allowed else None


class Notifier:
    """
    Simple async notifier for non-interactive notifications.
    Interactive prompts are handled synchronously via prompt_actions().
    """

    def __init__(self, timeout_ms: int) -> None:
        self.backend = NotificationBackend(timeout_ms=timeout_ms)
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

    def __init__(self, preference: str) -> None:
        self.preference = (preference or "auto").lower()

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
            return None
        try:
            p = subprocess.run(
                cmd,
                input=("\n".join(options) + "\n").encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            choice = p.stdout.decode("utf-8", errors="replace").strip()
            return choice or None
        except Exception as e:
            LOG.warning("Menu failed: %s", e)
            return None


class BusWatchd:
    def __init__(self, cfg: Dict[str, Any], config_dir: Path) -> None:
        self.cfg = cfg
        self.state = StateStore(config_dir)

        self._interactive = bool(cfg.get("interactive", True))
        self._timeout_ms = int(cfg.get("notify_timeout_ms", 30000))

        # USB debounce: suppress repeats within this window
        self._usb_dedupe_ms = int(cfg.get("usb", {}).get("dedupe_window_ms", 1200))
        self._recent_usb: Dict[str, float] = {}  # key -> last monotonic time

        self.notifier = Notifier(timeout_ms=self._timeout_ms)
        self.menu = MenuChooser(preference=str(cfg.get("menu_preference", "auto")))

        self._drm_status = self._read_drm_status()

        # Cache for remove events (which may not have usable properties)
        # Keyed by sys_path/device_path/sys_name -> stored event info from "add"
        self._usb_cache: Dict[str, Tuple[Optional[UsbIdentity], str]] = {}

    def _cache_usb(self, ev: UsbEvent) -> None:
        self._usb_cache[ev.sys_path] = (ev.ident, ev.display_name)
        if ev.device_path:
            self._usb_cache[ev.device_path] = (ev.ident, ev.display_name)
        if ev.sys_name:
            self._usb_cache[ev.sys_name] = (ev.ident, ev.display_name)

    def _lookup_usb_cache(self, sys_path: str, device_path: str, sys_name: str) -> Tuple[Optional[UsbIdentity], Optional[str]]:
        for k in (sys_path, device_path, sys_name):
            if k and k in self._usb_cache:
                ident, name = self._usb_cache[k]
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

    def _usb_identity(self, dev: pyudev.Device) -> Optional[UsbIdentity]:
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

    def _usb_name(self, dev: pyudev.Device) -> str:
        vendor = dev.properties.get("ID_VENDOR_FROM_DATABASE") or dev.properties.get("ID_VENDOR") or ""
        product = dev.properties.get("ID_MODEL_FROM_DATABASE") or dev.properties.get("ID_MODEL") or ""
        name = f"{vendor} {product}".strip()
        return name if name else "USB device"

    def _try_set_usb_authorized(self, sys_path: str, authorized: bool) -> bool:
        auth_path = Path(sys_path) / "authorized"
        if not auth_path.exists():
            return False
        try:
            auth_path.write_text("1" if authorized else "0", encoding="utf-8")
            return True
        except PermissionError:
            LOG.warning("Permission denied writing %s (need elevated privileges to enforce block/allow)", auth_path)
            return False
        except Exception as e:
            LOG.warning("Failed writing %s: %s", auth_path, e)
            return False

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

        # Auto-apply known state first.
        if self.state.is_blocked(ev.ident):
            self._try_set_usb_authorized(ev.sys_path, authorized=False)
            self.notifier.notify(
                f"USB blocked: {ev.display_name}",
                f"{ev.ident.key}\n(known blocked device)",
            )
            return

        if self.state.is_trusted(ev.ident):
            self.notifier.notify(
                f"USB trusted: {ev.display_name}",
                f"{ev.ident.key}\n(known trusted device)",
            )
            return

        summary = f"USB add: {ev.display_name}"
        body = f"{ev.ident.key}\n{ev.sys_path}"

        if not self._interactive:
            self.notifier.notify(summary, body)
            return

        actions = [("default", "Options"), ("trust", "Trust"), ("block", "Block"), ("ignore", "Ignore")]
        choice = self.notifier.prompt_actions(summary, body, actions)
        if not choice:
            return

        meta = {"name": ev.display_name, "sys_path": ev.sys_path}

        if choice == "trust":
            self.state.mark_trusted(ev.ident, meta)
            self.notifier.notify("USB trusted", f"{ev.display_name}\n{ev.ident.key}")
            return

        if choice == "block":
            self.state.mark_blocked(ev.ident, meta)
            self._try_set_usb_authorized(ev.sys_path, authorized=False)
            self.notifier.notify("USB blocked", f"{ev.display_name}\n{ev.ident.key}")
            return

        if choice == "default":
            selection = self._run_options_menu("USB device")
            if selection == "Trust":
                self.state.mark_trusted(ev.ident, meta)
                self.notifier.notify("USB trusted", f"{ev.display_name}\n{ev.ident.key}")
            elif selection == "Block":
                self.state.mark_blocked(ev.ident, meta)
                self._try_set_usb_authorized(ev.sys_path, authorized=False)
                self.notifier.notify("USB blocked", f"{ev.display_name}\n{ev.ident.key}")
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

    def _make_usb_event(self, dev: pyudev.Device, action: str) -> UsbEvent:
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

    def handle_device_event(self, dev: pyudev.Device) -> None:
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
    args = ap.parse_args()

    cfg_path = Path(args.config).expanduser()
    cfg = load_config(cfg_path)

    setup_logging(cfg.get("log_level", "INFO"))
    LOG.info("Starting buswatchd")

    config_dir = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "buswatchd"
    config_dir.mkdir(parents=True, exist_ok=True)

    daemon = BusWatchd(cfg, config_dir)

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
    except Exception:
        pass

    mon.start()

    while not stop.is_set():
        try:
            dev = mon.poll(timeout=1)
            if dev is None:
                continue
            daemon.handle_device_event(dev)
        except Exception as e:
            LOG.warning("Error handling event: %s", e)

    LOG.info("Stopping buswatchd")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

