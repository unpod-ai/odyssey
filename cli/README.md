# odyssey-cli

The single `odyssey` console script. See [ADR 0003](../docs/adr/0003-single-cli-entrypoint.md).

## Why this package exists

`packages/odyssey-core` used to declare `[project.scripts] odyssey = ...`,
which collides the moment any second member also wants the name `odyssey` —
last-installed wins, silently. This package owns the name instead; every
member (core, `data_preparation`, and eventually `training`/`services/api`/
...) registers a command group as a plugin rather than its own binary.

`typer` and `rich` live here and only here — `odyssey-core` and
`odyssey-dataprep` stay dependency-free, which is what makes lazy loading
matter: a future `training` member depending on `torch` must not be
*imported* just to print `odyssey --help`.

## How plugin discovery works

A member declares one entry point in its own `pyproject.toml`:

```toml
[project.entry-points."odyssey.commands"]
spool = "odyssey.cli:register"
```

`registry.py`'s `LazyGroup`:

- `list_commands` reads entry-point *names* via `importlib.metadata` —
  package metadata only, no import. This is what `odyssey --help` calls.
- `get_command(name)` calls `entry_point.load()` — *this* is what imports
  the target module — only for the one group actually being dispatched.
  `odyssey spool push` imports `odyssey.cli`; it never imports
  `odyssey_dataprep`, and vice versa.

`register(app)` receives a fresh `typer.Typer()` sub-app and mounts plain
`@app.command()`-decorated functions on it. See `odyssey.cli.register`
(in `packages/odyssey-core`) for the reference implementation — every
command there is pure plumbing that builds an argv list and delegates to
`odyssey.cli.main`, the same argparse entrypoint `python -m odyssey.cli`
already used and already tested. `odyssey_dataprep.cli.register` is the
other real example — that member had no pre-existing CLI, so its command
calls `odyssey_dataprep.normalization`'s functions directly instead.

## Commands today

```
odyssey spool {push,export,sft,dpo,status,show,health}   # from odyssey-core
odyssey data normalize                                    # from odyssey-dataprep
odyssey doctor                                             # plugin discovery + cold-start timing
odyssey push / odyssey status                              # deprecated aliases, warn to stderr
odyssey --version
```

Nothing else in `docs/STRUCTURE.md`'s planned surface (`train`, `model`,
`eval`, `api`, `sdk`, `db`) is built — those wait on the members they'd
delegate to (`training`, `services/api`, ...), which don't exist yet. Adding
one later is adding an entry point in that member's own `pyproject.toml`;
this package does not change.

## Run it

```bash
cd cli
uv sync --extra dev
uv run odyssey --help
uv run odyssey spool status --spool /path/to/.odyssey
uv run odyssey doctor
```

## Not done here

- `--profile`/`--config` (`~/.odyssey/config.toml` + env + flags) — no
  command needs profile-scoped config yet; deferred rather than built
  speculatively.
- `--dry-run` / lineage-refusal on mutating commands — applies to
  `dataset publish`/`model promote`/`train run` per ADR 0003, none of which
  exist yet.
- Shell completions (`completions/bash`, `zsh`, `fish`).
- A global `--json` — only `spool health` supports it today (inherited from
  `odyssey.cli`); adding it elsewhere means adding it in the member that
  owns the output shape, not here.
