"""Drive model, discovery, and operation facade."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import commands


@dataclass(frozen=True)
class Device:
    sr_path: Path
    usb_path: Path | None
    vendor: str = ""
    product: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "vendor", (self.vendor or "").lower())
        object.__setattr__(self, "product", (self.product or "").lower())

    @property
    def is_apple_superdrive(self) -> bool:
        return self.vendor == "05ac" and self.product == "1500"

    @property
    def display_name(self) -> str:
        tag = "Apple SuperDrive" if self.is_apple_superdrive else "Optical Drive"
        vp = (
            f"{self.vendor}:{self.product}"
            if self.vendor and self.product
            else "unknown"
        )
        return f"{tag} ({self.sr_path}) [{vp}]"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    command: str


class DriveService:
    def __init__(
        self,
        dev_root: Path = Path("/dev"),
        sys_block_root: Path = Path("/sys/class/block"),
    ) -> None:
        self.dev_root = dev_root
        self.sys_block_root = sys_block_root

    def find_usb_parent(self, sys_block_path: Path) -> Path | None:
        current = sys_block_path
        for _ in range(20):
            try:
                if (current / "idVendor").exists() and (
                    current / "idProduct"
                ).exists():
                    return current
            except OSError:
                pass
            if current == current.parent:
                break
            current = current.parent
        return None

    def read_text(self, path: Path) -> str | None:
        try:
            return path.read_text().strip()
        except (OSError, UnicodeDecodeError):
            return None

    def discover_devices(self) -> list[Device]:
        devices: list[Device] = []
        for sr in sorted(self.dev_root.glob("sr*")):
            sys_block = self.sys_block_root / sr.name
            try:
                sys_block_real = sys_block.resolve()
            except FileNotFoundError:
                continue
            usb_parent = self.find_usb_parent(sys_block_real)
            vendor = self.read_text(usb_parent / "idVendor") if usb_parent else None
            product = self.read_text(usb_parent / "idProduct") if usb_parent else None
            devices.append(Device(sr, usb_parent, vendor or "", product or ""))
        return devices

    def device_identity_is_current(self, device: Device) -> tuple[bool, str]:
        if not device.sr_path.exists():
            return False, f"{device.sr_path} no longer exists"

        sys_block = self.sys_block_root / device.sr_path.name
        try:
            sys_block_real = sys_block.resolve()
        except FileNotFoundError:
            return False, f"{sys_block} no longer exists"

        usb_parent = self.find_usb_parent(sys_block_real)
        vendor = (self.read_text(usb_parent / "idVendor") if usb_parent else None) or ""
        product = (
            self.read_text(usb_parent / "idProduct") if usb_parent else None
        ) or ""
        vendor = vendor.lower()
        product = product.lower()

        if device.usb_path:
            if usb_parent != device.usb_path:
                return False, f"{device.sr_path} USB parent changed or disappeared"
            if vendor != device.vendor or product != device.product:
                return (
                    False,
                    f"{device.sr_path} USB ID changed "
                    f"from {device.vendor}:{device.product} to {vendor}:{product}",
                )
        elif device.vendor or device.product:
            if vendor != device.vendor or product != device.product:
                return (
                    False,
                    f"{device.sr_path} device ID changed "
                    f"from {device.vendor}:{device.product} to {vendor}:{product}",
                )

        return True, ""

    def power_keep_on(self, usb_path: Path) -> list[tuple[str, bool, str]]:
        results: list[tuple[str, bool, str]] = []
        for path, value in (
            (usb_path / "power" / "control", "on"),
            (usb_path / "power" / "autosuspend_delay_ms", "-1"),
        ):
            try:
                path.write_text(value)
            except OSError as err:
                results.append((str(path), False, str(err)))
            else:
                results.append((str(path), True, f"set to {value}"))
        return results

    def probe_commands(self, sr: Path) -> list[tuple[str, int, str, str]]:
        command_specs: list[list[str]] = []
        if commands.which("sg_inq"):
            command_specs.append(["sg_inq", str(sr)])
        if commands.which("sg_vpd"):
            command_specs.append(["sg_vpd", "-a", str(sr)])
        if commands.which("sg_modes"):
            command_specs.append(["sg_modes", "-a", str(sr)])

        results: list[tuple[str, int, str, str]] = []
        for command in command_specs:
            rc, out, err = commands.run(
                command,
                timeout=commands.PROBE_TIMEOUT_SECONDS,
            )
            results.append((commands.command_display(command), rc, out, err))
        return results

    def require_sg3(self) -> tuple[bool, str]:
        return commands.require_sg3()

    def send_vendor_load(self, sr: Path) -> tuple[int, str, str]:
        return commands.send_vendor_load(sr)

    def eject_disc(self, sr: Path) -> tuple[int, str, str, str]:
        return commands.eject_disc(sr)

    def lock_door(self, sr: Path, lock: bool) -> tuple[int, str, str, str]:
        return commands.lock_door(sr, lock)

    def tur(self, sr: Path) -> tuple[int, str, str]:
        return commands.tur(sr)

    def get_mountpoint(self, sr: Path) -> str | None:
        return commands.get_mountpoint(sr)

    def unmount_if_mounted(self, sr: Path) -> tuple[bool, str]:
        return commands.unmount_if_mounted(sr)

    def holders_for_mount(self, mount_point: str) -> str:
        return commands.holders_for_mount(mount_point)


default_drive_service = DriveService()


class DriveController:
    def __init__(self, service: DriveService | None = None) -> None:
        self._service = service or default_drive_service

    def discover_devices(self) -> list[Device]:
        return self._service.discover_devices()

    def require_sg3(self) -> tuple[bool, str]:
        return self._service.require_sg3()

    def validate_device(self, device: Device) -> tuple[bool, str]:
        return self._service.device_identity_is_current(device)

    def send_vendor_load(self, sr: Path) -> tuple[int, str, str]:
        return self._service.send_vendor_load(sr)

    def probe_commands(self, sr: Path) -> list[tuple[str, int, str, str]]:
        return self._service.probe_commands(sr)

    def unmount_if_mounted(self, sr: Path) -> tuple[bool, str]:
        return self._service.unmount_if_mounted(sr)

    def get_mountpoint(self, sr: Path) -> str | None:
        return self._service.get_mountpoint(sr)

    def holders_for_mount(self, mount_point: str) -> str:
        return self._service.holders_for_mount(mount_point)

    def eject_disc(self, sr: Path) -> tuple[int, str, str, str]:
        return self._service.eject_disc(sr)

    def lock_door(self, sr: Path, lock: bool) -> tuple[int, str, str, str]:
        return self._service.lock_door(sr, lock)

    def tur(self, sr: Path) -> tuple[int, str, str]:
        return self._service.tur(sr)

    def power_keep_on(self, usb_path: Path) -> list[tuple[str, bool, str]]:
        return self._service.power_keep_on(usb_path)
