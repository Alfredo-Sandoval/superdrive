"""Top-level Typer application wiring."""

from __future__ import annotations

from typing import Annotated

import typer

from .completion import completion_app
from .core import APP_EPILOG, VERSION, JsonErrorTyperGroup, emit_json
from .device import device_app
from .schema import schema_app

app = typer.Typer(
    add_completion=False,
    cls=JsonErrorTyperGroup,
    epilog=APP_EPILOG,
    help="SuperDrive Helper command interface.",
    no_args_is_help=True,
)
app.add_typer(completion_app, name="completion")
app.add_typer(device_app, name="device")
app.add_typer(schema_app, name="schema")


@app.callback(invoke_without_command=True)
def callback(
    version: Annotated[
        bool,
        typer.Option("--version", help="Print version and exit.", is_eager=True),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON only."),
    ] = False,
) -> None:
    if version:
        if json_output:
            emit_json({"kind": "version", "version": VERSION})
        else:
            typer.echo(VERSION)
        raise typer.Exit(0)


def main() -> None:
    app()
