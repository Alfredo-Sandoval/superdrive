#!/usr/bin/env python3
"""Qt window for the Apple SuperDrive helper on Linux.

The window shows the SuperDrive's state and offers wake, eject and open-disc
actions. Every drive operation runs superdrive.py in a QProcess, so the window
needs no threads: ``status --json`` for the periodic status check, ``eject`` and
``open`` as the desktop user, and ``wake`` through pkexec because the wake
command needs root.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6 import __version__ as PYSIDE_VERSION
from PySide6.QtCore import QObject, QPointF, QProcess, QRectF, Qt, QTimer, QVariantAnimation, Signal, qVersion
from PySide6.QtGui import (
    QColor, QGuiApplication, QIcon, QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen,
    QPolygonF, QRadialGradient, QShortcut,
)
from PySide6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

HELPER_NAME = "superdrive.py"
POLL_INTERVAL_MS = 3000
DASH = "—"

# Same colors as the Windows window, so both versions look alike.
LIGHT = {
    "Bg": "#F3F4F6", "Surface": "#FFFFFF", "Surface2": "#F6F7F9", "Surface3": "#ECEEF1",
    "Stroke": "#E2E5E9", "Text": "#16191D", "Muted": "#5E6670", "Accent": "#2563EB",
    "AccentHover": "#1D4ED8", "AccentPressed": "#1E40AF", "OnAccent": "#FFFFFF",
    "Success": "#15803D", "Warning": "#B45309", "Danger": "#DC2626", "Neutral": "#8A929C",
    "LogBg": "#F6F7F9",
}
DARK = {
    "Bg": "#191B1F", "Surface": "#22252A", "Surface2": "#2A2E34", "Surface3": "#333840",
    "Stroke": "#383D45", "Text": "#ECEEF1", "Muted": "#A0A7B1", "Accent": "#4C8DFF",
    "AccentHover": "#6BA1FF", "AccentPressed": "#3B78E7", "OnAccent": "#0B1220",
    "Success": "#3FB950", "Warning": "#E0A43A", "Danger": "#F26B6B", "Neutral": "#6E7681",
    "LogBg": "#1C1F23",
}

WAKE_SOURCES = {"udev": "automatically on plug-in", "gui": "from this window", "cli": "from the command line"}
COMMAND_NAMES = {"wake": "Wake", "eject": "Eject", "open": "Open disc"}


# --- What the status area says ------------------------------------------------------------

@dataclass(frozen=True)
class HeroView:
    tone: str                   # palette key for the badge, glow and status dot
    badge: str
    headline: str
    detail: str
    spinning: bool = False
    action: str | None = None   # "wake" or "open": what the main button does
    action_text: str = ""


def describe_state(snapshot: dict | None, busy: tuple[str, str] | None) -> HeroView:
    """Chooses the status area's content from the latest ``status --json`` snapshot."""
    if busy is not None:
        return HeroView("Accent", "Working", busy[0], busy[1], spinning=True)
    if snapshot is None:
        return HeroView("Neutral", "Starting", "Looking for your SuperDrive",
                        "Checking the optical drives on this computer.", spinning=True)

    auto_wake = snapshot.get("auto_wake_installed", False)
    drive = first_drive(snapshot)
    if drive is None:
        if auto_wake:
            detail = "Plug the drive straight into a USB port. It is woken automatically as soon as it appears."
        else:
            detail = "Plug the drive straight into a USB port, then press Wake. Run install.sh to have it woken automatically."
        return HeroView("Neutral", "Not connected", "No SuperDrive connected", detail)

    media = drive["media"]
    wake = drive["wake"]
    if media["mounted_at"]:
        return HeroView("Success", "Disc mounted", "Disc ready", media_summary(media),
                        action="open", action_text="Open disc")
    if media["present"] and media["fs_type"]:
        return HeroView("Accent", "Disc inserted", "Disc inserted",
                        "It is not mounted yet. Open disc mounts it and shows its files.",
                        action="open", action_text="Open disc")
    if media["present"]:
        return HeroView("Accent", "Disc inserted", "Reading the disc",
                        "Waiting for Linux to identify it. A blank or audio disc stays here; use Eject to take it out.",
                        spinning=True)
    if wake is not None and wake["ok"]:
        how = " It was woken automatically when you plugged it in." if wake.get("source") == "udev" else ""
        return HeroView("Success", "Awake", "Ready for a disc", f"Slide a disc into the slot.{how}")
    if wake is not None:
        return HeroView("Danger", "Needs attention", "The drive did not wake",
                        f"{wake['detail']}\nTry another USB port or cable, then press Try again.",
                        action="wake", action_text="Try again")

    if auto_wake:
        detail = "Auto-wake did not run for this connection. Press Wake drive, or unplug the drive and plug it back in."
    else:
        detail = "It ignores discs until it receives the wake command."
    return HeroView("Warning", "Asleep", "The drive needs waking", detail, action="wake", action_text="Wake drive")


def first_drive(snapshot: dict | None) -> dict | None:
    if not snapshot or not snapshot.get("drives"):
        return None
    return snapshot["drives"][0]


def drive_key(drive: dict | None) -> tuple[str, str | None] | None:
    """Identifies one connection of a drive: a replug gets a new USB device number."""
    if drive is None:
        return None
    return drive["device"], drive["plug_id"]


def media_summary(media: dict) -> str:
    parts = [media["label"] or "Unlabeled disc"]
    if media["fs_type"]:
        parts.append(media["fs_type"].upper())
    if media["size_bytes"]:
        parts.append(format_bytes(media["size_bytes"]))
    if media["mounted_at"]:
        parts.append(media["mounted_at"])
    return " · ".join(parts)


def format_bytes(size: int | None) -> str:
    if size is None:
        return "Unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_record_time(text: str) -> str:
    try:
        return datetime.fromisoformat(text).strftime("%H:%M:%S")
    except ValueError:
        return text


# --- Theme --------------------------------------------------------------------------------

def prefers_dark() -> bool:
    hints = QGuiApplication.styleHints()
    color_scheme = getattr(hints, "colorScheme", None)  # Qt 6.5 and later
    if color_scheme is not None:
        scheme = color_scheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        if scheme == Qt.ColorScheme.Light:
            return False
    # Older Qt, or a desktop that reports no scheme: judge by the window color.
    return QGuiApplication.palette().window().color().lightness() < 128


def mix(first: str, second: str, amount: float) -> str:
    a, b = QColor(first), QColor(second)
    return QColor(
        round(a.red() + (b.red() - a.red()) * amount),
        round(a.green() + (b.green() - a.green()) * amount),
        round(a.blue() + (b.blue() - a.blue()) * amount),
    ).name()


def build_stylesheet(p: dict[str, str]) -> str:
    accent_disabled = mix(p["Accent"], p["Surface"], 0.55)
    accent_disabled_text = mix(p["OnAccent"], p["Surface"], 0.35)
    subtle_disabled_text = mix(p["Text"], p["Surface2"], 0.55)
    return f"""
    QWidget#Root {{ background: {p['Bg']}; color: {p['Text']}; font-size: 13px; }}
    QLabel {{ background: transparent; color: {p['Text']}; }}
    QLabel#Title {{ font-size: 20px; font-weight: 600; }}
    QLabel#Subtitle, QLabel#Detail, QLabel#CardTitle, QLabel#FieldLabel {{ color: {p['Muted']}; }}
    QLabel#Headline {{ font-size: 26px; font-weight: 600; }}
    QLabel#Detail {{ font-size: 14px; }}
    QLabel#CardTitle {{ font-size: 12px; font-weight: 600; }}
    QLabel#FieldValue {{ font-weight: 600; }}
    QLabel#Notice {{ color: {p['Danger']}; }}
    QLabel#Pill {{
        background: {p['Surface2']}; border: 1px solid {p['Stroke']}; border-radius: 10px;
        padding: 3px 10px; color: {p['Muted']}; font-size: 12px;
    }}
    QFrame#Card {{ background: {p['Surface']}; border: 1px solid {p['Stroke']}; border-radius: 12px; }}

    QPushButton {{ font-size: 13px; font-weight: 600; padding: 8px 16px; border-radius: 8px; min-width: 52px; }}
    QPushButton[kind="accent"] {{ background: {p['Accent']}; color: {p['OnAccent']}; border: 1px solid {p['Accent']}; }}
    QPushButton[kind="accent"]:hover {{ background: {p['AccentHover']}; border-color: {p['AccentHover']}; }}
    QPushButton[kind="accent"]:pressed {{ background: {p['AccentPressed']}; border-color: {p['AccentPressed']}; }}
    QPushButton[kind="accent"]:focus {{ border-color: {p['Text']}; }}
    QPushButton[kind="accent"]:disabled {{ background: {accent_disabled}; border-color: {accent_disabled}; color: {accent_disabled_text}; }}
    QPushButton[kind="subtle"] {{ background: {p['Surface2']}; color: {p['Text']}; border: 1px solid {p['Stroke']}; }}
    QPushButton[kind="subtle"]:hover {{ background: {p['Surface3']}; }}
    QPushButton[kind="subtle"]:pressed {{ background: {p['Stroke']}; }}
    QPushButton[kind="subtle"]:focus {{ border-color: {p['Accent']}; }}
    QPushButton[kind="subtle"]:disabled {{ color: {subtle_disabled_text}; }}
    QPushButton[kind="link"] {{ background: transparent; border: 1px solid transparent; color: {p['Accent']}; padding: 2px 4px; min-width: 0; }}
    QPushButton[kind="link"]:hover {{ color: {p['AccentHover']}; }}
    QPushButton[kind="link"]:focus {{ border-color: {p['Accent']}; border-radius: 4px; }}

    QPlainTextEdit#Log {{
        background: {p['LogBg']}; color: {p['Muted']}; border: none; border-radius: 8px; padding: 6px 8px;
        font-family: monospace; font-size: 12px;
        selection-background-color: {p['Accent']}; selection-color: {p['OnAccent']};
    }}
    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px 0; }}
    QScrollBar::handle:vertical {{ background: {p['Neutral']}; border-radius: 4px; min-height: 24px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
    QToolTip {{ background: {p['Surface']}; color: {p['Text']}; border: 1px solid {p['Stroke']}; }}
    """


# --- Drawn widgets ------------------------------------------------------------------------

class DriveArt(QWidget):
    """The slot-loading drive, with a glow and status dot in the state's color."""

    CANVAS_WIDTH = 240.0
    CANVAS_HEIGHT = 120.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(220, 110)
        self._tone = QColor("#8A929C")
        self._disc = False

    def set_state(self, tone: QColor, disc: bool) -> None:
        self._tone = QColor(tone)
        self._disc = disc
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(self.width() / self.CANVAS_WIDTH, self.height() / self.CANVAS_HEIGHT)

        # A circular radial gradient squashed into an ellipse under the drive.
        painter.save()
        painter.translate(120, 88)
        painter.scale(1.0, 0.24)
        glow = QRadialGradient(0, 0, 100)
        inner = QColor(self._tone)
        inner.setAlphaF(0.55)
        outer = QColor(self._tone)
        outer.setAlphaF(0.0)
        glow.setColorAt(0.0, inner)
        glow.setColorAt(1.0, outer)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(QPointF(0, 0), 100, 100)
        painter.restore()

        top = QPolygonF([QPointF(34, 20), QPointF(206, 20), QPointF(234, 58), QPointF(6, 58)])
        top_fill = QLinearGradient(0, 20, 0, 58)
        top_fill.setColorAt(0.0, QColor("#F4F5F7"))
        top_fill.setColorAt(1.0, QColor("#C9CDD3"))
        painter.setPen(QPen(QColor("#9CA3AF"), 0.75))
        painter.setBrush(top_fill)
        painter.drawPolygon(top)

        # Front face: rounded only at the bottom corners.
        rounded = QPainterPath()
        rounded.addRoundedRect(QRectF(6, 48, 228, 26), 7, 7)
        lower = QPainterPath()
        lower.addRect(QRectF(6, 58, 228, 16))
        front_fill = QLinearGradient(0, 58, 0, 74)
        front_fill.setColorAt(0.0, QColor("#B8BDC5"))
        front_fill.setColorAt(1.0, QColor("#8F959E"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(front_fill)
        painter.drawPath(rounded.intersected(lower))

        slot = QPainterPath()
        slot.addRoundedRect(QRectF(64, 64.5, 112, 3), 1.5, 1.5)
        painter.setBrush(QColor("#23272E"))
        painter.drawPath(slot)

        painter.setBrush(self._tone)
        painter.drawEllipse(QRectF(212, 63, 6, 6))

        if self._disc:
            disc_fill = QLinearGradient(62, 8, 178, 38)
            disc_fill.setColorAt(0.0, QColor("#EEF1F5"))
            disc_fill.setColorAt(0.4, QColor("#C7D2FE"))
            disc_fill.setColorAt(0.7, QColor("#FBCFE8"))
            disc_fill.setColorAt(1.0, QColor("#E5E7EB"))
            painter.setPen(QPen(QColor("#9CA3AF"), 0.6))
            painter.setBrush(disc_fill)
            painter.drawEllipse(QRectF(62, 8, 116, 30))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#AEB4BD"))
            painter.drawEllipse(QRectF(108, 19.5, 24, 7))
            painter.setBrush(QColor("#6B7280"))
            painter.drawEllipse(QRectF(115, 21.6, 10, 2.8))


class Spinner(QWidget):
    """A small rotating arc; it animates only while visible."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(12, 12)
        self._color = QColor("#8A929C")
        self._angle = 0.0
        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(360.0)
        self._animation.setDuration(900)
        self._animation.setLoopCount(-1)
        self._animation.valueChanged.connect(self._set_angle)

    def set_color(self, color: QColor) -> None:
        self._color = QColor(color)
        self.update()

    def _set_angle(self, value: float) -> None:
        self._angle = float(value)
        self.update()

    def showEvent(self, event) -> None:
        self._animation.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._animation.stop()
        super().hideEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(6, 6)
        painter.rotate(self._angle)
        pen = QPen(self._color, 1.75)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(QRectF(-4.75, -4.75, 9.5, 9.5), 90 * 16, -270 * 16)


# --- Processes ----------------------------------------------------------------------------

class DriveMonitor(QObject):
    """Runs ``superdrive.py status --json`` on a timer, one process at a time.

    ``invalidate()`` drops the result of a status check that started before an action
    changed the drive, so a stale snapshot never overwrites a newer state.
    """

    snapshot_ready = Signal(object)
    failed = Signal(str)

    def __init__(self, helper: Path, interval_ms: int, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._helper = helper
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.poll)
        self._process: QProcess | None = None
        self._generation = 0
        self._poll_again = False

    def start(self) -> None:
        self._timer.start()
        self.poll()

    def stop(self) -> None:
        self._timer.stop()
        if self._process is not None:
            self._process.kill()
            self._process.waitForFinished(1000)

    def invalidate(self) -> None:
        self._generation += 1

    def poll(self) -> None:
        if self._process is not None:
            self._poll_again = True
            return

        process = QProcess(self)
        process.setProgram(sys.executable)
        process.setArguments(["-I", str(self._helper), "status", "--json"])
        generation = self._generation
        process.finished.connect(
            lambda exit_code, exit_status, p=process, g=generation: self._finished(p, g, exit_code, exit_status))
        process.errorOccurred.connect(lambda error, p=process: self._error(p, error))
        self._process = process
        process.start()

    def _finished(self, process: QProcess, generation: int, exit_code: int, exit_status) -> None:
        self._process = None
        output = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        errors = bytes(process.readAllStandardError()).decode("utf-8", errors="replace").strip()
        process.deleteLater()

        if generation != self._generation:
            self._poll_again = True
        elif exit_status != QProcess.ExitStatus.NormalExit or exit_code != 0:
            self.failed.emit(errors or f"the status check exited with code {exit_code}")
        else:
            try:
                self.snapshot_ready.emit(json.loads(output))
            except ValueError as error:
                self.failed.emit(f"the status check printed unreadable output ({error})")

        if self._poll_again:
            self._poll_again = False
            self.poll()

    def _error(self, process: QProcess, error) -> None:
        # Only a failed start goes here; every other error is followed by finished().
        if error != QProcess.ProcessError.FailedToStart:
            return
        self._process = None
        process.deleteLater()
        self.failed.emit(f"could not start {sys.executable}: {process.errorString()}")


class CommandRunner(QObject):
    """Runs one wake, eject or open command at a time and reports how it ended."""

    # kind, exit code (-1: crashed, -2: did not start), stdout, stderr
    finished = Signal(str, int, str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._process: QProcess | None = None

    def is_running(self) -> bool:
        return self._process is not None

    def run(self, kind: str, program: str, arguments: list[str]) -> None:
        if self._process is not None:
            raise RuntimeError("A drive command is already running.")
        process = QProcess(self)
        process.setProgram(program)
        process.setArguments(arguments)
        process.finished.connect(
            lambda exit_code, exit_status, p=process, k=kind:
            self._finished(p, k, exit_code if exit_status == QProcess.ExitStatus.NormalExit else -1))
        process.errorOccurred.connect(lambda error, p=process, k=kind: self._error(p, k, error))
        self._process = process
        process.start()

    def stop(self) -> None:
        if self._process is not None:
            self._process.kill()
            self._process.waitForFinished(1000)

    def _finished(self, process: QProcess, kind: str, exit_code: int) -> None:
        self._process = None
        output = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        errors = bytes(process.readAllStandardError()).decode("utf-8", errors="replace")
        process.deleteLater()
        self.finished.emit(kind, exit_code, output, errors)

    def _error(self, process: QProcess, kind: str, error) -> None:
        if error != QProcess.ProcessError.FailedToStart:
            return
        self._process = None
        message = f"could not start {process.program()}: {process.errorString()}"
        process.deleteLater()
        self.finished.emit(kind, -2, "", message)


# --- Window -------------------------------------------------------------------------------

class MainWindow(QWidget):
    def __init__(self, helper: Path, log_path: Path) -> None:
        super().__init__()
        self.setObjectName("Root")
        self.setWindowTitle("Apple SuperDrive Helper")
        self.setWindowIcon(QIcon.fromTheme("drive-optical"))
        self.resize(900, 720)
        self.setMinimumSize(760, 640)

        self._helper = helper
        self._log_path = log_path
        self._palette = LIGHT
        self._snapshot: dict | None = None
        self._busy: tuple[str, str] | None = None   # headline and detail while a command runs
        self._notice = ""                           # error from the last command
        self._poll_error: str | None = None
        self._primary_action: str | None = None

        self._monitor = DriveMonitor(helper, POLL_INTERVAL_MS, self)
        self._monitor.snapshot_ready.connect(self.apply_snapshot)
        self._monitor.failed.connect(self._poll_failed)
        self._commands = CommandRunner(self)
        self._commands.finished.connect(self._command_finished)

        self._copy_reset = QTimer(self)
        self._copy_reset.setSingleShot(True)
        self._copy_reset.setInterval(2000)
        self._copy_reset.timeout.connect(lambda: self._copy.setText("Copy diagnostics"))

        self._build()

        refresh = QShortcut(QKeySequence(Qt.Key.Key_F5), self)
        refresh.activated.connect(self._monitor.poll)
        quit_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Quit), self)
        quit_shortcut.activated.connect(self.close)

        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(lambda _scheme: self.apply_theme())
        self.apply_theme()

    def start(self) -> None:
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_path.write_text(f"Apple SuperDrive Helper log, started {datetime.now():%Y-%m-%d %H:%M:%S}\n",
                                      encoding="utf-8")
        except OSError:
            pass  # The log file is a convenience for bug reports; the window works without it.
        self._log("Apple SuperDrive Helper started.")
        # Otherwise Qt focuses the first button, which then shows a focus ring nobody asked for.
        self._log_view.setFocus(Qt.FocusReason.OtherFocusReason)
        self._monitor.start()

    def closeEvent(self, event) -> None:
        self._monitor.stop()
        self._commands.stop()
        super().closeEvent(event)

    # --- Layout ---

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 24)
        root.setSpacing(16)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(3)
        titles.addWidget(self._label("Apple SuperDrive Helper", "Title"))
        titles.addWidget(self._label("Wakes the drive so it accepts discs, and shows what it is doing.", "Subtitle"))
        header.addLayout(titles, 1)
        self._pill = self._label("", "Pill")
        header.addWidget(self._pill, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(header)

        hero = self._card()
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(28, 26, 28, 20)
        hero_layout.setSpacing(32)
        self._art = DriveArt()
        hero_layout.addWidget(self._art, 0, Qt.AlignmentFlag.AlignVCenter)

        text = QVBoxLayout()
        text.setSpacing(0)
        self._badge = QFrame()
        self._badge.setObjectName("Badge")
        badge_layout = QHBoxLayout(self._badge)
        badge_layout.setContentsMargins(9, 3, 10, 3)
        badge_layout.setSpacing(6)
        self._spinner = Spinner()
        self._badge_text = QLabel()
        badge_layout.addWidget(self._spinner, 0, Qt.AlignmentFlag.AlignVCenter)
        badge_layout.addWidget(self._badge_text, 0, Qt.AlignmentFlag.AlignVCenter)
        badge_row = QHBoxLayout()
        badge_row.addWidget(self._badge)
        badge_row.addStretch(1)
        text.addLayout(badge_row)
        text.addSpacing(10)

        self._headline = self._label("", "Headline", wrap=True)
        self._detail = self._label("", "Detail", wrap=True)
        text.addWidget(self._headline)
        text.addSpacing(6)
        text.addWidget(self._detail)
        text.addSpacing(18)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self._primary = self._button("Wake drive", "accent", self._primary_clicked)
        self._wake = self._button("Wake", "subtle", self._start_wake)
        self._eject = self._button("Eject", "subtle", self._start_eject)
        self._refresh = self._button("Refresh", "subtle", self._monitor.poll)
        for button in (self._primary, self._wake, self._eject, self._refresh):
            buttons.addWidget(button)
        buttons.addStretch(1)
        text.addLayout(buttons)

        self._notice_label = self._label("", "Notice", wrap=True)
        self._notice_label.hide()
        text.addSpacing(8)
        text.addWidget(self._notice_label)
        hero_layout.addLayout(text, 1)
        root.addWidget(hero)

        details = QHBoxLayout()
        details.setSpacing(16)
        drive_card, self._drive_values = self._detail_card("Drive", ("Device", "Model", "Firmware", "Auto-wake"))
        disc_card, self._disc_values = self._detail_card("Disc", ("Disc", "Label", "File system", "Capacity"))
        details.addWidget(drive_card, 1)
        details.addWidget(disc_card, 1)
        root.addLayout(details)

        activity = self._card()
        activity_layout = QVBoxLayout(activity)
        activity_layout.setContentsMargins(20, 14, 20, 16)
        activity_layout.setSpacing(8)
        activity_header = QHBoxLayout()
        activity_header.addWidget(self._label("Activity", "CardTitle"))
        activity_header.addStretch(1)
        self._copy = self._button("Copy diagnostics", "link", self._copy_diagnostics)
        activity_header.addWidget(self._copy)
        activity_layout.addLayout(activity_header)
        self._log_view = QPlainTextEdit()
        self._log_view.setObjectName("Log")
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(500)
        activity_layout.addWidget(self._log_view, 1)
        root.addWidget(activity, 1)

    @staticmethod
    def _label(text: str, name: str, wrap: bool = False) -> QLabel:
        label = QLabel(text)
        label.setObjectName(name)
        label.setWordWrap(wrap)
        return label

    @staticmethod
    def _card() -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        return card

    @staticmethod
    def _button(text: str, kind: str, handler) -> QPushButton:
        button = QPushButton(text)
        button.setProperty("kind", kind)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        # Tab focus only, so the focus ring marks keyboard use rather than the last click.
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        button.clicked.connect(handler)
        return button

    def _detail_card(self, title: str, fields: tuple[str, ...]) -> tuple[QFrame, dict[str, QLabel]]:
        card = self._card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 12)
        layout.setSpacing(12)
        layout.addWidget(self._label(title, "CardTitle"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(9)
        grid.setColumnMinimumWidth(0, 104)
        grid.setColumnStretch(1, 1)
        values = {}
        for row, field in enumerate(fields):
            grid.addWidget(self._label(field, "FieldLabel"), row, 0)
            value = self._label(DASH, "FieldValue")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(value, row, 1)
            values[field] = value
        layout.addLayout(grid)
        return card, values

    # --- State ---

    def apply_theme(self) -> None:
        self._palette = DARK if prefers_dark() else LIGHT
        self.setStyleSheet(build_stylesheet(self._palette))
        self.update_view()

    def apply_snapshot(self, snapshot: dict) -> None:
        if self._poll_error is not None:
            self._log("Drive status is readable again.")
            self._poll_error = None
        self._log_changes(self._snapshot, snapshot)
        self._snapshot = snapshot
        self.update_view()

    def update_view(self) -> None:
        view = describe_state(self._snapshot, self._busy)
        tone = QColor(self._palette[view.tone])
        drive = first_drive(self._snapshot)
        media = drive["media"] if drive is not None else None

        self._art.set_state(tone, disc=bool(media and media["mounted_at"]))
        # Qt drops the rounding when the radius exceeds half the badge height, so keep it below.
        self._badge.setStyleSheet(
            f"QFrame#Badge {{ background: rgba({tone.red()}, {tone.green()}, {tone.blue()}, 41); border-radius: 8px; }}")
        self._badge_text.setStyleSheet(f"color: {tone.name()}; font-size: 12px; font-weight: 600;")
        self._badge_text.setText(view.badge)
        self._spinner.set_color(tone)
        self._spinner.setVisible(view.spinning)
        self._headline.setText(view.headline)
        self._detail.setText(view.detail)

        idle = self._busy is None
        self._primary_action = view.action
        self._primary.setText(view.action_text)
        self._primary.setVisible(view.action is not None)
        self._primary.setEnabled(idle)
        self._wake.setVisible(view.action != "wake")
        self._wake.setEnabled(idle and drive is not None)
        self._eject.setEnabled(idle and drive is not None)
        self._refresh.setEnabled(idle)
        self._notice_label.setText(self._notice)
        self._notice_label.setVisible(bool(self._notice))

        auto_wake = bool(self._snapshot and self._snapshot.get("auto_wake_installed"))
        self._pill.setText("Auto-wake on" if auto_wake else "Auto-wake not installed")
        self._pill.setVisible(self._snapshot is not None)

        values = self._drive_values
        values["Device"].setText(drive["device"] if drive else "Not connected")
        values["Model"].setText(f"{drive['vendor']} {drive['model']}" if drive else DASH)
        values["Firmware"].setText((drive["revision"] or "Unknown") if drive else DASH)
        values["Auto-wake"].setText("Installed" if auto_wake else "Not installed")

        values = self._disc_values
        if media is None or not media["present"]:
            values["Disc"].setText("None" if media is not None else DASH)
            for field in ("Label", "File system", "Capacity"):
                values[field].setText(DASH)
        else:
            values["Disc"].setText("Mounted" if media["mounted_at"] else "Inserted")
            values["Label"].setText(media["label"] or "(no label)")
            values["File system"].setText(media["fs_type"].upper() if media["fs_type"] else "Unknown")
            values["Capacity"].setText(format_bytes(media["size_bytes"]))

    def _log_changes(self, old: dict | None, new: dict) -> None:
        old_drive, new_drive = first_drive(old), first_drive(new)
        if old is None:
            self._log(f"Found the SuperDrive at {new_drive['device']}." if new_drive else "No SuperDrive connected.")
        elif drive_key(old_drive) != drive_key(new_drive):
            self._log(f"SuperDrive connected at {new_drive['device']}." if new_drive else "SuperDrive disconnected.")

        old_wake = old_drive["wake"] if old_drive else None
        new_wake = new_drive["wake"] if new_drive else None
        if new_wake and (old_wake is None or old_wake.get("time") != new_wake.get("time")):
            outcome = "accepted" if new_wake["ok"] else "failed"
            source = WAKE_SOURCES.get(new_wake.get("source"), new_wake.get("source"))
            self._log(f"Wake {outcome} ({source}, {format_record_time(new_wake['time'])}): {new_wake['detail']}")

        old_mount = old_drive["media"]["mounted_at"] if old_drive else None
        new_mount = new_drive["media"]["mounted_at"] if new_drive else None
        if new_mount and new_mount != old_mount:
            self._log(f"Disc mounted: {media_summary(new_drive['media'])}.")
        elif old_mount and not new_mount and new_drive:
            self._log("Disc unmounted.")

    def _poll_failed(self, message: str) -> None:
        if message != self._poll_error:
            self._log(f"Could not read the drive status: {message}")
            self._poll_error = message

    # --- Actions ---

    def _primary_clicked(self) -> None:
        if self._primary_action == "wake":
            self._start_wake()
        elif self._primary_action == "open":
            self._start_open()

    def _start_wake(self) -> None:
        drive = first_drive(self._snapshot)
        if drive is None or self._busy is not None:
            return

        wake_arguments = ["wake", "--device", drive["device"], "--source", "gui"]
        if os.geteuid() == 0:
            program, arguments = sys.executable, ["-I", str(self._helper), *wake_arguments]
        else:
            pkexec = shutil.which("pkexec")
            if pkexec is None:
                self._show_notice(f"pkexec is not installed, so this window cannot wake the drive. Run: sudo {self._helper} wake")
                return
            # The installed helper is executable and covered by the polkit policy. A checkout
            # may lack the executable bit, so run it through Python (polkit asks for a password).
            if os.access(self._helper, os.X_OK):
                program, arguments = pkexec, [str(self._helper), *wake_arguments]
            else:
                program, arguments = pkexec, ["/usr/bin/python3", "-I", str(self._helper), *wake_arguments]

        self._run("wake", program, arguments,
                  ("Waking the drive", f"Sending the wake command to {drive['device']}. You may be asked for your password."))

    def _start_eject(self) -> None:
        drive = first_drive(self._snapshot)
        if drive is None or self._busy is not None:
            return
        self._run("eject", sys.executable, ["-I", str(self._helper), "eject", "--device", drive["device"]],
                  ("Ejecting the disc", f"Unmounting and ejecting {drive['device']}."))

    def _start_open(self) -> None:
        drive = first_drive(self._snapshot)
        if drive is None or self._busy is not None:
            return
        self._run("open", sys.executable, ["-I", str(self._helper), "open", "--device", drive["device"]],
                  ("Opening the disc", "Mounting it if needed."))

    def _run(self, kind: str, program: str, arguments: list[str], busy: tuple[str, str]) -> None:
        self._busy = busy
        self._notice = ""
        self._monitor.invalidate()
        self.update_view()
        self._commands.run(kind, program, arguments)

    def _command_finished(self, kind: str, exit_code: int, output: str, errors: str) -> None:
        self._busy = None
        for line in output.splitlines():
            if line.strip():
                self._log(line.strip())

        if exit_code != 0:
            message = self._failure_message(kind, exit_code, errors)
            self._log(f"{COMMAND_NAMES[kind]} failed: {message}")
            # A rejected wake is already explained by the wake record in the status area.
            if not (kind == "wake" and exit_code == 1 and "did not wake" in errors):
                self._notice = message

        self._monitor.invalidate()
        self._monitor.poll()
        self.update_view()

    @staticmethod
    def _failure_message(kind: str, exit_code: int, errors: str) -> str:
        if kind == "wake" and exit_code == 126:
            return "Authorization was cancelled, so the drive was not woken."
        if kind == "wake" and exit_code == 127:
            return "Authorization failed, so the drive was not woken."
        lines = [line.strip() for line in errors.splitlines() if line.strip()]
        # The helper prints "error: <message>" and then, when it has one, "hint: <next step>".
        messages = [line.removeprefix("error: ") for line in lines if line.startswith("error: ")]
        hints = [line.removeprefix("hint: ") for line in lines if line.startswith("hint: ")]
        if messages:
            return f"{messages[-1]} {hints[-1]}" if hints else messages[-1]
        if lines:
            return lines[-1]
        if exit_code == -1:
            return f"The {kind} command stopped unexpectedly."
        return f"The {kind} command exited with code {exit_code}."

    def _show_notice(self, message: str) -> None:
        self._notice = message
        self._log(message)
        self.update_view()

    def _copy_diagnostics(self) -> None:
        QGuiApplication.clipboard().setText(self._diagnostics_text())
        self._copy.setText("Copied")
        self._copy_reset.start()
        self._log("Diagnostics copied to the clipboard.")

    def _diagnostics_text(self) -> str:
        lines = [
            "Apple SuperDrive Helper diagnostics (Linux)",
            f"Timestamp: {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z}",
            f"System: {platform.platform()}",
            f"Python: {platform.python_version()}, PySide6 {PYSIDE_VERSION}, Qt {qVersion()}",
            f"Helper: {self._helper}",
            "",
            "Status:",
            json.dumps(self._snapshot, indent=2) if self._snapshot is not None else "not read yet",
        ]
        log_lines = self._log_view.toPlainText().splitlines()
        if log_lines:
            lines += ["", "Recent log:", *log_lines[-25:]]
        return "\n".join(lines)

    def _log(self, message: str) -> None:
        line = f"{datetime.now():%H:%M:%S}  {message}"
        self._log_view.appendPlainText(line)
        try:
            with self._log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(line + "\n")
        except OSError:
            pass  # Same as in start(): the on-screen log still has the line.


def default_log_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state_home) / "apple-superdrive-helper" / "gui.log"


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Apple SuperDrive Helper")
    app.setDesktopFileName("apple-superdrive-helper")
    app.setStyle("Fusion")

    helper = Path(__file__).resolve().with_name(HELPER_NAME)
    if not helper.is_file():
        QMessageBox.critical(None, "Apple SuperDrive Helper", f"The helper {helper} is missing. Reinstall the SuperDrive helper.")
        return 1

    window = MainWindow(helper, default_log_path())
    window.show()
    window.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
