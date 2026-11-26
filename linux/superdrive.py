#!/usr/bin/python3 -I
"""Wake, eject and inspect an Apple USB SuperDrive on Linux.

The SuperDrive ignores discs until it receives the vendor-specific command
EA 00 00 00 00 00 01. ``wake`` sends it through the SG_IO ioctl. The kernel only
passes vendor commands for processes with CAP_SYS_RAWIO, so ``wake`` runs as root:
from the udev rule on every plug-in, or through pkexec from the window.
``status``, ``eject`` and ``open`` run as the desktop user.

This one file backs the udev rule, the command line and the window, and uses only
the Python standard library so it can run as root without a virtual environment.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

SYSFS_ROOT = Path("/sys")
MOUNTS_PATH = Path("/proc/self/mounts")
# Wake outcomes, written by root and read by the window (tmpfs, cleared on reboot).
STATE_DIR = Path("/run/apple-superdrive-helper")
UDEV_RULE_PATH = Path("/etc/udev/rules.d/99-apple-superdrive.rules")

WAKE_CDB = bytes([0xEA, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01])
SG_IO = 0x2285
SG_DXFER_NONE = -1
SG_INTERFACE_ID = ord("S")
SENSE_BUFFER_SIZE = 32
WAKE_TIMEOUT_MS = 10_000
# The first command after power-on usually reports UNIT ATTENTION instead of running.
# Retrying clears it; any other failure is reported unchanged.
MAX_WAKE_ATTEMPTS = 3
SENSE_KEY_UNIT_ATTENTION = 0x06

DEVICE_PATTERN = re.compile(r"/dev/sr[0-9]+")

SCSI_STATUS_NAMES = {
    0x00: "GOOD",
    0x02: "CHECK CONDITION",
    0x08: "BUSY",
    0x18: "RESERVATION CONFLICT",
    0x28: "TASK SET FULL",
}
SENSE_KEY_NAMES = (
    "NO SENSE", "RECOVERED ERROR", "NOT READY", "MEDIUM ERROR",
    "HARDWARE ERROR", "ILLEGAL REQUEST", "UNIT ATTENTION", "DATA PROTECT",
    "BLANK CHECK", "VENDOR SPECIFIC", "COPY ABORTED", "ABORTED COMMAND",
    "RESERVED", "VOLUME OVERFLOW", "MISCOMPARE", "COMPLETED",
)


class HelperError(Exception):
    """A failure reported to the user as one message, without a traceback."""


class SgIoHeader(ctypes.Structure):
    """``struct sg_io_hdr`` from <scsi/sg.h> (88 bytes on 64-bit Linux)."""

    _fields_ = [
        ("interface_id", ctypes.c_int),
        ("dxfer_direction", ctypes.c_int),
        ("cmd_len", ctypes.c_ubyte),
        ("mx_sb_len", ctypes.c_ubyte),
        ("iovec_count", ctypes.c_ushort),
        ("dxfer_len", ctypes.c_uint),
        ("dxferp", ctypes.c_void_p),
        ("cmdp", ctypes.c_void_p),
        ("sbp", ctypes.c_void_p),
        ("timeout", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("pack_id", ctypes.c_int),
        ("usr_ptr", ctypes.c_void_p),
        ("status", ctypes.c_ubyte),
        ("masked_status", ctypes.c_ubyte),
        ("msg_status", ctypes.c_ubyte),
        ("sb_len_wr", ctypes.c_ubyte),
        ("host_status", ctypes.c_ushort),
        ("driver_status", ctypes.c_ushort),
        ("resid", ctypes.c_int),
        ("duration", ctypes.c_uint),
        ("info", ctypes.c_uint),
    ]


@dataclass(frozen=True)
class Drive:
    name: str               # kernel name, for example "sr0"
    device: str             # device node, for example "/dev/sr0"
    vendor: str
    model: str
    revision: str
    plug_id: str | None     # "bus-devnum" of the USB device; new on every plug-in


@dataclass(frozen=True)
class Media:
    present: bool
    mounted_at: str | None
    label: str
    fs_type: str
    size_bytes: int | None


@dataclass(frozen=True)
class WakeResult:
    status: int
    host_status: int
    driver_status: int
    sense: tuple[int, int, int] | None   # sense key, ASC, ASCQ
    attempts: int

    @property
    def succeeded(self) -> bool:
        return self.status == 0x00 and self.host_status == 0

    def describe(self) -> str:
        name = SCSI_STATUS_NAMES.get(self.status, "UNEXPECTED")
        text = f"SCSI status 0x{self.status:02X} ({name})"
        if self.host_status:
            text += f", host status 0x{self.host_status:02X}"
        if not self.succeeded and self.sense is not None:
            key, asc, ascq = self.sense
            text += f", sense {SENSE_KEY_NAMES[key & 0x0F]} (key 0x{key:X}, ASC 0x{asc:02X}, ASCQ 0x{ascq:02X})"
        if self.attempts > 1:
            text += f", {self.attempts} attempts"
        return text


# --- Discovery ----------------------------------------------------------------------------

def find_superdrives(sysfs_root: Path = SYSFS_ROOT) -> list[Drive]:
    """Returns the connected Apple SuperDrives, identified by their SCSI inquiry strings."""
    block = sysfs_root / "class" / "block"
    if not block.is_dir():
        return []

    drives = []
    for entry in sorted(block.glob("sr*"), key=_kernel_name_order):
        scsi_device = entry / "device"
        vendor = _read_attribute(scsi_device / "vendor")
        model = _read_attribute(scsi_device / "model")
        if not (vendor.lower().startswith("apple") and "superdrive" in model.lower()):
            continue
        drives.append(Drive(
            name=entry.name,
            device=f"/dev/{entry.name}",
            vendor=vendor,
            model=model,
            revision=_read_attribute(scsi_device / "rev"),
            plug_id=usb_plug_id(scsi_device),
        ))
    return drives


def usb_plug_id(scsi_device: Path) -> str | None:
    """Returns "bus-devnum" of the USB device above a SCSI device.

    The kernel assigns a new device number on every plug-in, so this tells one
    connection of the drive apart from the next.
    """
    try:
        current = scsi_device.resolve()
    except OSError:
        return None
    for directory in (current, *current.parents):
        busnum = _read_attribute(directory / "busnum")
        devnum = _read_attribute(directory / "devnum")
        if busnum and devnum:
            return f"{busnum}-{devnum}"
    return None


def select_drives(device: str | None, sysfs_root: Path = SYSFS_ROOT) -> list[Drive]:
    """Returns the SuperDrive at ``device``, or all of them when ``device`` is None.

    Refuses any device that is not an Apple SuperDrive, because ``wake`` can run as
    root through pkexec and must not send vendor commands anywhere else.
    """
    drives = find_superdrives(sysfs_root)
    if device is None:
        if not drives:
            raise HelperError("No Apple SuperDrive is connected.")
        return drives

    if not DEVICE_PATTERN.fullmatch(device):
        raise HelperError(f"{device} is not an optical drive device such as /dev/sr0.")
    matching = [drive for drive in drives if drive.device == device]
    if not matching:
        raise HelperError(f"{device} is not a connected Apple SuperDrive.")
    return matching


# --- Disc state ---------------------------------------------------------------------------

def read_media(drive: Drive, sysfs_root: Path = SYSFS_ROOT, mounts_path: Path = MOUNTS_PATH) -> Media:
    properties = udev_properties(drive.device)
    sectors = _read_int(sysfs_root / "class" / "block" / drive.name / "size")
    present = properties.get("ID_CDROM_MEDIA") == "1" or sectors > 0

    device_names = {drive.device, os.path.realpath(drive.device)}
    mounted_at = find_mount_point(device_names, mounts_path.read_text(encoding="utf-8", errors="replace"))

    size_bytes = sectors * 512 if sectors > 0 else None
    if mounted_at is not None:
        try:
            size_bytes = shutil.disk_usage(mounted_at).total
        except OSError:
            pass  # Unmounted since /proc/self/mounts was read; keep the block size.

    label = ""
    fs_type = ""
    if present:
        label = decode_udev_value(properties.get("ID_FS_LABEL_ENC") or properties.get("ID_FS_LABEL", ""))
        fs_type = properties.get("ID_FS_TYPE", "")

    return Media(present=present, mounted_at=mounted_at, label=label, fs_type=fs_type, size_bytes=size_bytes)


def udev_properties(device: str) -> dict[str, str]:
    """Returns udev's properties for a device, or {} when udevadm is unavailable.

    The properties only add the disc label and file system, so their absence is
    shown as unknown values rather than treated as a failure.
    """
    try:
        completed = subprocess.run(
            ["udevadm", "info", "--query=property", f"--name={device}"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if completed.returncode != 0:
        return {}
    return parse_udev_properties(completed.stdout)


def parse_udev_properties(text: str) -> dict[str, str]:
    properties = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value
    return properties


def decode_udev_value(value: str) -> str:
    """Decodes the \\xNN escapes udev uses in *_ENC properties."""
    return re.sub(r"\\x([0-9A-Fa-f]{2})", lambda match: chr(int(match.group(1), 16)), value)


def find_mount_point(device_names: set[str], mounts_text: str) -> str | None:
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] in device_names:
            return unescape_mount_field(fields[1])
    return None


def unescape_mount_field(field: str) -> str:
    """Decodes the octal escapes (\\040 for a space) used in /proc/self/mounts."""
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), field)


# --- Wake ---------------------------------------------------------------------------------

def send_wake(device: str) -> WakeResult:
    """Sends the wake command to ``device`` and returns the drive's answer. Needs root."""
    import fcntl  # Linux only; imported here so the rest of the module loads anywhere.

    descriptor = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    try:
        result = None
        for attempt in range(1, MAX_WAKE_ATTEMPTS + 1):
            result = _send_command(fcntl, descriptor, WAKE_CDB, attempt)
            unit_attention = result.sense is not None and result.sense[0] == SENSE_KEY_UNIT_ATTENTION
            if result.succeeded or not unit_attention:
                break
        return result
    finally:
        os.close(descriptor)


def _send_command(fcntl_module, descriptor: int, cdb: bytes, attempt: int) -> WakeResult:
    command = (ctypes.c_ubyte * len(cdb)).from_buffer_copy(cdb)
    sense = (ctypes.c_ubyte * SENSE_BUFFER_SIZE)()
    header = SgIoHeader(
        interface_id=SG_INTERFACE_ID,
        dxfer_direction=SG_DXFER_NONE,
        cmd_len=len(cdb),
        mx_sb_len=SENSE_BUFFER_SIZE,
        timeout=WAKE_TIMEOUT_MS,
        cmdp=ctypes.addressof(command),
        sbp=ctypes.addressof(sense),
    )
    fcntl_module.ioctl(descriptor, SG_IO, header)

    sense_bytes = bytes(sense[: header.sb_len_wr])
    return WakeResult(
        status=header.status,
        host_status=header.host_status,
        driver_status=header.driver_status,
        sense=decode_sense(sense_bytes),
        attempts=attempt,
    )


def decode_sense(sense: bytes) -> tuple[int, int, int] | None:
    """Returns (sense key, ASC, ASCQ) from fixed or descriptor sense data, else None."""
    if len(sense) < 4:
        return None
    response_code = sense[0] & 0x7F
    if response_code in (0x70, 0x71) and len(sense) >= 14:
        return sense[2] & 0x0F, sense[12], sense[13]
    if response_code in (0x72, 0x73):
        return sense[1] & 0x0F, sense[2], sense[3]
    return None


def write_wake_record(drive: Drive, ok: bool, detail: str, source: str, state_dir: Path = STATE_DIR) -> None:
    record = {
        "device": drive.device,
        "plug_id": drive.plug_id,
        "ok": ok,
        "detail": detail,
        "source": source,
        "time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    state_dir.mkdir(mode=0o755, exist_ok=True)
    temporary = state_dir / f".{drive.name}.json.tmp"
    temporary.write_text(json.dumps(record), encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, state_dir / f"{drive.name}.json")


def read_wake_record(drive: Drive, state_dir: Path = STATE_DIR) -> dict | None:
    """Returns the last wake outcome for this connection of the drive, if there is one."""
    try:
        record = json.loads((state_dir / f"{drive.name}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise HelperError(f"Could not read the wake record for {drive.device}: {error}") from error

    # A record from an earlier connection says nothing about the drive as it is now.
    if drive.plug_id is None or record.get("plug_id") != drive.plug_id:
        return None
    return record


# --- Commands -----------------------------------------------------------------------------

def collect_status(sysfs_root: Path = SYSFS_ROOT, state_dir: Path = STATE_DIR) -> dict:
    drives = []
    for drive in find_superdrives(sysfs_root):
        entry = asdict(drive)
        entry["media"] = asdict(read_media(drive, sysfs_root))
        entry["wake"] = read_wake_record(drive, state_dir)
        drives.append(entry)
    return {"drives": drives, "auto_wake_installed": UDEV_RULE_PATH.exists()}


def command_status(args: argparse.Namespace) -> int:
    status = collect_status()
    if args.json:
        print(json.dumps(status))
        return 0

    if not status["drives"]:
        print("No Apple SuperDrive is connected.")
    for drive in status["drives"]:
        media = drive["media"]
        wake = drive["wake"]
        print(f"{drive['device']}: {drive['vendor']} {drive['model']} (firmware {drive['revision'] or 'unknown'})")
        if wake is None:
            print("  wake:  no wake recorded for this connection")
        else:
            outcome = "accepted" if wake["ok"] else "failed"
            print(f"  wake:  {outcome} at {wake['time']} ({wake['source']}): {wake['detail']}")
        if not media["present"]:
            print("  disc:  none")
        else:
            label = media["label"] or "unlabeled"
            where = media["mounted_at"] or "not mounted"
            print(f"  disc:  {label} ({media['fs_type'] or 'unknown file system'}), {where}")
    print(f"auto-wake: {'installed' if status['auto_wake_installed'] else 'not installed'}")
    return 0


def command_wake(args: argparse.Namespace) -> int:
    if os.geteuid() != 0:
        raise HelperError(f"Waking the drive needs root. Run: sudo {Path(__file__).resolve()} wake")

    all_woke = True
    for drive in select_drives(args.device):
        try:
            result = send_wake(drive.device)
        except OSError as error:
            ok = False
            detail = f"Could not send the wake command to {drive.device}: {error.strerror or error}"
        else:
            ok = result.succeeded
            detail = result.describe()

        all_woke = all_woke and ok
        verb = "woke" if ok else "did not wake"
        print(f"{drive.device} {verb}: {detail}", file=sys.stdout if ok else sys.stderr)

        try:
            write_wake_record(drive, ok, detail, args.source)
        except OSError as error:
            # The wake itself is done; only the window's status line will be missing it.
            print(f"warning: could not record the wake outcome: {error}", file=sys.stderr)

    return 0 if all_woke else 1


def command_eject(args: argparse.Namespace) -> int:
    eject_tool = shutil.which("eject")
    if eject_tool is None:
        raise HelperError("The eject command is not installed; it comes with util-linux.")

    for drive in select_drives(args.device):
        # eject unmounts the disc before opening the slot.
        completed = subprocess.run([eject_tool, drive.device], capture_output=True, text=True, timeout=60, check=False)
        if completed.returncode != 0:
            reason = completed.stderr.strip() or f"eject exited with code {completed.returncode}"
            raise HelperError(f"Could not eject {drive.device}: {reason}")
        print(f"Ejected {drive.device}.")
    return 0


def command_open(args: argparse.Namespace) -> int:
    drive = select_drives(args.device)[0]
    media = read_media(drive)
    if not media.present:
        raise HelperError(f"There is no disc in {drive.device}.")

    mounted_at = media.mounted_at
    if mounted_at is None:
        if not media.fs_type:
            raise HelperError("This disc has no file system to open. Blank and audio discs cannot be opened as files.")
        udisksctl = shutil.which("udisksctl")
        if udisksctl is None:
            raise HelperError("udisksctl is not installed, so the disc cannot be mounted here. Mount it from your file manager.")
        completed = subprocess.run([udisksctl, "mount", "--block-device", drive.device],
                                   capture_output=True, text=True, timeout=60, check=False)
        if completed.returncode != 0:
            raise HelperError(f"Could not mount {drive.device}: {completed.stderr.strip() or completed.stdout.strip()}")
        mounted_at = read_media(drive).mounted_at
        if mounted_at is None:
            raise HelperError(f"{drive.device} reported a mount, but it does not appear in /proc/self/mounts.")

    opener = shutil.which("xdg-open")
    if opener is None:
        print(mounted_at)
        return 0
    subprocess.Popen([opener, mounted_at], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    print(f"Opened {mounted_at}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="superdrive.py", description="Wake, eject and inspect an Apple USB SuperDrive.")
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", help="show connected SuperDrives and their discs")
    status.add_argument("--json", action="store_true", help="print machine-readable output")
    status.set_defaults(handler=command_status)

    wake = commands.add_parser("wake", help="send the wake command (needs root)")
    wake.add_argument("--device", help="device node such as /dev/sr0; default: every SuperDrive")
    wake.add_argument("--source", choices=("cli", "udev", "gui"), default="cli", help="who asked, for the wake record")
    wake.set_defaults(handler=command_wake)

    eject = commands.add_parser("eject", help="unmount the disc and eject it")
    eject.add_argument("--device", help="device node such as /dev/sr0; default: every SuperDrive")
    eject.set_defaults(handler=command_eject)

    open_disc = commands.add_parser("open", help="mount the disc if needed and open it in the file manager")
    open_disc.add_argument("--device", help="device node such as /dev/sr0; default: the first SuperDrive")
    open_disc.set_defaults(handler=command_open)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # The polkit policy lets desktop users run this file as root without a password,
    # so under pkexec it does only the one thing the policy is for.
    if os.environ.get("PKEXEC_UID") and args.command != "wake":
        print("error: through pkexec this helper only runs 'wake'.", file=sys.stderr)
        return 2

    try:
        return args.handler(args)
    except HelperError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def _read_attribute(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _read_int(path: Path) -> int:
    text = _read_attribute(path)
    return int(text) if text.isdigit() else 0


def _kernel_name_order(path: Path) -> tuple[int, str]:
    digits = path.name[2:]
    return (int(digits) if digits.isdigit() else sys.maxsize, path.name)


if __name__ == "__main__":
    sys.exit(main())
