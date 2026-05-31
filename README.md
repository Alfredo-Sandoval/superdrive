# SuperDrive Helper

![Python](https://img.shields.io/badge/python-3.10--3.12-blue)
![GUI](https://img.shields.io/badge/gui-PySide6-41cd52)
![CLI](https://img.shields.io/badge/cli-Typer-0f766e)
![License](https://img.shields.io/badge/license-MIT-green)

Linux support for the Apple SuperDrive.

SuperDrive Helper is a small GUI and CLI for using Apple's USB SuperDrive on
Linux.

The slot-loading Apple SuperDrive (USB ID `05ac:1500`) does not fully initialize
as a normal USB optical drive on Linux. It can appear as `/dev/sr0`, but still
ignore inserted discs until it receives an Apple-specific SCSI command. macOS
sends this command automatically. On Linux, it has to be sent explicitly:

```sh
sg_raw /dev/srX EA 00 00 00 00 00 01
```

SuperDrive Helper sends that command for supported Apple drives and provides
the related controls that are useful afterward:

- Detect optical drives and identify Apple SuperDrives by USB ID.
- Send the Apple wake command so the drive accepts a disc.
- Eject discs, with an auto-unmount step before eject.
- Lock or unlock the drive door during burns.
- Check drive status and probe details.
- Disable USB autosuspend for the drive's port when Linux power management gets
  in the way.

The GUI is built with PySide6. The CLI is built with Typer and supports JSON
output for scripts.

![SuperDrive Helper GUI](docs/screenshot.png)

## Install

From the repo root:

```bash
sudo apt install sg3-utils
python -m pip install .
```

`sg3-utils` provides the low-level SCSI tools used to talk to the drive.

If you prefer conda:

```bash
conda env create -f environment.yml
conda activate superdrive
sudo apt install sg3-utils   # not on conda-forge
python -m pip install .
```

Most Linux systems require your user to be in the `cdrom` group before it can
access `/dev/sr0` directly:

```bash
sudo usermod -aG cdrom "$USER"
newgrp cdrom
```

## GUI

Launch it with:

```bash
superdrive-helper
```

The main controls are:

- **Load / Accept Disc** sends the Apple wake command. This is only enabled
  when the selected drive is the Apple SuperDrive USB ID `05ac:1500`.
- **Eject** unmounts mounted media first, then ejects the disc.
- **Prevent Eject** sets SCSI Prevent/Allow Medium Removal so the door can't
  pop open during a burn. **Allow Eject** clears it.
- **Keep Port Powered** disables USB autosuspend on the parent port via sysfs.
  Requires sudo and resets on unplug; add a udev rule if you want it permanent.

While a command runs, the UI blocks overlapping actions and shows a modal
"Working" dialog.

## CLI

Read-only commands can run directly:

```bash
superdrive-cli device list
superdrive-cli device status --device /dev/sr0
superdrive-cli device probe  --device /dev/sr0
```

Commands that change hardware state require either `--yes` or `--dry-run`.
This prevents scripts from changing drive state by accident:

```bash
superdrive-cli device load  --yes --device /dev/sr0
superdrive-cli device eject --yes --device /dev/sr0
superdrive-cli device lock  --yes --prevent --device /dev/sr0
superdrive-cli device lock  --yes --allow   --device /dev/sr0
```

Add `--json` for a structured stdout payload. Failures go to stderr as
`{"error": {"message": ..., "hint": ..., "details": {...}}}`. Exit codes are
stable:

| code | meaning           |
|------|-------------------|
| 0    | success           |
| 1    | operation failed  |
| 2    | bad input/config  |
| 3    | device not found  |

`superdrive-cli schema describe --json` dumps the full command contract for
scripting.

## udev rule (optional)

The GUI and CLI let you wake the drive manually. If you want the drive to be
woken automatically when it is plugged in, install the included udev rule:

```bash
sudo cp udev/99-apple-superdrive.rules /etc/udev/rules.d/
sudo udevadm control --reload
```

Replug the drive after installing the rule. The rule sends the Apple wake
command and disables autosuspend for that port.

## Development

For local work:

```bash
pip install -e ".[dev]"
ruff check .
pytest -q
```
