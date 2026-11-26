import importlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

LOAD_FAILURE_RC = 5
SCAN_SECRET_MESSAGE = "token=sk-secret-token-value device scan failed"


def import_public_gui():
    return importlib.import_module("superdrive.gui.main_window")


def wait_for_window_idle(window, qapp, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if not window._workers and not window._busy:
            return
        time.sleep(0.01)
    qapp.processEvents()
    assert window._workers == []
    assert not window._busy


class FakeController:
    def __init__(self):
        self.devices = []
        self.identity_result = (True, "")
        self.identity_results = []
        self.unmount_result = (True, "not mounted")
        self.eject_calls = []
        self.load_calls = []

    def discover_devices(self):
        return list(self.devices)

    def require_sg3(self):
        return True, ""

    def validate_device(self, device):
        if self.identity_results:
            return self.identity_results.pop(0)
        return self.identity_result

    def send_vendor_load(self, sr):
        self.load_calls.append(sr)
        return 0, "", ""

    def probe_commands(self, sr):
        return []

    def unmount_if_mounted(self, sr):
        return self.unmount_result

    def get_mountpoint(self, sr):
        return "/media/test"

    def holders_for_mount(self, mount_point):
        return f"holders for {mount_point}"

    def eject_disc(self, sr):
        self.eject_calls.append(sr)
        return 0, "", "", f"eject {sr}"

    def lock_door(self, sr, lock):
        return 0, "", "", f"lock {sr} {lock}"

    def tur(self, sr):
        return 0, "", ""

    def power_keep_on(self, usb_path):
        return []


class FakeCliController(FakeController):
    def __init__(self):
        super().__init__()
        gui = import_public_gui()
        self.apple = gui.Device(
            Path("/dev/sr-test0"),
            Path("/sys/usb0"),
            "05ac",
            "1500",
        )
        self.other = gui.Device(Path("/dev/sr-test1"), None, "1234", "5678")
        self.devices = [self.apple]
        self.tur_calls = []
        self.power_calls = []

    def tur(self, sr):
        self.tur_calls.append(sr)
        return 0, "ready\n", ""

    def get_mountpoint(self, sr):
        return None

    def probe_commands(self, sr):
        return [(f"sg_inq {sr}", 0, "inq\n", "")]

    def power_keep_on(self, usb_path):
        self.power_calls.append(usb_path)
        return [(f"{usb_path}/power/control", True, "set to on")]


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(scope="module")
def gui():
    return import_public_gui()


@pytest.fixture
def window(qapp, gui):
    win = gui.MainWindow(controller=FakeController())
    wait_for_window_idle(win, qapp)
    yield win

    win.close()
    win.deleteLater()
    qapp.processEvents()


def test_package_structure_exposes_canonical_layers():
    package = importlib.import_module("superdrive")
    gui_module = import_public_gui()
    drive_contracts = importlib.import_module("superdrive.drive")
    runtime_constants = importlib.import_module("superdrive.commands")

    assert package.Device is drive_contracts.Device
    assert package.DriveController is drive_contracts.DriveController
    assert gui_module.Device is drive_contracts.Device
    assert gui_module.DriveController is drive_contracts.DriveController
    assert runtime_constants.SAFE_COMMAND_PATH == "/usr/sbin:/usr/bin:/sbin:/bin"


def test_package_star_import_exports_declared_api():
    package = importlib.import_module("superdrive")
    namespace = {}

    exec("from superdrive import *", namespace)

    assert set(package.__all__) <= set(namespace)
    assert namespace["Device"] is package.Device
    assert namespace["DriveController"] is package.DriveController


@pytest.mark.parametrize(
    "module_name",
    [
        "superdrive",
        "superdrive.cli",
        "superdrive.commands",
        "superdrive.drive",
        "superdrive.gui",
        "superdrive.gui.device_info",
        "superdrive.gui.main_window",
        "superdrive.gui.theme",
    ],
)
def test_public_modules_import_from_package_root(module_name):
    module = importlib.import_module(module_name)

    assert module.__name__ == module_name


def test_cli_version_is_parseable():
    cli_app = importlib.import_module("superdrive.cli")
    runner = CliRunner()

    result = runner.invoke(cli_app.app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == cli_app.VERSION


def test_cli_device_list_json_has_clean_streams(monkeypatch):
    cli_app = importlib.import_module("superdrive.cli")
    controller = FakeCliController()
    runner = CliRunner()
    monkeypatch.setattr(cli_app, "build_controller", lambda: controller)

    result = runner.invoke(cli_app.app, ["device", "list", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert result.stderr == ""
    assert payload["kind"] == "device_list"
    assert payload["devices"][0]["sr_path"] == "/dev/sr-test0"
    assert payload["devices"][0]["is_apple_superdrive"]


def test_cli_status_json_selects_single_device(monkeypatch):
    cli_app = importlib.import_module("superdrive.cli")
    controller = FakeCliController()
    runner = CliRunner()
    monkeypatch.setattr(cli_app, "build_controller", lambda: controller)

    result = runner.invoke(cli_app.app, ["device", "status", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert result.stderr == ""
    assert payload["ready"] == "READY"
    assert controller.tur_calls == [Path("/dev/sr-test0")]


def test_cli_mutation_requires_yes_or_dry_run(monkeypatch):
    cli_app = importlib.import_module("superdrive.cli")
    controller = FakeCliController()
    runner = CliRunner()
    monkeypatch.setattr(cli_app, "build_controller", lambda: controller)

    result = runner.invoke(cli_app.app, ["device", "load", "--json"])
    payload = json.loads(result.stderr)

    assert result.exit_code == cli_app.EXIT_CONFIG
    assert result.stdout == ""
    assert payload["error"]["message"] == "load requires confirmation."
    assert "--dry-run" in payload["error"]["hint"]


def test_cli_dry_run_load_does_not_call_device(monkeypatch):
    cli_app = importlib.import_module("superdrive.cli")
    controller = FakeCliController()
    runner = CliRunner()
    monkeypatch.setattr(cli_app, "build_controller", lambda: controller)

    result = runner.invoke(cli_app.app, ["device", "load", "--dry-run", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["status"] == "dry_run"
    assert controller.load_calls == []


def test_cli_load_failure_is_structured_error(monkeypatch):
    cli_app = importlib.import_module("superdrive.cli")
    controller = FakeCliController()
    runner = CliRunner()
    monkeypatch.setattr(cli_app, "build_controller", lambda: controller)

    def fail_load(sr):
        controller.load_calls.append(sr)
        return LOAD_FAILURE_RC, "", "sg_raw failed"

    controller.send_vendor_load = fail_load

    result = runner.invoke(cli_app.app, ["device", "load", "--yes", "--json"])
    payload = json.loads(result.stderr)

    assert result.exit_code == cli_app.EXIT_GENERIC
    assert result.stdout == ""
    assert payload["error"]["message"] == "load failed."
    assert "probe" in payload["error"]["hint"]
    assert payload["error"]["details"]["returncode"] == LOAD_FAILURE_RC
    assert payload["error"]["details"]["stderr"] == "sg_raw failed"
    assert controller.load_calls == [Path("/dev/sr-test0")]


@pytest.mark.parametrize(
    "args",
    [
        ["device", "list", "--bogus", "--json"],
        ["device", "status", "--json", "--device"],
    ],
)
def test_cli_parse_errors_are_json_when_json_flag_is_present(args):
    cli_app = importlib.import_module("superdrive.cli")
    runner = CliRunner()

    result = runner.invoke(cli_app.app, args)
    payload = json.loads(result.stderr)

    assert result.exit_code == cli_app.EXIT_CONFIG
    assert result.stdout == ""
    assert payload["error"]["message"]
    assert payload["error"]["hint"]


def test_cli_unexpected_runtime_error_is_json_and_sanitized(monkeypatch):
    cli_app = importlib.import_module("superdrive.cli")
    controller = FakeCliController()
    runner = CliRunner()
    monkeypatch.setattr(cli_app, "build_controller", lambda: controller)

    def fail_discovery():
        raise RuntimeError(SCAN_SECRET_MESSAGE)

    controller.discover_devices = fail_discovery

    result = runner.invoke(cli_app.app, ["device", "list", "--json"])
    payload = json.loads(result.stderr)
    serialized = json.dumps(payload)

    assert result.exit_code == cli_app.EXIT_GENERIC
    assert result.stdout == ""
    assert payload["error"]["message"] == "Operation failed."
    assert payload["error"]["details"]["exception"] == "RuntimeError"
    assert "sk-secret-token-value" not in serialized
    assert "[REDACTED]" in serialized


@pytest.mark.parametrize(
    "args",
    [
        ["device", "load", "--yes", "--dry-run", "--json"],
        ["device", "eject", "--yes", "--dry-run", "--json"],
        ["device", "lock", "--yes", "--dry-run", "--json"],
        ["device", "power", "--yes", "--dry-run", "--json"],
    ],
)
def test_cli_rejects_conflicting_mutation_confirmation(args, monkeypatch):
    cli_app = importlib.import_module("superdrive.cli")
    controller = FakeCliController()
    runner = CliRunner()
    monkeypatch.setattr(cli_app, "build_controller", lambda: controller)

    result = runner.invoke(cli_app.app, args)
    payload = json.loads(result.stderr)

    assert result.exit_code == cli_app.EXIT_CONFIG
    assert result.stdout == ""
    assert "conflicting confirmation flags" in payload["error"]["message"]


def test_cli_missing_device_uses_not_found_exit(monkeypatch):
    cli_app = importlib.import_module("superdrive.cli")
    controller = FakeCliController()
    controller.devices = []
    runner = CliRunner()
    monkeypatch.setattr(cli_app, "build_controller", lambda: controller)

    result = runner.invoke(cli_app.app, ["device", "status", "--json"])
    payload = json.loads(result.stderr)

    assert result.exit_code == cli_app.EXIT_NOT_FOUND
    assert result.stdout == ""
    assert payload["error"]["message"] == "No optical drives found."


def test_cli_requires_explicit_device_when_multiple_drives(monkeypatch):
    cli_app = importlib.import_module("superdrive.cli")
    controller = FakeCliController()
    controller.devices = [controller.apple, controller.other]
    runner = CliRunner()
    monkeypatch.setattr(cli_app, "build_controller", lambda: controller)

    result = runner.invoke(cli_app.app, ["device", "status", "--json"])
    payload = json.loads(result.stderr)

    assert result.exit_code == cli_app.EXIT_CONFIG
    assert result.stdout == ""
    assert payload["error"]["message"] == "Multiple optical drives found."
    assert "--device /dev/srX" in payload["error"]["hint"]
    assert controller.tur_calls == []


def test_cli_explicit_device_selects_matching_drive(monkeypatch):
    cli_app = importlib.import_module("superdrive.cli")
    controller = FakeCliController()
    controller.devices = [controller.apple, controller.other]
    runner = CliRunner()
    monkeypatch.setattr(cli_app, "build_controller", lambda: controller)

    result = runner.invoke(
        cli_app.app,
        ["device", "status", "--device", "/dev/sr-test1", "--json"],
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert result.stderr == ""
    assert payload["device"]["sr_path"] == "/dev/sr-test1"
    assert payload["device"]["is_apple_superdrive"] is False
    assert controller.tur_calls == [Path("/dev/sr-test1")]


def test_cli_rejects_load_for_non_apple_drive(monkeypatch):
    cli_app = importlib.import_module("superdrive.cli")
    controller = FakeCliController()
    controller.devices = [controller.other]
    runner = CliRunner()
    monkeypatch.setattr(cli_app, "build_controller", lambda: controller)

    result = runner.invoke(cli_app.app, ["device", "load", "--yes", "--json"])
    payload = json.loads(result.stderr)

    assert result.exit_code == cli_app.EXIT_CONFIG
    assert result.stdout == ""
    assert payload["error"]["message"] == (
        "Load is only supported for Apple SuperDrive USB ID 05ac:1500."
    )
    assert controller.load_calls == []


def test_cli_eject_skips_hardware_when_unmount_fails(monkeypatch):
    cli_app = importlib.import_module("superdrive.cli")
    controller = FakeCliController()
    controller.unmount_result = (False, "udisksctl rc=1\nbusy")
    runner = CliRunner()
    monkeypatch.setattr(cli_app, "build_controller", lambda: controller)

    result = runner.invoke(cli_app.app, ["device", "eject", "--yes", "--json"])
    payload = json.loads(result.stderr)

    assert result.exit_code == cli_app.EXIT_GENERIC
    assert result.stdout == ""
    assert payload["error"]["message"] == "Unmount failed; eject was skipped."
    assert payload["error"]["details"] == {
        "ejected": False,
        "unmount": "udisksctl rc=1 busy",
    }
    assert controller.eject_calls == []


def test_cli_import_does_not_load_qt_runtime():
    code = (
        "import sys;"
        "sys.path.insert(0, '.');"
        "import superdrive.cli as cli;"
        "print(any(name.startswith('PySide6') for name in sys.modules))"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=True,
        text=True,
    )

    assert result.stdout.strip() == "False"


def test_cli_rejects_malformed_device_path(monkeypatch):
    cli_app = importlib.import_module("superdrive.cli")
    controller = FakeCliController()
    runner = CliRunner()
    monkeypatch.setattr(cli_app, "build_controller", lambda: controller)

    result = runner.invoke(
        cli_app.app,
        ["device", "status", "--device", "/dev/sr0?bad", "--json"],
    )
    payload = json.loads(result.stderr)

    assert result.exit_code == cli_app.EXIT_CONFIG
    assert result.stdout == ""
    assert payload["error"]["message"] == "Invalid device path."


def test_cli_schema_and_completion_commands():
    cli_app = importlib.import_module("superdrive.cli")
    runner = CliRunner()

    schema_result = runner.invoke(cli_app.app, ["schema", "describe", "--json"])
    completion_result = runner.invoke(cli_app.app, ["completion", "bash"])
    completion_json_result = runner.invoke(
        cli_app.app,
        ["completion", "bash", "--json"],
    )
    schema = json.loads(schema_result.stdout)
    completion_payload = json.loads(completion_json_result.stdout)

    assert schema_result.exit_code == 0
    assert schema["error_shape"] == {
        "error": {
            "message": "string",
            "hint": "string",
            "details": "object optional",
        }
    }
    assert schema["commands"]["completion bash"]["json"]
    assert completion_result.exit_code == 0
    assert "_superdrive_cli_completion" in completion_result.stdout
    assert completion_json_result.exit_code == 0
    assert completion_json_result.stderr == ""
    assert completion_payload["kind"] == "completion"
    assert completion_payload["shell"] == "bash"
    assert completion_payload["program"] == "superdrive-cli"
    assert "_superdrive_cli_completion" in completion_payload["source"]


def test_cli_is_split_into_resource_modules():
    for module_name in (
        "superdrive.cli.app",
        "superdrive.cli.completion",
        "superdrive.cli.core",
        "superdrive.cli.device",
        "superdrive.cli.schema",
    ):
        module = importlib.import_module(module_name)

        assert module.__name__ == module_name


def test_cli_version_has_json_mode():
    cli_app = importlib.import_module("superdrive.cli")
    runner = CliRunner()

    result = runner.invoke(cli_app.app, ["--version", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert result.stderr == ""
    assert payload == {"kind": "version", "version": cli_app.VERSION}


def test_cli_rejects_path_traversal_device_path(monkeypatch):
    cli_app = importlib.import_module("superdrive.cli")
    controller = FakeCliController()
    runner = CliRunner()
    monkeypatch.setattr(cli_app, "build_controller", lambda: controller)

    result = runner.invoke(
        cli_app.app,
        ["device", "status", "--device", "/dev/../etc/passwd", "--json"],
    )
    payload = json.loads(result.stderr)

    assert result.exit_code == cli_app.EXIT_CONFIG
    assert result.stdout == ""
    assert payload["error"]["message"] == "Invalid device path."


def test_cli_top_level_help_lists_resource_commands():
    cli_app = importlib.import_module("superdrive.cli")
    runner = CliRunner()

    result = runner.invoke(cli_app.app, ["--help"])

    assert result.exit_code == 0
    assert "device list" in result.stdout
    assert "--install-completion" not in result.stdout


def test_canonical_device_contract_preserves_display_semantics():
    drive_contracts = importlib.import_module("superdrive.drive")

    device = drive_contracts.Device(Path("/dev/sr-test"), None, "05AC", "1500")

    assert device.vendor == "05ac"
    assert device.product == "1500"
    assert device.is_apple_superdrive
    assert device.display_name == "Apple SuperDrive (/dev/sr-test) [05ac:1500]"


def test_canonical_backend_run_returns_timeout_rc(monkeypatch):
    commands = importlib.import_module("superdrive.commands")

    def raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["slow-command"], timeout=3)

    monkeypatch.setattr(commands.subprocess, "run", raise_timeout)

    rc, out, err = commands.run(["/usr/bin/slow-command"])

    assert rc == commands.TIMEOUT_RC
    assert out == ""
    assert isinstance(err, str)


def test_backend_vendor_load_sends_exact_apple_cdb(monkeypatch):
    commands = importlib.import_module("superdrive.commands")
    calls = []

    def fake_run(cmd, use_shell=False, timeout=commands.COMMAND_TIMEOUT_SECONDS):
        calls.append((cmd, use_shell, timeout))
        return 0, "ok", ""

    monkeypatch.setattr(commands, "run", fake_run)

    rc, out, err = commands.send_vendor_load(Path("/dev/sr0"))

    assert (rc, out, err) == (0, "ok", "")
    assert calls == [
        (
            ["sg_raw", "/dev/sr0", "EA", "00", "00", "00", "00", "00", "01"],
            False,
            commands.COMMAND_TIMEOUT_SECONDS,
        )
    ]


def test_backend_rejects_shell_and_unsafe_absolute_commands():
    commands = importlib.import_module("superdrive.commands")

    shell_rc, _, shell_err = commands.run("echo unsafe", use_shell=True)
    path_rc, _, path_err = commands.run(["/tmp/sg_raw"])

    assert shell_rc == commands.COMMAND_NOT_FOUND_RC
    assert shell_err == "Shell execution is disabled"
    assert path_rc == commands.COMMAND_NOT_FOUND_RC
    assert "safe path" in path_err


def test_drive_controller_uses_injected_service_without_discovery():
    controller_module = importlib.import_module("superdrive.drive")
    drive_contracts = importlib.import_module("superdrive.drive")
    device = drive_contracts.Device(Path("/dev/sr-test"), None, "05ac", "1500")

    class FakeDriveService:
        def discover_devices(self):
            return [device]

        def require_sg3(self):
            return True, ""

        def device_identity_is_current(self, value):
            assert value is device
            return True, ""

    controller = controller_module.DriveController(FakeDriveService())

    assert controller.discover_devices() == [device]
    assert controller.require_sg3() == (True, "")
    assert controller.validate_device(device) == (True, "")


def test_drive_service_power_keep_on_writes_expected_sysfs_values(tmp_path):
    service_module = importlib.import_module("superdrive.drive")
    usb_path = tmp_path / "usb-device"
    power_path = usb_path / "power"
    power_path.mkdir(parents=True)
    control = power_path / "control"
    delay = power_path / "autosuspend_delay_ms"
    control.write_text("auto")
    delay.write_text("2000")

    service = service_module.DriveService()
    results = service.power_keep_on(usb_path)

    assert results == [
        (str(control), True, "set to on"),
        (str(delay), True, "set to -1"),
    ]
    assert control.read_text() == "on"
    assert delay.read_text() == "-1"


def test_drive_service_can_discover_and_revalidate_fake_sysfs(tmp_path):
    service_module = importlib.import_module("superdrive.drive")
    dev_root = tmp_path / "dev"
    sys_root = tmp_path / "sys" / "class" / "block"
    sr0 = dev_root / "sr0"
    sys_sr0 = sys_root / "sr0"
    dev_root.mkdir()
    sys_sr0.mkdir(parents=True)
    sr0.touch()
    (sys_sr0 / "idVendor").write_text("05AC")
    (sys_sr0 / "idProduct").write_text("1500")

    service = service_module.DriveService(dev_root=dev_root, sys_block_root=sys_root)
    devices = service.discover_devices()

    assert len(devices) == 1
    assert devices[0].sr_path == sr0
    assert devices[0].usb_path == sys_sr0
    assert devices[0].is_apple_superdrive

    (sys_sr0 / "idProduct").write_text("9999")
    ok, message = service.device_identity_is_current(devices[0])

    assert not ok
    assert "USB ID changed" in message


def test_eject_flow_does_not_eject_after_failed_unmount(gui, window):
    selected = gui.Device(Path("/dev/sr-test"), None, "05ac", "1500")

    window.selected = selected
    window.post = lambda fn: fn()
    window._bg = lambda fn, *args: fn(*args)
    window.controller.unmount_result = (
        False,
        f"udisksctl rc=1\nfailed to unmount {selected.sr_path}",
    )

    window.do_eject()

    assert window.controller.eject_calls == []


def test_eject_revalidates_before_final_eject(gui, window):
    selected = gui.Device(Path("/dev/sr-test"), None, "05ac", "1500")

    window.selected = selected
    window.post = lambda fn: fn()
    window._bg = lambda fn, *args: fn(*args)
    window.controller.unmount_result = (True, "unmounted")
    window.controller.identity_results = [
        (True, ""),
        (False, "USB parent changed"),
    ]

    window.do_eject()

    assert window.controller.eject_calls == []
    assert not window._busy


def test_operation_rejects_second_action_while_busy(gui, window):
    selected = gui.Device(Path("/dev/sr-test"), None, "05ac", "1500")
    window.selected = selected
    window._busy = True

    try:
        window.do_load()
    finally:
        window._busy = False

    assert window.controller.load_calls == []


def test_operation_aborts_when_selected_device_identity_changed(gui, window):
    selected = gui.Device(Path("/dev/sr-test"), None, "05ac", "1500")

    window.selected = selected
    window.post = lambda fn: fn()
    window._bg = lambda fn, *args: fn(*args)
    window.controller.identity_result = (False, "USB parent changed")

    window.do_load()

    assert window.controller.load_calls == []
    assert not window._busy


def test_load_button_disabled_for_non_apple_and_enabled_for_apple(gui, window):
    non_apple = gui.Device(Path("/dev/sr0"), None, "1234", "5678")
    apple = gui.Device(Path("/dev/sr1"), None, "05ac", "1500")
    window.devices = [non_apple, apple]

    window.on_device_selected(0)
    assert not window.btn_load.isEnabled()

    window.on_device_selected(1)
    assert window.btn_load.isEnabled()


def test_refresh_devices_preserves_no_device_status(window):
    assert window.statusBar().currentMessage() == "No optical drives found"


def test_refresh_devices_runs_discovery_off_ui_thread(qapp, gui):
    main_thread = threading.get_ident()

    class ThreadRecordingController(FakeController):
        def __init__(self):
            super().__init__()
            self.discovery_threads = []

        def discover_devices(self):
            self.discovery_threads.append(threading.get_ident())
            return []

    controller = ThreadRecordingController()
    win = gui.MainWindow(controller=controller)
    try:
        wait_for_window_idle(win, qapp)
        assert controller.discovery_threads
        assert controller.discovery_threads[0] != main_thread
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()


def test_busy_state_disables_probe_with_other_command_buttons(window):
    command_buttons = [
        window.btn_unlock,
        window.btn_load,
        window.btn_tur,
        window.btn_probe,
        window.btn_lock,
        window.btn_eject,
        window.btn_power,
    ]

    window.start_busy("Working...")
    try:
        assert all(not button.isEnabled() for button in command_buttons)
    finally:
        window.end_busy("Ready")


def test_close_event_rejects_while_operation_is_busy(monkeypatch, window):
    main_window_module = importlib.import_module("superdrive.gui.main_window")
    warnings = []

    class FakeCloseEvent:
        def __init__(self):
            self.ignored = False

        def ignore(self):
            self.ignored = True

    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "warning",
        lambda *args: warnings.append(args),
    )
    window._busy = True
    event = FakeCloseEvent()

    try:
        window.closeEvent(event)
    finally:
        window._busy = False

    assert event.ignored
    assert warnings == [
        (
            window,
            "Operation in progress",
            "Wait for the current drive operation to finish before closing.",
        )
    ]


def test_close_event_rejects_while_worker_is_active(monkeypatch, window):
    main_window_module = importlib.import_module("superdrive.gui.main_window")
    warnings = []

    class FakeCloseEvent:
        def __init__(self):
            self.ignored = False

        def ignore(self):
            self.ignored = True

    class WorkerProbe:
        def __init__(self, alive):
            self.alive = alive

        def is_alive(self):
            return self.alive

    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "warning",
        lambda *args: warnings.append(args),
    )
    active_worker = WorkerProbe(True)
    window._workers = [WorkerProbe(False), active_worker]
    event = FakeCloseEvent()

    try:
        window.closeEvent(event)
        assert event.ignored
        assert warnings
        assert window._workers == [active_worker]
    finally:
        window._workers = []


def test_active_workers_prunes_completed_threads(window):
    class WorkerProbe:
        def __init__(self, alive):
            self.alive = alive

        def is_alive(self):
            return self.alive

    active_worker = WorkerProbe(True)
    window._workers = [WorkerProbe(False), active_worker, WorkerProbe(False)]

    try:
        assert window._active_workers() == [active_worker]
        assert window._workers == [active_worker]
    finally:
        window._workers = []


def test_background_worker_is_tracked_and_pruned(window, qapp):
    done = threading.Event()

    window._bg(lambda: done.set())

    assert window._workers
    assert done.wait(timeout=1)
    wait_for_window_idle(window, qapp)

    assert window._workers == []
