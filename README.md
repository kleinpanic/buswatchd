# buswatchd

A small udev-based hotplug notifier for Linux. It watches the `usb` and `drm`
subsystems and sends desktop notifications when you plug something in, pull
something out, or connect a display.

USB devices can be **trusted** or **blocked** interactively from the
notification itself, and those decisions persist across reboots.

Runs as a systemd **user** service. One Python file, one dependency.

## Requirements

- Linux with udev and systemd
- Python 3.9+
- [`pyudev`](https://pyudev.readthedocs.io/) (`requirements.txt`)
- A notification daemon plus `notify-send` (preferred) or `dunstify`
- Optional, for the "Options" menu: `rofi`, `rofi-wayland`, `wofi`, or `dmenu`

## Install

```sh
pip install --user -r requirements.txt
make install
```

`make install` verifies the dependency first, copies the daemon to
`~/.local/bin/buswatchd`, installs the user unit, seeds
`~/.config/buswatchd/config.json` (never overwriting an existing one), and
enables the service.

```sh
make status     # systemctl --user status
make logs       # last 200 journal lines
make test       # run the test suite
make check      # dependency + syntax preflight
make uninstall  # remove binary and unit, keep config and state
```

## Configuration

`~/.config/buswatchd/config.json`:

| Key | Default | Meaning |
| --- | --- | --- |
| `notify_timeout_ms` | `30000` | Notification lifetime, and how long an interactive prompt waits |
| `interactive` | `true` | Offer Trust/Block/Ignore actions on USB add |
| `menu_preference` | `"auto"` | `auto`, `rofi`, `wofi`, or `dmenu` |
| `log_level` | `"INFO"` | Standard Python log level |
| `state_dir` | config's directory | Where `trusted.json` / `blocked.json` live |
| `usb.notify_add` | `true` | Notify on USB add |
| `usb.notify_remove` | `true` | Notify on USB remove |
| `usb.dedupe_window_ms` | `1200` | Debounce window; udev emits bursts per physical action |
| `usb.block_enforcement` | `"direct"` | How hard to try to deauthorize a blocked device (below) |
| `drm.notify_changes` | `true` | Notify on display connect/disconnect |

State lives next to the config by default, so `--config /somewhere/else.json`
keeps its `trusted.json` and `blocked.json` in `/somewhere`. Override with
`--state-dir` or the `state_dir` key.

## Blocking, and what it actually does

Blocking a USB device does two separate things:

1. **Records** the device identity (`vid:pid:serial`) in `blocked.json`, so it
   is recognized and re-blocked on every future plug-in. This always works.
2. **Deauthorizes** it by writing `0` to `/sys/bus/usb/devices/*/authorized`.
   This needs privileges a user service does not have by default.

When step 2 fails, the notification says **"USB block recorded (not enforced)"**
rather than claiming success. `usb.block_enforcement` picks the strategy:

- `"direct"` (default) — write the sysfs attribute directly. Succeeds only if
  `authorized` is writable by your user; otherwise the block is recorded but
  not enforced.
- `"pkexec"` — fall back to `pkexec`, which raises a polkit authentication
  prompt. Interactive, but needs no standing privilege grant.
- `"sudo"` — fall back to `sudo -n`, which requires a passwordless sudoers
  rule. Note that a `NOPASSWD` rule broad enough to cover this is effectively a
  grant of full root; prefer `pkexec`.
- `"none"` — never touch sysfs; record the decision only.

To make `"direct"` work without a privilege helper, make the attribute
group-writable with a udev rule along these lines (adapt the group to your
distro — `plugdev` on Debian, `wheel`/`users` elsewhere) and re-plug the device:

```
# /etc/udev/rules.d/99-buswatchd-authorized.rules
ACTION=="add", SUBSYSTEM=="usb", RUN+="/bin/sh -c 'chgrp plugdev /sys%p/authorized; chmod g+w /sys%p/authorized'"
```

This is a starting point, not a tested drop-in; verify with `udevadm test` on
your own system before relying on it.

## Design notes

- **The udev poll loop never blocks.** Interactive prompts block for as long as
  the notification stays on screen, so they run on a dedicated prompt thread.
  A blocked poll loop drops hotplug events (the netlink buffer overflows) and
  delays `SIGTERM` by the full notification timeout.
- **Prompts are serialized, one at a time.** A burst of USB events should not
  stack up four menus. Overflow past 16 pending prompts is dropped with a log
  line and degraded to a plain notification.
- **Remove events are matched against an add-time cache.** udev `remove` events
  usually lack `ID_*` properties, so metadata captured on `add` is what makes a
  remove notification readable. That cache is a bounded LRU.
- **udev filters are mandatory.** If installing the subsystem filters fails the
  daemon exits rather than running unfiltered and handling every event on the
  system.

## Layout

```
config/config.json          default config
src/buswatchd.py            the daemon
systemd/buswatchd.service   user unit
tests/                      unittest suite, standard library only
Makefile                    install / uninstall / test / check
```

## Tests

```sh
make test
```

Or directly: `python3 -m unittest discover -s tests -t tests`. The suite covers
the state store, dedupe, the bounded cache, DRM diffing, config and state-dir
resolution, menu selection, block-enforcement reporting, and the off-thread
prompt behaviour. No test needs real hardware or a notification daemon.

## License

MIT. See [LICENSE](LICENSE).
