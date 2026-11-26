# Apple SuperDrive Helper

Small Windows helpers for getting an Apple USB SuperDrive working on non-Apple systems.

This repo exists because some Apple SuperDrives enumerate correctly in Windows but still refuse to accept discs until they receive a vendor-specific initialization command. These scripts provide that wake step and a native GUI for everyday use.

## What It Does

- Detects an attached Apple USB SuperDrive
- Sends the raw SuperDrive initialization command
- Launches a native Windows GUI for refresh, wake, eject, drive open, and diagnostics copy
- Auto-elevates when admin rights are needed
- Shows drive state, Windows status, and media-loaded state

## Files

- `AppleSuperDriveGui.ps1`: native WinForms GUI
- `AppleSuperDriveGui.cmd`: double-click launcher for the GUI
- `Enable-AppleSuperDrive.ps1`: command-line wake helper
- `Enable-AppleSuperDrive.cmd`: double-click launcher for the CLI helper

## Usage

### GUI

Run:

```powershell
.\AppleSuperDriveGui.cmd
```

The GUI:

- sends a startup wake command when the drive is connected but idle
- shows grouped drive and disc status with stronger ready/not-ready states
- lets you manually `Wake/Enable`, `Refresh`, `Open Drive`, and `Eject`
- lets you `Copy Diagnostics` for bug reports or sharing results
- shows a rolling activity log

### Command Line

Run:

```powershell
.\Enable-AppleSuperDrive.cmd
```

Or:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Enable-AppleSuperDrive.ps1
```

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

## Quality-of-Life Features Already In

- auto-elevation
- startup wake in the GUI
- grouped drive and media panels
- manual wake, refresh, open-drive, and eject actions
- copy-to-clipboard diagnostics
- periodic status refresh
- timestamped log output

## Good Next Improvements

- Add a tray mode that wakes the drive silently on launch
- Add device arrival monitoring and auto-wake on reconnect
- Add a retry button that sends alternate init sequences if needed
- Add a packaged `.exe` release for users who do not want to run PowerShell scripts directly
- Add per-user settings for auto-start, minimize-on-launch, and silent mode

## Notes

- Use a direct USB connection when possible.
- This is an unofficial community workaround, not an Apple-supported Windows utility.
- If the drive is detected but still fails after wake, the remaining possibilities are power, cable/adapter behavior, or a mechanical issue.

## License

MIT
