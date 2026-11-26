# Apple SuperDrive Helper

Wakes an Apple USB SuperDrive on Windows and Linux so it accepts discs.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/gui-dark.png">
  <img alt="The Apple SuperDrive Helper window on Windows with a disc mounted" src="docs/gui-light.png" width="720">
</picture>

On Windows and Linux the SuperDrive shows up as an ordinary optical drive, but it will not take a disc until it receives a vendor-specific command, `EA 00 00 00 00 00 01`. macOS sends that command automatically. Windows and Linux do not, and the drive needs it again every time it is plugged in. These scripts send it and give you a small window for waking the drive, ejecting discs, and opening them.

## Windows

Tested on Windows 11. Nothing needs to be installed. Download or clone the repository and keep the files in one folder, because both scripts load `SuperDriveNative.cs` from the folder they run from.

### Window

Double-click `AppleSuperDriveGui.cmd` and accept the administrator prompt. Windows only lets administrators send the wake command. If you decline the prompt, the window still opens and shows the drive's status, but it cannot wake the drive.

The window wakes the drive when it opens and again each time the drive is plugged back in. It shows the drive's current state with the next step as the main button: wake the drive, insert a disc, or open the disc in File Explorer. The other buttons are Wake, Eject, and Refresh (F5). If the drive rejects the wake, the window shows the SCSI status and sense code it returned.

Copy diagnostics puts the drive details and recent log on the clipboard for a bug report. The full log is in `%LOCALAPPDATA%\AppleSuperDriveHelper\gui.log`.

### Command line

```powershell
.\Enable-AppleSuperDrive.cmd
.\Enable-AppleSuperDrive.cmd -DriveLetter E
```

This asks for administrator rights, wakes every connected SuperDrive (or only the one at `-DriveLetter`), prints `Awake` or `Rejected` for each, and waits for Enter before closing.

For scripts, run it from an elevated PowerShell. It exits with code 1 if no SuperDrive is found or any drive rejects the wake.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Enable-AppleSuperDrive.ps1 -DriveLetter E
```

## Linux

The Linux version wakes the drive automatically each time it is plugged in, whether or not the window is open. It has not been tested with a real drive yet.

### Install

```bash
sudo bash linux/install.sh
```

This installs:

- `/usr/local/lib/apple-superdrive-helper/superdrive.py`, the command-line helper (Python 3 standard library only)
- `/etc/udev/rules.d/99-apple-superdrive.rules`, which matches the SuperDrive's USB ID `05ac:1500` and, on systemd systems, starts `apple-superdrive-wake@.service` to wake it
- a polkit policy that lets the logged-in user wake the drive from the window without a password
- the window, an `apple-superdrive-helper` launcher, and a "SuperDrive Helper" menu entry

The window needs PySide6. Install it from your distribution (`python3-pyside6.qtwidgets` on Debian and recent Ubuntu, `python3-pyside6` on Fedora, `pyside6` on Arch), or add `--venv` to install it from PyPI into a private virtual environment.

| Option | Effect |
| --- | --- |
| `--venv` | Install PySide6 from PyPI if the system does not have it |
| `--no-gui` | Install only the automatic wake and the command-line helper |
| `--uninstall` | Remove everything the installer added |

### Use

Plug in the drive. It is woken within a second or two. To see the result:

```bash
journalctl -u 'apple-superdrive-wake@*'
```

Open SuperDrive Helper from the applications menu, or run `apple-superdrive-helper`. The window follows the desktop's light or dark theme and logs to `~/.local/state/apple-superdrive-helper/gui.log`.

From a terminal:

```bash
helper=/usr/local/lib/apple-superdrive-helper/superdrive.py
$helper status            # add --json for machine-readable output
sudo $helper wake
$helper eject
$helper open              # mounts the disc if needed and opens it in the file manager
$helper wake --device /dev/sr1
```

Without `--device`, `wake` and `eject` act on every connected SuperDrive and `open` uses the first one.

Waking needs root because Linux only passes vendor-specific SCSI commands from processes with `CAP_SYS_RAWIO`. When started through pkexec, the helper accepts only `wake`, and only for a device whose SCSI inquiry identifies it as an Apple SuperDrive.

To run the window from a checkout without installing, use `python3 linux/superdrive_gui.py`. Without the polkit policy, waking the drive asks for your password.

## Troubleshooting

- Plug the drive directly into a USB port on the computer, without a hub or adapter, when you can.
- If the wake is rejected, try another USB port or cable, then wake the drive again.
- If the drive accepts the wake but still refuses discs, the remaining causes are power, the cable or adapter, or a mechanical fault.

## Tests

```bash
python3 -m unittest discover -s linux/tests
```

The tests cover the Linux code that does not need a drive: the `sg_io_hdr` layout, sense decoding, drive discovery on a fake sysfs tree, mount and udev parsing, wake records, and what the window shows in each state. The window tests are skipped when PySide6 is not installed.

## License

MIT. This is an unofficial tool and is not affiliated with Apple.
