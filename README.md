# Apple SuperDrive Helper

Small Windows and Linux helpers for getting an Apple USB SuperDrive working on non-Apple systems.

This repo exists because some Apple SuperDrives enumerate correctly in Windows and Linux but still refuse to accept discs until they receive a vendor-specific initialization command. These scripts provide that wake step and a native GUI for everyday use. The Linux version is described in [Linux](#linux).

## What It Does

- Detects an attached Apple USB SuperDrive
- Sends the raw SuperDrive initialization command
- Launches a WPF window for wake, eject, opening the disc, and copying diagnostics
- Auto-elevates when admin rights are needed
- Shows drive state, Windows status, and media-loaded state

## Files

- `AppleSuperDriveGui.ps1`: WPF GUI (no install needed; WPF ships with Windows)
- `AppleSuperDriveGui.cmd`: double-click launcher for the GUI
- `Enable-AppleSuperDrive.ps1`: command-line wake helper
- `Enable-AppleSuperDrive.cmd`: double-click launcher for the CLI helper
- `SuperDriveNative.cs`: wake and eject commands shared by both scripts; keep it in the same folder
- `linux/`: the Linux version (helper, Qt window, installer, tests); see [Linux](#linux)

## Usage

### GUI

Run:

```powershell
.\AppleSuperDriveGui.cmd
```

The GUI:

- follows the Windows light or dark app theme, including the title bar
- wakes the drive at startup, and again each time it is plugged back in while the window is open
- shows one clear state (no drive, needs waking, ready for a disc, reading, disc ready, or needs attention) with the next action as the main button
- checks the drive's reply and shows the SCSI status and sense code when it rejects the wake
- runs every drive query and command in the background, so the window never freezes
- has `Wake`, `Eject`, `Refresh` (or F5), and `Open disc` buttons, plus `Copy diagnostics` for bug reports
- keeps an activity log on screen and in `%LOCALAPPDATA%\AppleSuperDriveHelper\gui.log`

If you decline the administrator prompt, the GUI still opens and shows status, but waking the drive needs administrator rights.

### Command Line

Run:

```powershell
.\Enable-AppleSuperDrive.cmd
```

Or:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Enable-AppleSuperDrive.ps1
```

The helper reopens itself as administrator when needed and waits for Enter before that window closes. It prints `Awake` or `Rejected` for each drive and exits with code 1 if any drive rejected the wake. Use `-DriveLetter E` to target one drive.

## Linux

On Linux the drive is woken automatically: a udev rule sends the wake command every time the SuperDrive is plugged in, whether or not any window is open. A Qt window with the same look as the Windows one shows the drive's state and lets you wake it, eject the disc, or open it.

### Install

```bash
sudo bash linux/install.sh
```

This installs:

- `superdrive.py` in `/usr/local/lib/apple-superdrive-helper`, the helper behind everything else (standard-library Python only)
- a udev rule in `/etc/udev/rules.d/99-apple-superdrive.rules` that matches the SuperDrive's USB ID `05ac:1500` and, on systemd systems, starts `apple-superdrive-wake@.service` to send the wake command
- a polkit policy so the window's Wake button works without a password for the person at the computer
- the window, the `apple-superdrive-helper` launcher, and a "SuperDrive Helper" desktop entry

The window needs PySide6. Install it from your distribution (for example `python3-pyside6.qtwidgets` on Debian and recent Ubuntu, `python3-pyside6` on Fedora, `pyside6` on Arch), or run `sudo bash linux/install.sh --venv` to download it from PyPI into a private virtual environment. Use `--no-gui` to install only the automatic wake. Remove everything with `sudo bash linux/install.sh --uninstall`.

### Use

- Plug in the drive. It is woken within a second or two; `journalctl -u 'apple-superdrive-wake@*'` shows the result.
- Open "SuperDrive Helper" from your applications menu, or run `apple-superdrive-helper`. It follows your desktop's light or dark theme, refreshes every few seconds, and logs to `~/.local/state/apple-superdrive-helper/gui.log`.
- From a terminal, run the helper with `status`, `wake` (as root), `eject`, or `open` (mounts the disc if needed and opens it in the file manager). Add `--device /dev/sr1` to pick a drive:

  ```bash
  /usr/local/lib/apple-superdrive-helper/superdrive.py status
  sudo /usr/local/lib/apple-superdrive-helper/superdrive.py wake
  ```

You can also run the window straight from the repository with `python3 linux/superdrive_gui.py`; without the installed polkit policy, waking the drive asks for your password.

The wake command needs root because Linux only passes vendor-specific SCSI commands to processes with `CAP_SYS_RAWIO`. When run as root through pkexec, the helper only accepts `wake`, and only for a device whose SCSI inquiry strings say Apple SuperDrive.

### Tests

```bash
python3 -m unittest discover -s linux/tests
```

The tests cover the parts that need no drive: the `sg_io_hdr` layout, sense decoding, drive discovery on a fake sysfs tree, mount and udev parsing, wake records, and what the window shows for each state (skipped without PySide6).

## Why This Exists

The key wake packet is:

```text
EA 00 00 00 00 00 01
```

Windows already provides the normal optical-drive driver path, but some SuperDrives still need that extra vendor-specific command before the slot-loading mechanism behaves normally.

## Tested Setup

- Windows 11
- Apple USB SuperDrive detected as a standard optical drive
- Microsoft `cdrom` driver path

The Linux version has not yet been run against a real drive. Its unit tests pass and its window has been rendered in each state, but the udev rule, pkexec wake, eject, and open still need a first run on Linux hardware.

## Quality-of-Life Features Already In

- auto-elevation
- startup wake and auto-wake on reconnect in the GUI
- automatic retry when the drive answers the first command with UNIT ATTENTION
- eject through the Windows storage driver, with a wake-and-retry if the drive ignores it
- light and dark themes
- manual wake, refresh, open-disc, and eject actions
- copy-to-clipboard diagnostics
- background status refresh
- timestamped log output, also written to a log file

## Good Next Improvements

- Add a tray mode that wakes the drive silently on launch
- Add a retry button that sends alternate init sequences if needed
- Add a packaged `.exe` release for users who do not want to run PowerShell scripts directly
- Add per-user settings for auto-start, minimize-on-launch, and silent mode

## Notes

- Use a direct USB connection when possible.
- This is an unofficial community workaround, not an Apple-supported Windows utility.
- If the drive is detected but still fails after wake, the remaining possibilities are power, cable/adapter behavior, or a mechanical issue.

## License

MIT
