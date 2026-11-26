"""Machine-readable CLI contract description."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from .core import JsonErrorTyperGroup, emit_json

schema_app = typer.Typer(
    add_completion=False,
    cls=JsonErrorTyperGroup,
    help="Describe machine-readable command contracts.",
)


def _schema_payload() -> dict[str, Any]:
    return {
        "kind": "schema",
        "commands": {
            "completion bash": {"json": True, "mutates": False},
            "completion fish": {"json": True, "mutates": False},
            "completion zsh": {"json": True, "mutates": False},
            "device eject": {
                "mutates": True,
                "json": True,
                "requires": ["--yes or --dry-run"],
            },
            "device list": {"mutates": False, "json": True},
            "device load": {
                "mutates": True,
                "json": True,
                "requires": ["--yes or --dry-run"],
            },
            "device lock": {
                "mutates": True,
                "json": True,
                "requires": ["--yes or --dry-run"],
            },
            "device power": {
                "mutates": True,
                "json": True,
                "requires": ["--yes or --dry-run"],
            },
            "device probe": {"mutates": False, "json": True, "selects_device": True},
            "device status": {"mutates": False, "json": True, "selects_device": True},
            "schema describe": {"mutates": False, "json": True},
        },
        "exit_codes": {
            "success": 0,
            "generic_error": 1,
            "config_error": 2,
            "not_found": 3,
        },
        "error_shape": {
            "error": {
                "message": "string",
                "hint": "string",
                "details": "object optional",
            }
        },
    }


@schema_app.command("describe")
def describe(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON only."),
    ] = False,
) -> None:
    payload = _schema_payload()
    emit_json(payload)
