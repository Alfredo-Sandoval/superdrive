"""Small Qt styling helper for the SuperDrive utility."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QPalette

STYLE_PATH = Path(__file__).with_name("style.qss")


def _qcolor(red: int, green: int, blue: int) -> QColor:
    return QColor(red, green, blue)


def apply_style(app) -> None:
    app.setStyle("Fusion")
    palette = QPalette()

    palette.setColor(QPalette.ColorRole.Window, _qcolor(43, 43, 43))
    palette.setColor(QPalette.ColorRole.WindowText, _qcolor(224, 224, 224))
    palette.setColor(QPalette.ColorRole.Base, _qcolor(36, 36, 36))
    palette.setColor(QPalette.ColorRole.AlternateBase, _qcolor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ToolTipBase, _qcolor(224, 224, 224))
    palette.setColor(QPalette.ColorRole.ToolTipText, _qcolor(224, 224, 224))
    palette.setColor(QPalette.ColorRole.Text, _qcolor(224, 224, 224))
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Text,
        _qcolor(120, 120, 120),
    )
    palette.setColor(QPalette.ColorRole.Button, _qcolor(60, 60, 60))
    palette.setColor(QPalette.ColorRole.ButtonText, _qcolor(224, 224, 224))
    palette.setColor(QPalette.ColorRole.BrightText, _qcolor(210, 160, 80))
    palette.setColor(QPalette.ColorRole.Link, _qcolor(90, 130, 180))
    palette.setColor(QPalette.ColorRole.Highlight, _qcolor(90, 130, 180))
    palette.setColor(
        QPalette.ColorRole.HighlightedText,
        _qcolor(240, 240, 240),
    )
    app.setPalette(palette)
    app.setStyleSheet(STYLE_PATH.read_text(encoding="utf-8"))
