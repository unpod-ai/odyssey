"""odyssey — the single console script. See ADR 0003.

The root command's own surface is deliberately tiny: plugin discovery
(``registry.py``), ``--version``, a ``doctor`` command, and the two
deprecated top-level aliases (``push``/``status``) ADR 0003 promises for one
minor release. Every other command comes from a member's own
``odyssey.commands`` entry point, loaded lazily — see ``registry.py``.

Every command here — including the eager ones — is a plain ``typer``
``@app.command()``. A raw ``click.Command`` mixed in breaks nested
``--help`` for the same reason described in ``registry.py``'s docstring:
this typer version no longer shares one exception hierarchy with the
installed ``click`` package once you leave its own construction path.
"""

from __future__ import annotations

import subprocess
import sys
import time
from typing import Optional

import typer

from odyssey_cli import __version__
from odyssey_cli.registry import GROUP
from odyssey_cli.registry import LazyGroup as LazyGroup  # re-exported for tests
from odyssey_cli.registry import discover

app = typer.Typer(
    cls=LazyGroup,
    name="odyssey",
    help="odyssey — agent traces in, training corpora out.",
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"odyssey-cli {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """odyssey — agent traces in, training corpora out."""


def _deprecated(replacement: str) -> None:
    typer.echo(
        f"odyssey: this command is deprecated, use `{replacement}` instead",
        err=True,
    )


@app.command("push", help="[deprecated] alias for `odyssey spool push`")
def push_alias(
    out: str = typer.Option(..., help="output directory for JSONL"),
    journey: Optional[str] = typer.Option(None, help="drain only this journey_id"),
    spool: str = typer.Option(".odyssey", help="spool root"),
) -> None:
    _deprecated("odyssey spool push")
    from odyssey.cli import main as core_main

    argv = ["--spool", spool, "push", "--out", out]
    if journey:
        argv += ["--journey", journey]
    raise typer.Exit(code=core_main(argv))


@app.command("status", help="[deprecated] alias for `odyssey spool status`")
def status_alias(spool: str = typer.Option(".odyssey", help="spool root")) -> None:
    _deprecated("odyssey spool status")
    from odyssey.cli import main as core_main

    raise typer.Exit(code=core_main(["--spool", spool, "status"]))


@app.command("doctor", help="environment sanity: plugin discovery, cold-start speed")
def doctor() -> None:
    """Discovered command groups and a cold ``--help`` timing check.

    The 200ms budget is ADR 0003's own claim ("a doctor check asserts cold
    --help stays under 200ms"); this is that check, made real rather than
    left as a comment.
    """
    groups = discover()
    typer.echo(f"discovered command groups ({GROUP}): {sorted(groups) or '(none)'}")

    start = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-m", "odyssey_cli.main", "--help"],
        capture_output=True,
        text=True,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    budget_ok = elapsed_ms < 200
    status = "OK" if budget_ok else "SLOW"
    typer.echo(f"cold --help: {elapsed_ms:.0f}ms (budget: 200ms) {status}")

    if proc.returncode != 0 or not budget_ok:
        raise typer.Exit(code=1)


def run() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    run()
