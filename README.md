# SuperDrive Helper

A Linux GUI and CLI for slot‑loading Apple USB SuperDrive drives — and any
other optical drive that speaks SCSI through `sg3_utils`.

Apple's slot‑loading SuperDrive (USB ID `05ac:1500`) refuses to spin up under
Linux until it receives a vendor‑specific "wake" command. SuperDrive Helper
sends that command for you, then exposes the everyday operations — eject,
lock/unlock door, status, autosuspend — through a small PySide6 GUI **and** a
scriptable Typer CLI.

```
sg_raw /dev/srX EA 00 00 00 00 00 01   # the magic Apple wake CDB
```

---

## Features

- **Apple wake**: sends the Apple‑specific `0xEA` CDB so the drive accepts a disc
- **Auto‑detection**: enumerates `/dev/sr*` and tags Apple SuperDrives by USB ID
- **Eject with auto‑unmount**: unmounts via `udisksctl` or `umount` before ejecting
- **Door lock / unlock**: SCSI Prevent/Allow Medium Removal — useful during burns
- **Status & inquiry**: Test Unit Ready plus `sg_inq` summary when available
- **USB autosuspend toggle**: keep flaky slot‑loaders from browning out
- **Stable CLI**: JSON output, dry‑run, fixed exit codes — built for agents and scripts
- **Safe by construction**: fixed `PATH`, no shell, per‑command timeouts, hotplug rechecks

---

## Requirements

| Component | Notes |
|---|---|
| Python | 3.10 – 3.12 |
| `sg3_utils` | `sudo apt install sg3-utils` |
| PySide6 (Qt) | for the GUI only — CLI works without it |
| Access to `/dev/srX` | usually means being in the `cdrom` group |

---

## Install

### Conda (recommended)

```bash
conda env create -f environment.yml
conda activate superdrive
sudo apt install sg3-utils   # system package, not on conda-forge
```

### pip

```bash
python3 -m pip install --user PySide6 typer
sudo apt install sg3-utils
```

### Development install

```bash
python3 -m pip install -e ".[dev]"
```

---

## Run

**GUI**

```bash
python3 -m superdrive.gui.main_window   # or, once installed:
superdrive-helper
```

**CLI**

```bash
superdrive-cli --version
superdrive-cli device list --json
superdrive-cli device status --json --device /dev/sr0
```

---

## CLI for scripts and agents

Reads are non‑mutating. Hardware‑changing commands (`load`, `eject`, `lock`,
`power`) require **either** `--dry-run` **or** `--yes`, so a script can
validate intent before touching the drive.

```bash
# Dry-run first
superdrive-cli device load --dry-run --json --device /dev/sr0

# Actually do it
superdrive-cli device load --yes    --json --device /dev/sr0
```

In `--json` mode, success payloads go to **stdout**; failures go to **stderr** as:

```json
{"error": {"message": "...", "hint": "...", "details": {}}}
```

Exit codes (stable):

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | generic operation failure |
| `2` | config / input error |
| `3` | not found |

Run `superdrive-cli schema describe --json` to dump the live command contract.

---

## GUI buttons, briefly

| Button | What it does |
|---|---|
| **Load / Accept Disc** | Sends Apple CDB `0xEA 00 00 00 00 00 01`. Only enabled for `05ac:1500`. |
| **Eject (Auto‑unmount)** | Unmounts (udisksctl → umount), then `sg_start --eject` (falls back to `eject`). |
| **Allow Eject (Unlock)** | Clears SCSI Prevent/Allow — door can open. |
| **Prevent Eject (Lock)** | Sets SCSI Prevent/Allow — door stays shut (use during burns). |
| **Check Status** | Mount state + Test Unit Ready + `sg_inq` summary if installed. |
| **Keep Port Powered** | Disables USB autosuspend on the underlying port via sysfs. Temporary; needs sudo. |

A modal "Working" dialog and a status‑bar progress bar appear during actions,
and other controls are disabled to prevent overlapping commands.

---

## Permissions

If you see permission errors on `/dev/srX`:

```bash
sudo usermod -aG cdrom "$USER"
newgrp cdrom
```

Don't run the whole GUI as root. The **Keep Port Powered** action writes to
sysfs and may need elevation — the app prints the exact commands to copy, or
you can install a local udev rule.

---

## Architecture

Strict layering — the CLI and drive layers do not import from `gui`:

```
superdrive/
  commands.py            subprocess adapter for sg3_utils, mount, holders
  drive.py               device model, discovery service, controller facade
  cli/                   Typer command surface split by resource
  gui/                   Qt widgets and compact styling
```

Safety guarantees baked in:
- External commands resolve from a fixed safe `PATH` (`/usr/sbin:/usr/bin:/sbin:/bin`)
- Shell execution is disabled everywhere
- Every command has a timeout
- Before each hardware action, the selected `/dev/srX` is re‑checked against
  sysfs so hotplug or device reuse cannot silently target the wrong drive

---

## Development

```bash
ruff check .
pytest -q
python3 -m py_compile $(find . -name '*.py' -not -path './.venv/*')
```

---

## Packaging a single binary

```bash
python3 -m pip install --user pyinstaller
pyinstaller --onefile --windowed --name SuperDriveHelper \
  superdrive/gui/main_window.py
# binary in dist/
```

---

## Notes

- The Apple CDB path is gated to USB ID `05ac:1500` — it will not be sent to
  other drives, even by mistake.
- USB autosuspend toggling is **temporary**: it does not persist across
  unplug or reboot. For a permanent fix, add a udev rule.
- The GUI uses a compact dark Qt stylesheet in `superdrive/gui/theme/style.qss`.
