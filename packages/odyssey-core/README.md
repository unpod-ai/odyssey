# odyssey-core

The library half of odyssey: the journey event schema, the fold that projects events into a journey,
the versioned JSONL codec, the local append-only spool, and the trace→training-example builders.

`dependencies = []`, and that is a constraint rather than a coincidence — the schema, builders, codec
and spool import only `json`, `typing`, `dataclasses` and `pathlib`. A dependency nothing imports is a
phantom dep; the change that needs one adds it.

## Modules

### The library

| Module | LOC | Responsibility |
|---|---|---|
| `primitives.py` | 434 | `JourneyEvent` and the schema it validates against (`SCHEMA_VERSION = "2.1"`) |
| `fold.py` | 349 | event fold + journey projection |
| `jsonl.py` | 468 | versioned JSONL codec: truncation handling, per-line rejection |
| `spool.py` | 891 | append-only local capture, per-journey watermark, `drain()`, `gc()` |
| `sinks.py` | 363 | `FileSink` and `HttpSink` — where a drain sends events |
| `hashing.py` | 43 | stable content hashing |
| `cli.py` | 455 | `push` · `export` · `sft` · `dpo` · `status` · `show` · `prune` · `health` |
| `export.py` / `sft.py` / `dpo.py` | 373 / 133 / 144 | the training artifacts: Trajectory JSON, SFT lines, DPO pairs |
| `pii.py` | 133 | content-level PII scan/redact (regex, not NER) |
| `builders/journey.py` | 213 | journey-level assembly |
| `builders/messages.py` | 782 | message adapters (Anthropic, LangSmith shapes) |
| `builders/steps.py` | 161 | cumulative steps |
| `builders/metrics.py` | 57 | metric extraction |
| `builders/reward.py` | 42 | reward attachment |

### The capture layer

`odyssey.init()` is the whole integration: `instrument` defaults to `"auto"`,
so every provider SDK the process actually has installed is patched in place
and LangChain's handler is registered process-wide. `ODYSSEY_INSTRUMENT` (or
`instrument="none"|"all"|["openai", ...]`) narrows it; `ODYSSEY_ENDPOINT`
selects the default sink. Every integration imports its third party lazily,
behind an optional extra, so `dependencies = []` still holds.

| Module | LOC | Responsibility |
|---|---|---|
| `client.py` / `config.py` | 691 / 194 | `init()`, the singleton, `ODYSSEY_*` resolution, `health()` |
| `context.py` | 259 | ambient journey `ContextVar`, `SeqAllocator`, header tag seeding (`project`) |
| `capture.py` | 608 | `journey()`, `@observe`, `_emit()` — the never-raise boundary |
| `diagnostics.py` | 292 | `scan()`, `render_journey()`, the `health`/`show` formatters |
| `metrics.py` / `project.py` | 150 / 82 | opt-in host telemetry; which repo a capturing process belongs to |
| `integrations/anthropic.py` + `_base.py` | 375 + 300 | drop-in sync/async client, opt-in patch, streaming |
| `integrations/openai.py` + `_openai_base.py` | 247 + 253 | same, OpenAI's shape — also every OpenAI-compatible gateway |
| `integrations/gemini.py` + `_gemini_base.py` | 239 + 307 | same, Gemini's shape (`contents`/`parts`, `role="model"`) |
| `integrations/langchain.py` | 435 | callback handler (LangGraph included) + process-wide `instrument()` |
| `integrations/otel.py` | 428 | `OdysseySpanProcessor`, one journey per trace — outside `"auto"` on purpose |
| `integrations/livekit.py` | 1 154 | `attach(session, ...)`: turn coalescing, tool pairing, agent handoffs, voice events |
| `integrations/pipecat.py` | 674 | `attach(task, ...)`: a `BaseObserver`, so it does not care which LLM service the pipeline runs |
| `integrations/_timing.py` / `_reentry.py` | 77 / 51 | shared `Timer`/`stamp()`; the guard that keeps one call to one recorded turn |

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
