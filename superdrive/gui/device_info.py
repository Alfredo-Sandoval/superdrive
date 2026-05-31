"""Device information panel widget."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QVBoxLayout

from superdrive.drive import Device


class DeviceInfoPanel(QFrame):
    def __init__(self, parent: QFrame | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("deviceInfoPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._build_ui()
        self.clear()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(4)

        self._badge = QLabel()
        self._badge.setObjectName("deviceBadge")
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._badge)

        self._path_lbl = QLabel()
        self._path_lbl.setObjectName("devicePathLabel")
        self._path_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._path_lbl)

        self._usb_lbl = QLabel()
        self._usb_lbl.setObjectName("deviceUsbLabel")
        self._usb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._usb_lbl)

        self._sysfs_lbl = QLabel()
        self._sysfs_lbl.setObjectName("deviceSysfsLabel")
        self._sysfs_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sysfs_lbl.setWordWrap(True)
        self._sysfs_lbl.hide()
        layout.addWidget(self._sysfs_lbl)

    def _refresh_badge_style(self) -> None:
        self._badge.style().unpolish(self._badge)
        self._badge.style().polish(self._badge)

    def update_device(self, device: Device | None) -> None:
        if device is None:
            self.clear()
            return
        is_apple = device.is_apple_superdrive
        self._badge.setText("Apple SuperDrive" if is_apple else "Optical Drive")
        self._badge.setProperty("apple", is_apple)
        self._refresh_badge_style()
        self._path_lbl.setText(str(device.sr_path))
        vp = (
            f"{device.vendor}:{device.product}"
            if device.vendor and device.product
            else "unknown"
        )
        self._usb_lbl.setText(f"USB  {vp}")
        if device.usb_path:
            self._sysfs_lbl.setText(str(device.usb_path))
            self.setToolTip(str(device.usb_path))
        else:
            self._sysfs_lbl.setText("")
            self.setToolTip("")
        self._sysfs_lbl.setVisible(False)

    def clear(self) -> None:
        self._badge.setText("No Device")
        self._badge.setProperty("apple", False)
        self._refresh_badge_style()
        self._path_lbl.setText("")
        self._usb_lbl.setText("")
        self._sysfs_lbl.setText("")
        self.setToolTip("")
        self._sysfs_lbl.setVisible(False)
