"""Lazy plugin discovery — the ADR 0003 contract.

Each workspace member that wants a command group declares one entry point in
its own ``pyproject.toml``::

    [project.entry-points."odyssey.commands"]
    spool = "odyssey.cli:register"

``discover()`` reads that metadata via ``importlib.metadata`` — package
metadata only, no import of the target module. ``LazyGroup.get_command``
only calls ``entry_point.load()`` (which *does* import the target module)
the moment that specific command group is actually dispatched. That is the
whole mechanism behind "``odyssey --help`` must not import torch": a future
``training`` member's entry point is never loaded just to print the top-level
help text, only when someone actually runs ``odyssey train ...``.

``register(app)`` receives a fresh ``typer.Typer()`` sub-app and mounts that
member's commands onto it with plain ``@app.command()``/``typer.Option(...)``
— see ``odyssey.cli.register`` for the reference implementation.

Two things learned the hard way while building this against typer 0.27:

- Subclass ``typer.core.TyperGroup`` (typer's own public group class), not
  raw ``click.Group``. This typer version no longer builds directly on the
  installed ``click`` package for its own command/context machinery, so a
  plain ``click.Group`` root mixed with ``typer.main.get_command()``-built
  subcommands breaks nested ``--help`` — the two no longer share one
  exception hierarchy for ``ctx.exit()``.
- A command returned from ``get_command`` has no name of its own — typer
  only sets it when going through ``add_typer(name=...)``, which this
  bypasses. Without setting ``.name`` explicitly, the group renders in
  ``--help`` with a blank/missing label even though dispatch still works.
"""

from __future__ import annotations

import importlib.metadata as metadata
from typing import Dict, List, Optional

import click
import typer
import typer.core

GROUP = "odyssey.commands"


def discover() -> Dict[str, metadata.EntryPoint]:
    """Every declared command group, keyed by name. No member is imported."""
    return {ep.name: ep for ep in metadata.entry_points(group=GROUP)}


class LazyGroup(typer.core.TyperGroup):
    """Subcommands are discovered from entry-point metadata and built only
    when invoked — never for a cold ``odyssey --help``."""

    def list_commands(self, ctx: click.Context) -> List[str]:
        return sorted(set(discover()) | set(super().list_commands(ctx)))

    def get_command(self, ctx: click.Context, name: str) -> Optional[click.Command]:
        ep = discover().get(name)
        if ep is None:
            # Not a plugin group — an eagerly-added command (doctor, the
            # deprecated push/status aliases) or genuinely unknown.
            return super().get_command(ctx, name)

        try:
            register_fn = ep.load()
        except Exception as exc:  # noqa: BLE001 - one broken plugin must not
            # take the rest of the CLI down with it.
            click.echo(
                f"odyssey: failed to load command group {name!r}: {exc}", err=True
            )
            return None

        sub_app = typer.Typer(help=f"{name} commands")
        try:
            register_fn(sub_app)
        except Exception as exc:  # noqa: BLE001 - see above
            click.echo(
                f"odyssey: failed to register command group {name!r}: {exc}", err=True
            )
            return None

        command = typer.main.get_command(sub_app)
        command.name = name
        return command
