"""Agent-friendly CLI for SuperDrive Helper."""

from .app import app, main
from .core import (
    EXIT_CONFIG,
    EXIT_GENERIC,
    EXIT_NOT_FOUND,
    VERSION,
    CliError,
    build_controller,
)

__all__ = [
    "CliError",
    "EXIT_CONFIG",
    "EXIT_GENERIC",
    "EXIT_NOT_FOUND",
    "VERSION",
    "app",
    "build_controller",
    "main",
]
