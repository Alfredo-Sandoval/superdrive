"""Shell completion commands."""

from __future__ import annotations

from typing import Annotated, Any, Literal

import click
import typer
from click.shell_completion import BashComplete, FishComplete, ZshComplete

from .core import JsonErrorTyperGroup, run_json_guard

completion_app = typer.Typer(
    add_completion=False,
    cls=JsonErrorTyperGroup,
    help="Print shell completion scripts.",
)

ShellName = Literal["bash", "fish", "zsh"]


def _completion_script(shell: ShellName) -> str:
    from .app import app

    command = typer.main.get_command(app)
    complete_var = "_SUPERDRIVE_CLI_COMPLETE"
    classes: dict[ShellName, type[click.shell_completion.ShellComplete]] = {
        "bash": BashComplete,
        "fish": FishComplete,
        "zsh": ZshComplete,
    }
    return classes[shell](command, {}, "superdrive-cli", complete_var).source()


def _completion_payload(shell: ShellName) -> dict[str, Any]:
    return {
        "kind": "completion",
        "shell": shell,
        "program": "superdrive-cli",
        "source": _completion_script(shell),
    }


def _emit_human(payload: dict[str, Any]) -> None:
    typer.echo(payload["source"], nl=False)


def _emit_completion(shell: ShellName, *, json_output: bool) -> None:
    def body() -> dict[str, Any]:
        return _completion_payload(shell)

    run_json_guard(body, json_output=json_output, emit_human=_emit_human)


@completion_app.command("bash")
def completion_bash(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON only."),
    ] = False,
) -> None:
    _emit_completion("bash", json_output=json_output)


@completion_app.command("fish")
def completion_fish(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON only."),
    ] = False,
) -> None:
    _emit_completion("fish", json_output=json_output)


@completion_app.command("zsh")
def completion_zsh(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON only."),
    ] = False,
) -> None:
    _emit_completion("zsh", json_output=json_output)
