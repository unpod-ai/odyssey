# odyssey-core

The library half of odyssey: the journey event schema, the fold that projects events into a journey,
the versioned JSONL codec, the local append-only spool, and the trace→training-example builders.

`dependencies = []`, and that is a constraint rather than a coincidence — the schema, builders, codec
and spool import only `json`, `typing`, `dataclasses` and `pathlib`. A dependency nothing imports is a
phantom dep; the change that needs one adds it.

## Modules

| Module | LOC | Responsibility |
|---|---|---|
| `primitives.py` | 367 | `JourneyEvent` and the schema it validates against |
| `fold.py` | 267 | event fold + journey projection |
| `jsonl.py` | 326 | versioned JSONL codec: truncation handling, per-line rejection |
| `spool.py` | 423 | append-only local capture, per-journey watermark, `drain()` |
| `hashing.py` | 43 | stable content hashing |
| `cli.py` | 96 | `push` (drain now) and `status` (per-journey spool state) |
| `builders/journey.py` | 213 | journey-level assembly |
| `builders/messages.py` | 665 | message adapters (Anthropic, LangSmith shapes) |
| `builders/steps.py` | 88 | cumulative steps |
| `builders/metrics.py` | 57 | metric extraction |
| `builders/reward.py` | 42 | reward attachment |

## Tests

`scripts/run_tests.sh` defines the module map — add new modules to its `case`, not just to `tests/`:

```bash
bash scripts/run_tests.sh list
bash scripts/run_tests.sh schema     # fold, projection, JourneyEvent validation
bash scripts/run_tests.sh build      # message adapters, metrics, reward, steps
bash scripts/run_tests.sh jsonl      # codec: truncation, per-line rejection
bash scripts/run_tests.sh spool      # capture, watermark, drain
bash scripts/run_tests.sh cli
bash scripts/run_tests.sh contract   # golden fixture + no-import-coupling gate
bash scripts/run_tests.sh all
```

Or via Taskfile: `task test`, `task check` (fmt + lint + pyrefly + tests).

## CLI

Three triggers share one `drain()`: `Spool.push()` (SDK), `IntervalDrainer` (time), and the command
line. This module adds no drain logic of its own.

```bash
python -m odyssey.cli --spool .odyssey status
python -m odyssey.cli --spool .odyssey push --out ./out [--journey <id>]
```

`push` exits non-zero when the drain reports failures, so a cron-driven drain is visible to its
supervisor. Gaps (missing sequence numbers) and errors go to stderr.

The console script named `odyssey` moves to the workspace-level `cli/` member, which will re-expose
these two commands as `odyssey spool push` / `odyssey spool status` — see
`docs/adr/0003-single-cli-entrypoint.md`. `python -m odyssey.cli` keeps working regardless.

## Python version

`>=3.12,<3.13`. The upper bound is load-bearing, not caution: soup (soup-cli, the trainer adapter this
project is built around) pins `>=3.10,<3.13` and enforces it with a test that parses its own CI matrix;
the super workspace is `>=3.12`. The intersection is exactly 3.12.
