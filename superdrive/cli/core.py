"""Shared CLI contract helpers."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import click
import typer

from superdrive.drive import DriveController

VERSION = "0.1.0"
EXIT_GENERIC = 1
EXIT_CONFIG = 2
EXIT_NOT_FOUND = 3

CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]+=*"),
    re.compile(r"(?i)\b(token|api[_-]?key|password|secret)=([^\s]+)"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b"),
)
APP_EPILOG = (
    "Commands: completion bash, completion fish, completion zsh, "
    "device eject, device list, device load, device lock, device power, "
    "device probe, device status, schema describe."
)


class CliError(RuntimeError):
    def __init__(
        self,
        message: str,
        hint: str,
        exit_code: int = EXIT_GENERIC,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.exit_code = exit_code
        self.details = dict(details) if details else None


def build_controller() -> DriveController:
    return DriveController()


def json_requested(raw_args: list[str] | tuple[str, ...] | None) -> bool:
    args = sys.argv[1:] if raw_args is None else list(raw_args)
    return any(arg == "--json" or arg.startswith("--json=") for arg in args)


def sanitize_text(value: object) -> str:
    text = CONTROL_CHARS.sub(" ", str(value))
    text = SECRET_PATTERNS[0].sub(r"\1[REDACTED]", text)
    text = SECRET_PATTERNS[1].sub(r"\1=[REDACTED]", text)
    return SECRET_PATTERNS[2].sub("[REDACTED]", text)


def sanitize_details(value: object) -> object:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, Path):
        return sanitize_text(value)
    if isinstance(value, Mapping):
        return {
            sanitize_text(key): sanitize_details(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [sanitize_details(item) for item in value]
    return sanitize_text(value)


def emit_json(payload: Mapping[str, Any]) -> None:
    typer.echo(json.dumps(payload, sort_keys=True))


def error_payload(error: CliError) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": {
            "message": error.message,
            "hint": error.hint,
        }
    }
    if error.details is not None:
        payload["error"]["details"] = sanitize_details(error.details)
    return payload


def write_error(error: CliError, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(error_payload(error), sort_keys=True), err=True)
    else:
        typer.echo(f"Error: {error.message}", err=True)
        typer.echo(f"Hint: {error.hint}", err=True)


def emit_error(error: CliError, *, json_output: bool) -> None:
    write_error(error, json_output=json_output)
    raise typer.Exit(error.exit_code)


def unexpected_runtime_error(exc: OSError | RuntimeError | ValueError) -> CliError:
    return CliError(
        "Operation failed.",
        "Review the error details, correct the device or environment, and retry.",
        EXIT_GENERIC,
        details={
            "exception": exc.__class__.__name__,
            "message": str(exc),
        },
    )


def click_exception_to_cli_error(error: click.ClickException) -> CliError:
    return CliError(
        sanitize_text(error.format_message()),
        "Run `superdrive-cli --help` or `superdrive-cli schema describe --json`.",
        error.exit_code,
    )


class JsonErrorTyperGroup(typer.core.TyperGroup):
    def main(
        self,
        args: list[str] | tuple[str, ...] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        try:
            result = super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        except click.ClickException as error:
            if json_requested(args):
                cli_error = click_exception_to_cli_error(error)
                write_error(cli_error, json_output=True)
                if standalone_mode:
                    sys.exit(cli_error.exit_code)
                raise
            if not standalone_mode:
                raise
            error.show()
            sys.exit(error.exit_code)

        if standalone_mode:
            sys.exit(result if isinstance(result, int) else 0)
        return result


def run_json_guard(
    fn: Callable[[], dict[str, Any]],
    *,
    json_output: bool,
    emit_human: Callable[[dict[str, Any]], None],
) -> None:
    try:
        payload = fn()
    except CliError as error:
        emit_error(error, json_output=json_output)
    except (OSError, RuntimeError, ValueError) as exc:
        emit_error(unexpected_runtime_error(exc), json_output=json_output)
    if json_output:
        emit_json(payload)
    else:
        emit_human(payload)
