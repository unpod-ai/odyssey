# ADR 0003 — One `odyssey` console script, plugin-dispatched

Status: accepted · Date: 2026-08-17

## Context

`packages/odyssey-core/pyproject.toml` declares:

```toml
[project.scripts]
odyssey = "odyssey.cli:main"
```

That name is the natural one for the product-wide CLI too — the surface the workspace needs covers spool
drain, data preparation, dataset registry, training, model registry, evaluation, API serving, SDK codegen
and migrations. Two installed wheels declaring the same console script means last-installed wins: a silent,
environment-dependent break with no error at install time.

There is a second constraint. `training/` depends on torch, trl, peft and soup-cli. A CLI that imports
every subcommand at startup would make `odyssey --help` pay for a torch import.

## Decision

The `cli/` member owns the `odyssey` console script. Core drops `[project.scripts]` and registers a plugin
instead:

```toml
[project.entry-points."odyssey.commands"]
spool = "odyssey.cli:register"
```

- `cli/src/odyssey_cli/registry.py` reads entry-point *metadata* at startup and imports a command module
  only when its subcommand is dispatched. `odyssey --help` never imports torch; a `doctor` check asserts
  cold `--help` stays under 200 ms.
- Core's existing `argparse` parser is untouched and stays usable as `python -m odyssey.cli`, so core keeps
  working standalone with zero dependencies. Typer and rich live in `cli/`, never in core.
- `odyssey push` / `odyssey status` survive one minor release as deprecated aliases of
  `odyssey spool push` / `odyssey spool status`, warning to stderr.
- The CLI holds no logic. A command parses arguments, calls a member's public API, and renders. A branch
  worth testing belongs in the member.
- Every command supports `--json`: humans get tables, CI gets stable output.
- Exit codes: `0` ok · `1` runtime failure · `2` usage error · `3` contract or lineage violation
  (train/eval leakage, missing manifest, OpenAPI drift). CI greps for `3` specifically.
- Mutating commands (`dataset publish`, `model promote`, `train run`) refuse to run until recipe hash and
  corpus version resolve. `--dry-run` prints the resolved plan and exits 0.

## Alternatives rejected

- **A binary per member** (`odyssey-train`, `odyssey-data`, …). No collision, but discovery is worse, shell
  completion fragments, and global flags like `--profile` get re-implemented per binary.
- **Core keeps `odyssey`, the umbrella takes another name.** Punishes the common case: the name users type
  would reach only the spool commands.
- **One package importing every member eagerly.** Simplest dispatch, but `--help` then imports torch, and
  `cli/` inherits the union of all dependencies.

## Consequences

- Installing `odyssey-core` alone no longer puts an `odyssey` binary on PATH. `python -m odyssey.cli` is the
  documented standalone path (`packages/odyssey-core/README.md`).
- The plugin contract is a public API: `register(app)` per member, versioned with the CLI.
- Each member must keep its command module import-light; a heavy module-level import re-introduces the
  startup cost the lazy registry exists to avoid.
