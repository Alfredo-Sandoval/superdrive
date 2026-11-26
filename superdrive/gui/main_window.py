#!/usr/bin/env python3
"""Qt main window for SuperDrive Helper."""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDockWidget,
    QGridLayout,
    QFrame,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from superdrive.drive import Device, DriveController
from superdrive.gui.device_info import DeviceInfoPanel
from superdrive.gui.theme import apply_style


class MainWindow(QMainWindow):
    _post_signal = Signal(object)

    def __init__(self, controller: DriveController | None = None) -> None:
        super().__init__()
        self.controller = controller or DriveController()
        self.setWindowTitle("SuperDrive Helper")
        self.resize(430, 500)
        self.setMinimumSize(380, 360)
        self.devices: list[Device] = []
        self.selected: Device | None = None
        self._busy = False
        self._workers: list[threading.Thread] = []

        self._post_signal.connect(self._dispatch_post)
        self._build_ui()
        self.refresh_devices()

    def post(self, fn: Callable[[], None]) -> None:
        self._post_signal.emit(fn)

    def _dispatch_post(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except (RuntimeError, AttributeError) as exc:
            self.statusBar().showMessage(f"Error in UI update: {exc}")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._busy or self._active_workers():
            QMessageBox.warning(
                self,
                "Operation in progress",
                "Wait for the current drive operation to finish before closing.",
            )
            event.ignore()
            return
        super().closeEvent(event)

    def _build_ui(self) -> None:
        self._build_preferences_menu()
        self._build_device_bar()

        self.setCentralWidget(self._build_controls())
        self._build_debug_dock()
        self._build_progress_widgets()

    def _build_preferences_menu(self) -> None:
        preferences = self.menuBar().addMenu("Prefs")
        self.debug_mode_action = QAction("Debug Mode", self)
        self.debug_mode_action.setCheckable(True)
        self.debug_mode_action.setToolTip("Show raw command output")
        self.debug_mode_action.toggled.connect(self._set_debug_mode)
        preferences.addAction(self.debug_mode_action)

    def _build_device_bar(self) -> None:
        device_toolbar = QToolBar("Drive", self)
        device_toolbar.setObjectName("deviceToolbar")
        device_toolbar.setMovable(False)
        device_toolbar.setFloatable(False)
        device_toolbar.setIconSize(QSize(16, 16))
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, device_toolbar)

        drive_label = QLabel("Drive")
        drive_label.setObjectName("toolbarLabel")
        device_toolbar.addWidget(drive_label)

        self.device_cb = QComboBox()
        self.device_cb.setObjectName("deviceCombo")
        self.device_cb.currentIndexChanged.connect(self.on_device_selected)
        self.device_cb.setMinimumWidth(180)
        self.device_cb.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        device_toolbar.addWidget(self.device_cb)

        self.btn_rescan = QToolButton(self)
        self.btn_rescan.setText("Scan")
        self.btn_rescan.setToolTip("Refresh optical drive list")
        self.btn_rescan.setAutoRaise(False)
        self.btn_rescan.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.btn_rescan.clicked.connect(self.refresh_devices)
        device_toolbar.addWidget(self.btn_rescan)

    def _build_controls(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName("controlsPane")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        container.setObjectName("controlsContent")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self._device_info = DeviceInfoPanel()
        layout.addWidget(self._device_info)

        self._build_action_groups(layout)

        layout.addStretch()
        scroll.setWidget(container)
        return scroll

    def _section_header(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("sectionHeader")
        return lbl

    def _sidebar_button(
        self,
        text: str,
        tooltip: str,
        callback: Callable[[], None],
        *,
        primary: bool = False,
        danger: bool = False,
    ) -> QToolButton:
        button = QToolButton(self)
        button.setText(text)
        button.setToolTip(tooltip)
        button.setAutoRaise(False)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        button.setProperty("primary", primary)
        button.setProperty("danger", danger)
        button.setProperty("sidebarBtn", True)
        button.clicked.connect(callback)
        return button

    def _build_action_groups(self, layout: QVBoxLayout) -> None:
        self.btn_load = self._sidebar_button(
            "Load",
            "Accept a disc with the Apple SuperDrive nudge",
            self.do_load,
            primary=True,
        )
        self.btn_eject = self._sidebar_button(
            "Eject",
            "Unmount if mounted, then eject",
            self.do_eject,
            danger=True,
        )
        self.btn_unlock = self._sidebar_button(
            "Unlock",
            "Allow medium removal",
            lambda: self.do_lock(False),
        )
        self.btn_lock = self._sidebar_button(
            "Lock",
            "Prevent medium removal",
            lambda: self.do_lock(True),
        )
        self.btn_tur = self._sidebar_button(
            "Status",
            "Query readiness and basic info",
            self.do_tur,
        )
        self.btn_probe = self._sidebar_button(
            "Probe",
            "Show INQUIRY, VPD, and mode data",
            self.do_probe,
        )
        self.btn_power = self._sidebar_button(
            "Power",
            "Disable USB autosuspend for the drive",
            self.do_power_keep,
        )

        self.command_buttons = [
            self.btn_unlock,
            self.btn_load,
            self.btn_tur,
            self.btn_probe,
            self.btn_lock,
            self.btn_eject,
            self.btn_power,
        ]

        layout.addWidget(self._section_header("DISC"))
        disc_grid = QGridLayout()
        disc_grid.setSpacing(5)
        disc_grid.addWidget(self.btn_load, 0, 0)
        disc_grid.addWidget(self.btn_eject, 0, 1)
        layout.addLayout(disc_grid)

        layout.addWidget(self._section_header("DOOR"))
        door_grid = QGridLayout()
        door_grid.setSpacing(5)
        door_grid.addWidget(self.btn_unlock, 0, 0)
        door_grid.addWidget(self.btn_lock, 0, 1)
        layout.addLayout(door_grid)

        layout.addWidget(self._section_header("DIAGNOSTICS"))
        diag_grid = QGridLayout()
        diag_grid.setSpacing(5)
        diag_grid.addWidget(self.btn_tur, 0, 0)
        diag_grid.addWidget(self.btn_probe, 0, 1)
        layout.addLayout(diag_grid)

        layout.addWidget(self._section_header("POWER"))
        layout.addWidget(self.btn_power)

    def _build_debug_dock(self) -> None:
        self.debug_dock = QDockWidget("Debug", self)
        self.debug_dock.setObjectName("debugDock")
        self.debug_dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self.debug_dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)

        self.txt = QPlainTextEdit()
        self.txt.setObjectName("commandLog")
        self.txt.setReadOnly(True)
        self.txt.setMinimumHeight(220)
        font = self.txt.font()
        font.setFamily("monospace")
        font.setPointSize(9)
        self.txt.setFont(font)

        self.debug_dock.setWidget(self.txt)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.debug_dock)
        self.debug_dock.visibilityChanged.connect(self._sync_debug_action)
        self.debug_dock.hide()

    def _build_progress_widgets(self) -> None:
        self._progress = QProgressBar()
        self._progress.setMaximumWidth(160)
        self._progress.setRange(0, 1)
        self._progress.hide()
        self.statusBar().addPermanentWidget(self._progress)
        self.set_status("Ready")

    def _set_debug_mode(self, checked: bool) -> None:
        if checked:
            self.debug_dock.show()
            return
        self.debug_dock.hide()

    def _sync_debug_action(self, visible: bool) -> None:
        if self.debug_mode_action.isChecked() != visible:
            self.debug_mode_action.setChecked(visible)

    def _set_controls_enabled(self, enabled: bool) -> None:
        self._busy = not enabled
        self._update_action_state()

    def _update_action_state(self) -> None:
        has_device = self.selected is not None
        self.device_cb.setEnabled(not self._busy)
        self.btn_rescan.setEnabled(not self._busy)
        for button in self.command_buttons:
            button.setEnabled((not self._busy) and has_device)

        is_apple = bool(self.selected and self.selected.is_apple_superdrive)
        self.btn_load.setEnabled((not self._busy) and is_apple)
        has_usb_sysfs = bool(self.selected and self.selected.usb_path)
        self.btn_power.setEnabled((not self._busy) and has_usb_sysfs)

    def start_busy(self, message: str) -> None:
        self.set_status(message)
        self._busy = True
        self._update_action_state()
        self._progress.setRange(0, 0)
        self._progress.show()

    def end_busy(self, message: str | None = None) -> None:
        if message:
            self.set_status(message)
        self._busy = False
        self._update_action_state()
        self._progress.setRange(0, 1)
        self._progress.hide()

    def set_status(self, status: str) -> None:
        self.statusBar().showMessage(status)

    def log(self, text: str) -> None:
        text = text.rstrip()
        if not text:
            return
        self.txt.appendPlainText(text)
        self.txt.verticalScrollBar().setValue(self.txt.verticalScrollBar().maximum())

    def refresh_devices(self) -> None:
        if not self._begin_operation("Scanning devices..."):
            return

        def task() -> None:
            try:
                devices = self.controller.discover_devices()
                sg3_ok, sg3_msg = self.controller.require_sg3()
            except (OSError, RuntimeError, ValueError) as exc:
                self.post(lambda err=exc: self.log(f"Scan failed: {err}"))
                self.post(lambda: self.end_busy("Scan failed"))
                return
            self.post(
                lambda found=devices, ok=sg3_ok, msg=sg3_msg: (
                    self._finish_device_refresh(found, ok, msg)
                )
            )

        self._bg(task)

    def _finish_device_refresh(
        self,
        devices: list[Device],
        sg3_ok: bool,
        sg3_message: str,
    ) -> None:
        try:
            self.devices = devices
            self.device_cb.blockSignals(True)
            try:
                self.device_cb.clear()
                for device in self.devices:
                    self.device_cb.addItem(device.display_name)
            finally:
                self.device_cb.blockSignals(False)

            if self.devices:
                self.device_cb.setCurrentIndex(0)
                self.on_device_selected(0)
            else:
                self.selected = None
                self._device_info.clear()
                self.set_status("No optical drives found")
            self._update_action_state()

            if not sg3_ok:
                self.log(sg3_message)
        finally:
            self.end_busy()

    def on_device_selected(self, idx: int) -> None:
        if 0 <= idx < len(self.devices):
            self.selected = self.devices[idx]
            self._device_info.update_device(self.selected)
            self.set_status(f"Selected {self.selected.display_name}")
            if self.selected.is_apple_superdrive:
                self.btn_load.setToolTip(
                    "Send Apple vendor nudge (EA 00 ...) to accept a disc"
                )
            else:
                self.btn_load.setToolTip(
                    "Disabled: this Apple-specific nudge is only enabled for "
                    "05ac:1500 SuperDrives"
                )
            self._update_action_state()
        else:
            self.selected = None
            self._device_info.clear()
            self._update_action_state()

    def _ensure_sel(self) -> Device | None:
        if not self.selected:
            QMessageBox.warning(self, "No device", "Select an optical drive first")
            return None
        return self.selected

    def _begin_operation(self, message: str) -> bool:
        if self._busy:
            self.log("Another operation is already running.")
            self.set_status("Busy")
            return False
        self.start_busy(message)
        return True

    def _validate_device_before_operation(self, device: Device) -> tuple[bool, str]:
        return self.controller.validate_device(device)

    def _active_workers(self) -> list[threading.Thread]:
        self._workers = [worker for worker in self._workers if worker.is_alive()]
        return list(self._workers)

    def _mark_worker_done(self, worker: threading.Thread) -> None:
        if worker in self._workers:
            self._workers.remove(worker)

    def _bg(self, fn: Callable[[], None]) -> None:
        def wrapped() -> None:
            try:
                fn()
            finally:
                self.post(lambda worker=thread: self._mark_worker_done(worker))

        thread = threading.Thread(
            target=wrapped,
            daemon=False,
            name="superdrive-operation",
        )
        self._workers.append(thread)
        thread.start()

    def _run_device_operation(
        self,
        device: Device,
        message: str,
        fn: Callable[[], str],
    ) -> None:
        if not self._begin_operation(message):
            return

        def runner() -> None:
            final_message = "Done"
            try:
                ok, msg = self._validate_device_before_operation(device)
                if not ok:
                    self.post(lambda m=msg: self.log(f"Device changed; aborted: {m}"))
                    final_message = "Device changed; aborted"
                    return
                final_message = fn()
            except (OSError, RuntimeError, ValueError) as exc:
                message_text = f"Error: {exc}"
                self.post(lambda m=message_text: self.log(m))
                final_message = "Error"
            finally:
                self.post(lambda m=final_message: self.end_busy(m))

        self._bg(runner)

    def do_load(self) -> None:
        device = self._ensure_sel()
        if not device:
            return
        if not device.is_apple_superdrive:
            QMessageBox.warning(
                self,
                "Unsupported device",
                "The load nudge is Apple SuperDrive-specific and is only "
                "enabled for USB ID 05ac:1500.",
            )
            return

        def task() -> str:
            rc, out, err = self.controller.send_vendor_load(device.sr_path)
            self.post(
                lambda: self.log(
                    "$ sg_raw "
                    f"{device.sr_path} EA 00 00 00 00 00 01\n"
                    f"rc={rc}\n{out}{err}"
                )
            )
            return "Done" if rc == 0 else f"Failed (rc={rc})"

        self._run_device_operation(device, "Sending vendor load...", task)

    def do_probe(self) -> None:
        device = self._ensure_sel()
        if not device:
            return

        def task() -> str:
            results = self.controller.probe_commands(device.sr_path)
            if not results:
                self.post(
                    lambda: self.log(
                        "No sg3_utils probe tools available "
                        "(sg_inq/sg_vpd/sg_modes)."
                    )
                )
            for cmd, rc, out, err in results:
                self.post(
                    lambda c=cmd, r=rc, o=out, e=err: self.log(
                        f"$ {c}\nrc={r}\n{o}{e}"
                    )
                )
            return "Done"

        self._run_device_operation(device, "Probing device...", task)

    def do_eject(self) -> None:
        device = self._ensure_sel()
        if not device:
            return

        def task() -> str:
            ok_um, info = self.controller.unmount_if_mounted(device.sr_path)
            self.post(lambda i=info: self.log(i))
            if not ok_um:
                mount_point = self.controller.get_mountpoint(device.sr_path)
                if mount_point and "busy" in info.lower():
                    holders = self.controller.holders_for_mount(mount_point)
                    self.post(lambda h=holders: self.log(h))
                self.post(lambda: self.log("Eject skipped because unmount failed."))
                return "Unmount failed; eject skipped"
            current, msg = self._validate_device_before_operation(device)
            if not current:
                self.post(lambda m=msg: self.log(f"Device changed; eject skipped: {m}"))
                return "Device changed; eject skipped"
            rc, out, err, cmd = self.controller.eject_disc(device.sr_path)
            self.post(lambda: self.log(f"$ {cmd}\nrc={rc}\n{out}{err}"))
            return "Done" if rc == 0 else f"Failed (rc={rc})"

        self._run_device_operation(
            device,
            "Unmounting (if needed) and ejecting...",
            task,
        )

    def do_lock(self, lock: bool) -> None:
        device = self._ensure_sel()
        if not device:
            return

        def task() -> str:
            rc, out, err, cmd = self.controller.lock_door(device.sr_path, lock)
            self.post(lambda: self.log(f"$ {cmd}\nrc={rc}\n{out}{err}"))
            return "Done" if rc == 0 else f"Failed (rc={rc})"

        self._run_device_operation(
            device,
            ("Locking" if lock else "Unlocking") + "...",
            task,
        )

    def do_tur(self) -> None:
        device = self._ensure_sel()
        if not device:
            return

        def task() -> str:
            mount_point = self.controller.get_mountpoint(device.sr_path)
            mount_line = (
                f"mount: {mount_point}" if mount_point else "mount: (not mounted)"
            )
            rc, out, err = self.controller.tur(device.sr_path)
            text = (out or "") + (err or "")
            if rc == 0:
                ready_msg = "status: READY"
            elif "Medium not present" in text or "tray open" in text:
                ready_msg = "status: NOT READY (no disc / tray open)"
            elif "becoming ready" in text:
                ready_msg = "status: NOT READY (waking up)"
            else:
                ready_msg = f"status: NOT READY (rc={rc})"
            self.post(lambda: self.log(f"{mount_line}\n{ready_msg}"))
            self.post(
                lambda: self.log(
                    f"$ sg_turs -vv {device.sr_path}\nrc={rc}\n{out}{err}"
                )
            )
            for cmd, rc2, out2, err2 in self.controller.probe_commands(device.sr_path):
                if cmd.startswith("sg_inq "):
                    self.post(
                        lambda c=cmd, r=rc2, o=out2, e=err2: self.log(
                            f"$ {c}\nrc={r}\n{o}{e}"
                        )
                    )
                    break
            return "Done"

        self._run_device_operation(device, "Checking status...", task)

    def do_power_keep(self) -> None:
        device = self._ensure_sel()
        if not device:
            return
        if not device.usb_path:
            QMessageBox.warning(
                self,
                "USB info not found",
                "Could not locate USB sysfs path for this device.",
            )
            return
        usb_path = Path(device.usb_path)

        def task() -> str:
            current, msg = self._validate_device_before_operation(device)
            if not current:
                self.post(lambda m=msg: self.log(f"Device changed; power skipped: {m}"))
                return "Device changed; power skipped"
            results = self.controller.power_keep_on(usb_path)
            for path, ok, msg in results:
                self.post(
                    lambda p=path, o=ok, m=msg: self.log(
                        f"{p}: {'OK' if o else 'FAIL'} ({m})"
                    )
                )
            if any((not ok) and "Permission denied" in msg for _, ok, msg in results):
                hint = (
                    "Permission denied: elevate only the sysfs writes. "
                    "Example commands:\n"
                    f"  printf '%s\\n' on | sudo tee {usb_path}/power/control "
                    ">/dev/null\n"
                    f"  printf '%s\\n' -1 | sudo tee "
                    f"{usb_path}/power/autosuspend_delay_ms >/dev/null"
                )
                self.post(lambda h=hint: self.log(h))
            return "Done"

        self._run_device_operation(device, "Disabling USB autosuspend...", task)


def main() -> int:
    app = QApplication(sys.argv)
    apply_style(app)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
