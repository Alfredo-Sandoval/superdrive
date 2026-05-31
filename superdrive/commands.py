"""Subprocess-backed command adapter for optical-drive utilities."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from shutil import which as shutil_which

COMMAND_TIMEOUT_SECONDS = 20.0
PROBE_TIMEOUT_SECONDS = 45.0
COMMAND_NOT_FOUND_RC = 127
TIMEOUT_RC = 124
SAFE_COMMAND_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"

SAFE_COMMAND_DIRS = tuple(Path(path) for path in SAFE_COMMAND_PATH.split(":"))


def which(cmd: str) -> str | None:
    return shutil_which(cmd, path=SAFE_COMMAND_PATH)


def _timeout_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def command_display(cmd: list[str] | str) -> str:
    if isinstance(cmd, str):
        return cmd
    return " ".join(shlex.quote(part) for part in cmd)


def _absolute_command_is_safe(executable: str) -> bool:
    executable_path = Path(executable)
    return executable_path.is_absolute() and executable_path.parent in SAFE_COMMAND_DIRS


def _resolve_command(cmd: list[str] | str, use_shell: bool) -> list[str] | str | None:
    if use_shell:
        return None
    if isinstance(cmd, str):
        return cmd
    if not cmd:
        return cmd
    executable = cmd[0]
    if "/" not in executable:
        resolved = which(executable)
        return [resolved, *cmd[1:]] if resolved else None
    return cmd if _absolute_command_is_safe(executable) else None


def run(
    cmd: list[str] | str,
    use_shell: bool = False,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    if use_shell:
        return COMMAND_NOT_FOUND_RC, "", "Shell execution is disabled"
    if isinstance(cmd, str) and not use_shell:
        cmd = shlex.split(cmd)
    resolved_cmd = _resolve_command(cmd, use_shell)
    if resolved_cmd is None:
        return (
            COMMAND_NOT_FOUND_RC,
            "",
            f"Command not found in safe path: {command_display(cmd)}",
        )
    try:
        proc = subprocess.run(
            resolved_cmd,
            capture_output=True,
            text=True,
            shell=use_shell,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as err:
        return COMMAND_NOT_FOUND_RC, "", str(err)
    except subprocess.TimeoutExpired as err:
        out = _timeout_output(err.stdout)
        stderr = _timeout_output(err.stderr)
        timeout_msg = f"Timed out after {timeout:g}s: {command_display(cmd)}"
        return TIMEOUT_RC, out, f"{stderr}\n{timeout_msg}".strip()
    else:
        return proc.returncode, proc.stdout, proc.stderr


def require_sg3() -> tuple[bool, str]:
    missing = []
    for cmd in ("sg_raw", "sg_turs", "sg_start", "sg_prevent"):
        if not which(cmd):
            missing.append(cmd)
    if missing:
        return (
            False,
            "Missing sg3_utils tools: "
            f"{', '.join(missing)}. Install: sudo apt install sg3-utils",
        )
    return True, ""


def send_vendor_load(sr: Path) -> tuple[int, str, str]:
    return run(["sg_raw", str(sr), "EA", "00", "00", "00", "00", "00", "01"])


def eject_disc(sr: Path) -> tuple[int, str, str, str]:
    first_attempt: str | None = None
    if which("sg_start"):
        cmd = ["sg_start", "--eject", str(sr)]
        first_rc, first_out, first_err = run(cmd)
        if first_rc == 0:
            return first_rc, first_out, first_err, command_display(cmd)
        first_attempt = (
            f"$ {command_display(cmd)}\n"
            f"rc={first_rc}\n"
            f"{first_out}{first_err}"
        )
    cmd = ["eject", str(sr)]
    rc, out, err = run(cmd)
    if first_attempt is not None:
        err = f"{first_attempt}\n{err}".strip()
    return rc, out, err, command_display(cmd)


def lock_door(sr: Path, lock: bool) -> tuple[int, str, str, str]:
    if which("sg_prevent"):
        cmd = ["sg_prevent", "--prevent=1" if lock else "--allow", str(sr)]
        rc, out, err = run(cmd)
        return rc, out, err, command_display(cmd)
    cmd = ["eject", "-i", "on" if lock else "off", str(sr)]
    rc, out, err = run(cmd)
    return rc, out, err, command_display(cmd)


def tur(sr: Path) -> tuple[int, str, str]:
    if which("sg_turs"):
        return run(["sg_turs", "-vv", str(sr)])
    return COMMAND_NOT_FOUND_RC, "", "sg_turs not available"


def get_mountpoint(sr: Path) -> str | None:
    rc, out, _ = run(["lsblk", "-no", "MOUNTPOINT", str(sr)])
    if rc == 0:
        mount_point = out.strip()
        return mount_point if mount_point else None
    rc, out, _ = run(["findmnt", "-no", "TARGET", str(sr)])
    if rc == 0:
        mount_point = out.strip()
        return mount_point if mount_point else None
    return None


def unmount_if_mounted(sr: Path) -> tuple[bool, str]:
    mount_point = get_mountpoint(sr)
    if not mount_point:
        return True, "not mounted"
    if which("udisksctl"):
        rc, out, err = run(["udisksctl", "unmount", "-b", str(sr)])
        return (rc == 0), f"udisksctl rc={rc}\n{out}{err}"
    rc, out, err = run(["umount", str(sr)])
    return (rc == 0), f"umount rc={rc}\n{out}{err}"


def holders_for_mount(mount_point: str) -> str:
    lines: list[str] = []
    if which("lsof"):
        rc, out, err = run(["lsof", "-w", "-n", mount_point])
        if rc == 0 and out.strip():
            lines.append("Processes using mount (lsof):")
            lines.append(out.rstrip())
            return "\n".join(lines)
        if err.strip():
            lines.append(err.rstrip())
    if which("fuser"):
        rc, out, err = run(["fuser", "-vm", mount_point])
        if (out + err).strip():
            lines.append("Processes using mount (fuser):")
            lines.append((out + err).rstrip())
            return "\n".join(lines)
    return "Could not list processes holding the mount (install lsof or fuser)."
