"""Device resource commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from .core import (
    CONTROL_CHARS,
    EXIT_CONFIG,
    EXIT_GENERIC,
    EXIT_NOT_FOUND,
    CliError,
    JsonErrorTyperGroup,
    emit_json,
    run_json_guard,
)
from superdrive.drive import Device, DriveController

device_app = typer.Typer(
    add_completion=False,
    cls=JsonErrorTyperGroup,
    help="Inspect and operate optical drives.",
)


def _build_controller() -> DriveController:
    from superdrive import cli as cli_root

    return cli_root.build_controller()


def _invalid_device_path_error() -> CliError:
    return CliError(
        "Invalid device path.",
        "Pass a plain block device path such as /dev/sr0.",
        EXIT_CONFIG,
    )


def _relative_device_path_error() -> CliError:
    return CliError(
        "Device path must be absolute.",
        "Pass a path such as /dev/sr0.",
        EXIT_CONFIG,
    )


def _no_drives_found_error() -> CliError:
    return CliError(
        "No optical drives found.",
        "Connect a drive or pass a device after it appears under /dev.",
        EXIT_NOT_FOUND,
    )


def _multiple_drives_found_error() -> CliError:
    return CliError(
        "Multiple optical drives found.",
        "Pass --device /dev/srX to select one explicitly.",
        EXIT_CONFIG,
    )


def _device_not_found_error(device_path: Path) -> CliError:
    return CliError(
        f"Device not found: {device_path}",
        "Run `superdrive-cli device list --json` to see available devices.",
        EXIT_NOT_FOUND,
    )


def _confirmation_required_error(action: str) -> CliError:
    return CliError(
        f"{action} requires confirmation.",
        "Pass --dry-run to preview or --yes to execute.",
        EXIT_CONFIG,
    )


def _conflicting_confirmation_error(action: str) -> CliError:
    return CliError(
        f"{action} received conflicting confirmation flags.",
        "Pass either --dry-run to preview or --yes to execute, not both.",
        EXIT_CONFIG,
    )


def _unsupported_load_error() -> CliError:
    return CliError(
        "Load is only supported for Apple SuperDrive USB ID 05ac:1500.",
        "Use generic status/probe/eject commands for non-Apple drives.",
        EXIT_CONFIG,
    )


def _missing_usb_path_error() -> CliError:
    return CliError(
        "USB sysfs path is not available for this device.",
        "Run `superdrive-cli device list --json` and select a USB optical drive.",
        EXIT_CONFIG,
    )


def _operation_failed_error(
    action: str,
    *,
    returncode: int,
    stdout: str,
    stderr: str,
    command: str | None = None,
) -> CliError:
    details: dict[str, Any] = {
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
    }
    if command:
        details["command"] = command
    return CliError(
        f"{action} failed.",
        "Run `superdrive-cli device probe --json --device /dev/srX` for diagnostics.",
        EXIT_GENERIC,
        details=details,
    )


def _unmount_failed_error(info: str) -> CliError:
    return CliError(
        "Unmount failed; eject was skipped.",
        "Close processes using the disc, then retry eject.",
        EXIT_GENERIC,
        details={"unmount": info, "ejected": False},
    )


def _device_changed_error(message: str) -> CliError:
    return CliError(
        "Selected device changed before the hardware action.",
        "Run `superdrive-cli device list --json` and retry with the current path.",
        EXIT_GENERIC,
        details={"message": message},
    )


def _power_policy_failed_error(results: list[tuple[str, bool, str]]) -> CliError:
    return CliError(
        "Power policy write failed.",
        "Run the sysfs write with appropriate permissions or add a udev rule.",
        EXIT_GENERIC,
        details={
            "results": [
                {"path": path, "ok": ok, "message": msg}
                for path, ok, msg in results
            ]
        },
    )


def _emit_human(payload: dict[str, Any]) -> None:
    kind = payload.get("kind")
    if kind == "device_list":
        devices = payload["devices"]
        if not devices:
            typer.echo("No optical drives found.")
            return
        for device in devices:
            label = (
                "Apple SuperDrive"
                if device["is_apple_superdrive"]
                else "Optical Drive"
            )
            typer.echo(
                f"{device['sr_path']}\t"
                f"{label}\t"
                f"{device['vendor']}:{device['product']}"
            )
    elif kind == "device_status":
        typer.echo(f"{payload['device']['sr_path']}: {payload['ready']}")
        typer.echo(f"mount: {payload['mountpoint'] or '(not mounted)'}")
    elif kind == "device_probe":
        for result in payload["results"]:
            typer.echo(f"$ {result['command']}")
            typer.echo(f"rc={result['returncode']}")
            typer.echo(result["stdout"] or result["stderr"], nl=False)
    elif kind == "ack":
        typer.echo(f"{payload['status']}: {payload['action']}")
    else:
        emit_json(payload)


def _device_to_dict(device: Device) -> dict[str, Any]:
    return {
        "sr_path": str(device.sr_path),
        "usb_path": str(device.usb_path) if device.usb_path else None,
        "vendor": device.vendor,
        "product": device.product,
        "is_apple_superdrive": device.is_apple_superdrive,
        "display_name": device.display_name,
    }


def _normalize_device_path(raw: str | None) -> Path | None:
    if raw is None:
        return None
    if CONTROL_CHARS.search(raw) or "?" in raw or "#" in raw:
        raise _invalid_device_path_error()
    path = Path(raw)
    if not path.is_absolute():
        raise _relative_device_path_error()
    if path.parts[:2] != ("/", "dev") or ".." in path.parts or path.name == "":
        raise _invalid_device_path_error()
    return path


def _select_device(controller: DriveController, raw_device: str | None) -> Device:
    device_path = _normalize_device_path(raw_device)
    devices = controller.discover_devices()
    if device_path is None:
        if len(devices) == 1:
            return devices[0]
        if not devices:
            raise _no_drives_found_error()
        raise _multiple_drives_found_error()
    for device in devices:
        if device.sr_path == device_path:
            return device
    raise _device_not_found_error(device_path)


def _require_confirmed_mutation(*, yes: bool, dry_run: bool, action: str) -> None:
    if yes and dry_run:
        raise _conflicting_confirmation_error(action)
    if dry_run or yes:
        return
    raise _confirmation_required_error(action)


def _ready_status(returncode: int, stdout: str, stderr: str) -> str:
    text = stdout + stderr
    if returncode == 0:
        return "READY"
    if "Medium not present" in text or "tray open" in text:
        return "NOT_READY_NO_DISC"
    if "becoming ready" in text:
        return "NOT_READY_WAKING"
    return "NOT_READY"


@device_app.command("list")
def list_devices(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON only."),
    ] = False,
) -> None:
    def body() -> dict[str, Any]:
        devices = _build_controller().discover_devices()
        return {
            "kind": "device_list",
            "devices": [_device_to_dict(device) for device in devices],
        }

    run_json_guard(body, json_output=json_output, emit_human=_emit_human)


@device_app.command("status")
def status(
    device: Annotated[
        str | None,
        typer.Option("--device", help="Optical drive path, for example /dev/sr0."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON only."),
    ] = False,
) -> None:
    def body() -> dict[str, Any]:
        controller = _build_controller()
        selected = _select_device(controller, device)
        mountpoint = controller.get_mountpoint(selected.sr_path)
        rc, out, err = controller.tur(selected.sr_path)
        return {
            "kind": "device_status",
            "device": _device_to_dict(selected),
            "mountpoint": mountpoint,
            "ready": _ready_status(rc, out, err),
            "command": "sg_turs",
            "returncode": rc,
            "stdout": out,
            "stderr": err,
        }

    run_json_guard(body, json_output=json_output, emit_human=_emit_human)


@device_app.command("probe")
def probe(
    device: Annotated[
        str | None,
        typer.Option("--device", help="Optical drive path, for example /dev/sr0."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON only."),
    ] = False,
) -> None:
    def body() -> dict[str, Any]:
        controller = _build_controller()
        selected = _select_device(controller, device)
        results = [
            {
                "command": command,
                "returncode": rc,
                "stdout": stdout,
                "stderr": stderr,
            }
            for command, rc, stdout, stderr in controller.probe_commands(
                selected.sr_path
            )
        ]
        return {
            "kind": "device_probe",
            "device": _device_to_dict(selected),
            "results": results,
        }

    run_json_guard(body, json_output=json_output, emit_human=_emit_human)


@device_app.command("load")
def load(
    device: Annotated[
        str | None,
        typer.Option("--device", help="Apple SuperDrive path, for example /dev/sr0."),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Execute the load command."),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate only.")] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON only."),
    ] = False,
) -> None:
    def body() -> dict[str, Any]:
        _require_confirmed_mutation(yes=yes, dry_run=dry_run, action="load")
        controller = _build_controller()
        selected = _select_device(controller, device)
        if not selected.is_apple_superdrive:
            raise _unsupported_load_error()
        if dry_run:
            return {
                "kind": "ack",
                "status": "dry_run",
                "action": "load",
                "device": _device_to_dict(selected),
            }
        rc, out, err = controller.send_vendor_load(selected.sr_path)
        if rc != 0:
            raise _operation_failed_error(
                "load",
                returncode=rc,
                stdout=out,
                stderr=err,
            )
        return {
            "kind": "ack",
            "status": "ok",
            "action": "load",
            "device": _device_to_dict(selected),
            "returncode": rc,
            "stdout": out,
            "stderr": err,
        }

    run_json_guard(body, json_output=json_output, emit_human=_emit_human)


@device_app.command("eject")
def eject(
    device: Annotated[
        str | None,
        typer.Option("--device", help="Optical drive path, for example /dev/sr0."),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Unmount and eject.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate only.")] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON only."),
    ] = False,
) -> None:
    def body() -> dict[str, Any]:
        _require_confirmed_mutation(yes=yes, dry_run=dry_run, action="eject")
        controller = _build_controller()
        selected = _select_device(controller, device)
        if dry_run:
            return {
                "kind": "ack",
                "status": "dry_run",
                "action": "eject",
                "device": _device_to_dict(selected),
            }
        ok, info = controller.unmount_if_mounted(selected.sr_path)
        if not ok:
            raise _unmount_failed_error(info)
        current, message = controller.validate_device(selected)
        if not current:
            raise _device_changed_error(message)
        rc, out, err, command = controller.eject_disc(selected.sr_path)
        if rc != 0:
            raise _operation_failed_error(
                "eject",
                returncode=rc,
                stdout=out,
                stderr=err,
                command=command,
            )
        return {
            "kind": "ack",
            "status": "ok",
            "action": "eject",
            "device": _device_to_dict(selected),
            "command": command,
            "returncode": rc,
            "stdout": out,
            "stderr": err,
            "ejected": rc == 0,
        }

    run_json_guard(body, json_output=json_output, emit_human=_emit_human)


@device_app.command("lock")
def lock(
    device: Annotated[
        str | None,
        typer.Option("--device", help="Optical drive path, for example /dev/sr0."),
    ] = None,
    prevent: Annotated[
        bool,
        typer.Option("--prevent/--allow", help="Prevent or allow medium removal."),
    ] = True,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Execute the lock command."),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate only.")] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON only."),
    ] = False,
) -> None:
    def body() -> dict[str, Any]:
        _require_confirmed_mutation(yes=yes, dry_run=dry_run, action="lock")
        controller = _build_controller()
        selected = _select_device(controller, device)
        action = "prevent" if prevent else "allow"
        if dry_run:
            return {
                "kind": "ack",
                "status": "dry_run",
                "action": action,
                "device": _device_to_dict(selected),
            }
        rc, out, err, command = controller.lock_door(selected.sr_path, prevent)
        if rc != 0:
            raise _operation_failed_error(
                action,
                returncode=rc,
                stdout=out,
                stderr=err,
                command=command,
            )
        return {
            "kind": "ack",
            "status": "ok",
            "action": action,
            "device": _device_to_dict(selected),
            "command": command,
            "returncode": rc,
            "stdout": out,
            "stderr": err,
        }

    run_json_guard(body, json_output=json_output, emit_human=_emit_human)


@device_app.command("power")
def power(
    device: Annotated[
        str | None,
        typer.Option("--device", help="Optical drive path, for example /dev/sr0."),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Write sysfs power settings."),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate only.")] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON only."),
    ] = False,
) -> None:
    def body() -> dict[str, Any]:
        _require_confirmed_mutation(yes=yes, dry_run=dry_run, action="power")
        controller = _build_controller()
        selected = _select_device(controller, device)
        if selected.usb_path is None:
            raise _missing_usb_path_error()
        if dry_run:
            return {
                "kind": "ack",
                "status": "dry_run",
                "action": "power",
                "device": _device_to_dict(selected),
            }
        current, message = controller.validate_device(selected)
        if not current:
            raise _device_changed_error(message)
        results = controller.power_keep_on(selected.usb_path)
        if not all(result[1] for result in results):
            raise _power_policy_failed_error(results)
        return {
            "kind": "ack",
            "status": "ok",
            "action": "power",
            "device": _device_to_dict(selected),
            "results": [
                {"path": path, "ok": ok, "message": msg}
                for path, ok, msg in results
            ],
        }

    run_json_guard(body, json_output=json_output, emit_human=_emit_human)
