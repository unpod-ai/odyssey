# odyssey — working state & build checklist

What we are building, what is actually built, and what is left — verified against
the code, not against the plan.

Companion docs: [`STRUCTURE.md`](STRUCTURE.md) is the *planned* tree; ADRs 0001–0003
are the decisions. This file is the *implemented* truth plus the step-wise
checklist.

Every claim here was checked by running the code. The commands that prove each
claim are in [§9 Verification](#9-verification--prove-every-claim-yourself).

**Last verified:** 2026-08-25 · 468 passed, 1 skipped · flake8 clean at 100 cols

---

## 1. What we are building

**A Langfuse-shaped SDK for agent calls, whose dump is a training corpus.**

The product requirement, stated plainly:

> Install it in **one place**. Every call log lands automatically. Everything
> collects into **one destination**. That destination feeds model training.

Two halves to that, and they pull in different directions:

- **Collection must be invisible.** One `init()`, one drop-in client or one
  decorator. No instrumentation sprinkled across the codebase, no per-call-site
  bookkeeping. If a developer has to remember to log, the corpus has holes.
- **The dump must be training-grade.** Not just observability rows. Cumulative
  turns, tool-call correlation preserved, per-turn `trainable` labels, and
  preference pairs (chosen vs rejected) that DPO can actually read.

Langfuse solves the first half and stops. odyssey has to do both, which is why
the schema work came first.

### The ten layers

A Langfuse-style SDK is these layers. Ticks are verified, not aspirational.

| # | Layer | What it means | Langfuse equivalent | odyssey |
|---|---|---|---|---|
| L1 | **Event schema / wire format** | the one unit written and shipped | trace + observation model | ✅ **done, tested** |
| L2 | **Local buffer + delivery** | survive process death, retry, dedupe | in-memory queue + background flush | ✅ **done** (disk-backed — stronger than Langfuse) |
| L3 | **Projection / read side** | events → usable conversation | server-side trace assembly | ✅ **done** (`fold`) |
| L4 | **Ambient context** | auto `journey_id` + auto `seq`, no threading params | `contextvar` trace/span stack | ✅ **done** (`context.py`) |
| L5 | **Auto-instrumentation** | drop-in clients, decorator, framework callbacks | `@observe`, `openai` shim, LangChain handler, OTel | 🟡 **Anthropic + LiveKit done**; OpenAI / LangChain / OTel open |
| L6 | **One-line init** | `odyssey.init()` + env vars + `atexit` flush | `langfuse.init()` | ✅ **done** (`client.py`) |
| L7 | **HTTP transport** | ship to a server, not a folder | `/api/public/ingestion` | ❌ **0%** (only `FileSink`) |
| L8 | **Collector / server** | the "one place" everything lands | Langfuse server | ❌ **0%** |
| L9 | **Dashboard** | look at what landed | Langfuse UI | ❌ **0%** |
| L10 | **Training export** | corpus → SFT/DPO files | (Langfuse: dataset export) | 🟡 **Trajectory JSON ships** (`odyssey export`); SFT/DPO writers open |

**6 of 10 layers built.** L4 and L6 — the "install it in one place" half — landed
in the capture-layer change. What remains between "an agent runs" and "a model
trains" is now exactly two things: **L7/L8, a destination that is not a local
folder**, and **L10, something that writes an SFT or DPO file**.

### What the API looks like now

```python
# once, at process start — the ONE place
import odyssey
odyssey.init()                 # reads ODYSSEY_* from env; starts the drainer;
                               # registers an atexit flush

# scope a journey; nothing inside names a journey_id or a seq
with odyssey.journey(id=platform_call_id, user_id="u_42") as j:
    ...
    j.signal("thumbs_up")      # target defaults to the turn just recorded
    j.reward(0.9)

# automatic provider capture — an import swap, nothing else
from odyssey.integrations.anthropic import Anthropic
client = Anthropic()           # every messages.create() is recorded

# your own tool functions
@odyssey.observe(as_tool=True)
def book(day: str, time: str) -> dict:
    ...
```

Opt-in patching, for call sites that cannot be edited:

```python
odyssey.init(instrument=["anthropic"])   # existing anthropic clients now record
```

The explicit drop-in is the default because a patched call stack is harder to
read in a traceback and harder to reason about when two libraries patch the same
method. Patching is the escape hatch.

### The constraint that shaped it: single writer per journey

`seq` is allocated per process, seeded from whatever is already on disk. Two
processes recording one journey would both seed from the same maximum and issue
the same numbers — a journey that reads as valid while interleaving two
conversations. The fold deduplicates on `event_id`, not `seq`, so it would not
catch that on its own.

So every event carries a `writer_id` in `JourneyEvent.metadata`
(`WRITER_META_KEY`) — **not a new schema field**, which is what keeps
`SCHEMA_VERSION` at `1.0`. `fold()` reports `writers`, exposes
`writer_conflict`, and sets `complete=False` when there is more than one. The
CLI exits `3` on it.

Single-writer-per-journey is therefore a *contract with detection*, not an
assumption. If one call ever spans two pods, the honest fix is per-writer
sequences, which is a `SCHEMA_VERSION` major bump — the upgrade path is open and
deliberately not taken yet.

---

## 2. Where we are — scorecard

### Verified, just now

```
pytest tests -q                     → 468 passed, 1 skipped   (2.3 s)
pytest --collect-only -q            → 469 collected across 17 files
flake8 --max-line-length=100        → exit 0, clean
scripts/make_golden.py --check      → golden fixture is current   ← wire format intact
python -m odyssey.cli --help        → push · export · status · show · health
python -m odyssey.cli export --help → --events · --out · --journey · --last-step
```

The one skip is `test_superdialog_does_not_depend_on_odyssey` — the sibling
checkout is not on this machine, not a failure.

Two notes on the lint number, because "clean" needs a column width to mean
anything. At 100 columns the tree is clean. At flake8's own default of 79 it is
not, and the offenders are almost all one file: `builders/messages.py` carries 27
long lines of provider-shape literals, where wrapping a payload example makes it
harder to compare against the SDK's own docs. There is no `.flake8` /`setup.cfg`
in the repo yet, so the width lives in the command rather than in config — item
9.2, the CI file that would pin it, is still unwritten.

### Size

| | Before the capture layer | Now |
|---|---|---|
| Source | 11 files, 2 587 LOC | **24 files, 6 806 LOC** |
| Tests | 12 files, 3 025 LOC | **18 files, 7 640 LOC** |
| Tests passing | 197 | **468** (+1 skipped) |
| Third-party deps in core | 0 | **0** — still, verified by import scan |
| `src/odyssey/__init__.py` | 0 bytes | **131 LOC** (public API, 50 exports) |

Tests now outweigh source, 7 640 lines to 6 806. That ratio is the honest cost of
a layer whose failure mode is silence: a capture bug does not crash the host, it
produces a corpus that is quietly wrong, so almost every rule in the fold and the
export has a test that would notice.

> **`uv.lock` grew from 19 to 35 packages, and that is not a regression.**
> Declaring the `anthropic` optional extra makes uv lock its whole transitive
> tree — `pydantic`, `httpx`, `anyio` and friends now appear in the lockfile.
> None of it is installed: `uv sync --extra dev` still produces an environment
> with no `anthropic` and no `pydantic`, and all 468 tests pass in it, because
> `test_integrations.py` injects a fake SDK through `sys.modules`. Only
> `uv sync --extra anthropic` pulls the real one. `dependencies = []` is
> unchanged.

### Test report — 469 cases, and what each file is holding down

One line per file, newest first in intent rather than alphabet. The count is what
`pytest --collect-only` reports, so parametrised cases are counted the way they
actually run.

| File | Cases | What it exists to catch |
|---|---|---|
| `test_livekit.py` | 83 | Streamed utterances coalescing into one turn; tool calls paired with their outputs; the system prompt read off the live agent and followed through a handoff; a `close` that arrives after the worker already tore the session down |
| `test_sdk.py` | 56 | `init()`/`journey()`/`observe()` end to end, the never-raise boundary, `health()` counters, the `atexit` drain |
| `test_spool.py` | 51 | Append-only durability (a real SIGKILLed child), shard rotation, LRU handle eviction, watermarks, resumed drains, redaction |
| `test_export.py` | 38 | The artifact: Trajectory shape, cumulative steps, `--last-step` trimming, filename sanitisation, atomic write, one unreadable shard not aborting a run |
| `builders/test_build_messages.py` | 33 | Provider payload parsing — OpenAI, Anthropic, Vercel, flat — including unparseable tool arguments |
| `test_fold.py` | 31 | Dedupe, terminal cut, gap detection, writer conflict, `trainable_status` precedence |
| `test_jsonl.py` | 27 | The codec: version header, truncated last line, per-line rejection, append without a second header |
| `builders/test_build_helpers.py` | 25 | The small pure functions the builders lean on |
| `test_integrations.py` | 23 | The Anthropic drop-in and opt-in patching, against a fake SDK injected through `sys.modules` |
| `test_contract.py` | 21 | The cross-project golden fixture — every event kind, tool correlation, preference chain, and the rule that `Step` never reaches the wire |
| `test_context.py` | 18 | `ContextVar` propagation into tasks but *not* threads, and 8×200 concurrent `seq` allocation with no holes |
| `test_cli.py` | 18 | `push` · `export` · `status` · `show` · `health`, including the exit-3 writer-conflict contract |
| `builders/test_build_metrics_and_steps.py` | 18 | Tool counts, error rate, one cumulative step per turn |
| `builders/test_build_journey.py` | 17 | Journey assembly, content hash, idempotency key |
| `builders/test_langsmith_roundtrip.py` | 4 | A LangSmith-shaped trace surviving the round trip |
| `builders/test_build_reward.py` | 3 | Scalar → `Reward` |
| `builders/test_anthropic_e2e.py` | 3 | A real-shaped Anthropic exchange end to end |

Ten cases worth naming, because each one is a bug that shipped or nearly did:

- `test_no_step_record_is_ever_encoded` — the wire/artifact split. A step holds
  the whole conversation up to its point, so N steps on the wire cost O(N²) bytes
  where N events cost O(N).
- `test_the_last_step_alone_still_carries_every_message` — the other half of that
  split. `--last-step` writes one step and it is the complete conversation.
- `test_trimming_the_steps_keeps_the_tool_call_and_its_result` — trimming must
  not drop the tool turn, which is the only evidence a booking failed.
- `test_a_trimmed_export_says_so` — a consumer counting `steps` has to be able to
  tell a one-turn call from a trimmed twelve-turn one.
- `test_the_system_prompt_can_be_kept_out_of_the_journey` — a prompt of business
  rules can dwarf the call; `record_instructions=False` keeps it out without
  touching anything else.
- `test_show_marks_the_trainable_turn` — `trainable_status` is derived at read
  time. Reading the stored value reported every turn as `not_trainable`.
- `test_a_regeneration_supersedes_the_earlier_answer` — the preference pair a DPO
  extractor will eventually read.
- `test_record_is_fast_enough_for_a_capture_hot_path` — p50 under 90 µs, the
  guard on the 196 µs → 23 µs work below.
- `test_a_writer_conflict_is_named_in_the_artifact` — two writers on one journey
  is the corruption that reads as valid data.
- `test_golden_fixture_is_not_stale` — the wire format cannot drift without a
  deliberate regeneration.

### Performance

`record()` is on the application's hot path, so the old implementation mattered:
it re-derived the filesystem on every event — `mkdir`, two `resolve()`s, a
directory `glob` and three `stat`s — all inside the global lock.

| | Before | After |
|---|---|---|
| `record()` p50 | 196 µs | **23 µs** |
| Throughput, single thread | 5 100 events/s | **39 800 events/s** |
| Lock held per event | ~184 µs | **~2 µs** |

The lock number is the one that matters for concurrency: journeys serialise on
one lock, so shrinking the critical section 90× is what makes many concurrent
calls viable. A regression guard pins p50 under 90 µs
(`test_record_is_fast_enough_for_a_capture_hot_path`).

### Against the repo's own phase plan

- [x] **0** extract `odyssey/` → `packages/odyssey-core`, history preserved
- [x] **1** workspace root, gitignore contract, version pins, docs, ADRs — *plus `__init__.py`, now closed*
- [x] **0′** *(inserted)* capture layer: ambient context, `init()`, `@observe`, Anthropic capture, diagnostics
- [ ] **2** `cli/` — root app, plugin registry, `spool` group (ADR 0003)
- [ ] **3** `odyssey-schemas` + `services/api` + `openapi.json` + `sdk/python`
- [ ] **4** `data_preparation` stages + `datasets/` registry
- [ ] **5** `training` (soup adapter) + `models/registry.yaml` + `evaluation`
- [ ] **6** `apps/web` + `sdk/javascript` + `sdk/examples`

The original phase list had no phase for L4–L7 — it predates the
one-integration-point requirement. Step 0 below is that work; the parts of it
that are built are now ticked, and the parts that are not (L7) are the top of the
critical path.

---

## 3. Step-wise checklist (verified against code)

The lineage chain everyone has been passing around:

```
raw traces (immutable)
  → data_preparation   collection → cleaning → normalization → annotation
                       → augmentation → validation → splitting
  → corpus             version = sha(recipe_hash + curated_watermark)
  → training           config sha + corpus version → checkpoint
  → models             registry entry: sha256 + base model + corpus version
  → evaluation         frozen eval set → report
  → services/api → sdk → apps/web
```

That is the **project structure** — the shape of the pipeline. It is not a
checklist, and it silently assumes "raw traces" already exist. Getting traces to
arrive by themselves *is the SDK*, and it is Step 0 below.

Legend: ✅ done & tested · 🟡 partly there · ❌ not started

---

### Step 0 — Collection: the single integration point (L4–L6)

**Done.** This is what the capture-layer change delivered.

| # | Item | Status | Where / what remains |
|---|---|---|---|
| 0.1 | Event schema to record into | ✅ | `primitives.py` — validated in `__post_init__` |
| 0.2 | Durable local sink | ✅ | `spool.py` — O(1), thread-safe, no network. The drain destination is separate and injectable: `init(sink=...)` takes anything with `send(journey_id, events, header)` |
| 0.3 | Secret redaction at record time | ✅ | `spool.redact_event()` — `api_key` masked before disk |
| 0.4 | **Ambient journey context** | ✅ | `context.py` — `ContextVar`; asyncio-native, `bind()` for thread handoff |
| 0.5 | **Automatic `seq` allocation** | ✅ | `context.SeqAllocator` — seeded from disk, so a restart resumes instead of colliding. 8-thread test proves no number is issued twice |
| 0.6 | **`odyssey.init()`** | ✅ | `client.py` + `config.py`. Second call warns and reuses; `force=True` replaces |
| 0.7 | **`@observe` decorator** | ✅ | `capture.observe()` — sync and async |
| 0.8 | **`with odyssey.journey(...)`** | ✅ | Nesting joins the parent; an exception closes with `ERROR` and re-raises |
| 0.9 | **Drop-in provider client** | 🟡 | Anthropic sync + async + patch done. **OpenAI, Gemini not written** |
| 0.10 | **Framework hooks** (LangChain/LangGraph, LlamaIndex) | ❌ | Not started |
| 0.11 | **OTel bridge** | ❌ | Not started |
| 0.12 | **Flush on exit** | ✅ | `atexit` by default. SIGTERM is **opt-in** (`init(handle_sigterm=True)`) — `atexit` does not run on SIGTERM, and hijacking a signal from a library is rude |
| 0.13 | Background drain thread | ✅ | `IntervalDrainer`, now started by `init()` |
| 0.14 | Voice-agent turn capture | 🟡 | `integrations/livekit.py` — one `attach()` per `AgentSession`: streamed utterances coalesced into one turn, tool calls paired with outputs, the system prompt read off the live agent and followed through handoffs (or skipped with `record_instructions=False`), and `.tool()` for engines LiveKit does not drive. Voice-*specific* events (STT, TTS, barge-in, per-turn latency) still have no schema home — see Step 0′ |
| 0.15 | **Never crash the host** | ✅ | Capture failures counted, not raised. `ODYSSEY_DEBUG=1` re-raises for local dev |
| 0.16 | **Diagnostics** | ✅ | `diagnostics.py`, `odyssey.health()`, `odyssey.cli health` |

**Design decisions worth knowing before extending this:**

- **`@observe()` records no event by default.** It opens or joins a journey and
  nothing else. `@observe(as_tool=True)` records a tool turn. A corpus is not a
  span log — an arbitrary internal call is noise that every recipe would then
  have to filter out. Langfuse records everything because it is an observability
  product; this is the one place the two designs genuinely diverge.
- **Events outside a journey are dropped and counted.** Auto-creating one would
  mint a single-event journey with no terminal, which is never `complete`, which
  is untrainable noise. The integration wrappers open their own journey, so a
  standalone LLM call is still captured.
- **Providers resend the whole conversation.** `integrations/_base.py` tracks how
  much of the message list it has recorded and emits only the tail. The system
  prompt and the tool definitions are recorded once, then again only when they
  change. Without this, turn 3 would re-record turns 1 and 2 with fresh
  `event_id`s — duplicates the fold cannot detect.
- **Unknown provider block types do not lose the turn.**
  `messages_from_anthropic_messages` refuses unknown blocks by design, which is
  right for a batch import a human is watching and wrong on an auto-capture path.
  So `_base.split_blocks` separates them first: `thinking` becomes
  `Message.reasoning`, anything else is named in `metadata["unknown_blocks"]`.

### Step 0′ — Collection, still open

| # | Item | Status | Note |
|---|---|---|---|
| 0′.1 | OpenAI drop-in + patch | ❌ | `messages_from_openai_chat` already parses the format; the wrapper is what is missing |
| 0′.2 | LangChain / LangGraph callback handler | ❌ | |
| 0′.3 | OTel bridge | ❌ | Would make every OTel-instrumented app record for free |
| 0′.4 | **Voice events** | ❌ | STT input, TTS output, barge-in, per-turn latency do not fit `Message`, which is LLM-turn shaped. Needs a decision: new `EventKind` (schema major bump) or `metadata` convention |
| 0′.5 | Streaming coverage | 🟡 | Sync `messages.stream()` wrapped; the async streaming path is not |
| 0′.6 | Sampling | ❌ | No way to record 10% of traffic. Matters once volume is real |

---

### Step 1 — Transport & the one destination (L7–L8)

**"Sab logs ek jagah aa jaayein" — the *jagah* still does not exist.** With only
`FileSink`, the "one place" is a local folder per machine, which is not one
place. **This is the top of the critical path.**

| # | Item | Status | Evidence / what's missing |
|---|---|---|---|
| 1.1 | `Sink` protocol | ✅ | `spool.Sink` — `runtime_checkable`, one method, raise-to-fail |
| 1.2 | Drain with at-least-once + watermark | ✅ | Verified: 8 pushed, second push 0 |
| 1.3 | Retry semantics | ✅ | Failure leaves shard *and* watermark untouched; the shard **is** the retry queue |
| 1.4 | `FileSink` | ✅ | `sinks.py` (moved out of `cli.py` so the library never imports the CLI) |
| 1.5 | **`HttpSink`** | ❌ | No HTTP client in `src/` — verified by grep. **The next thing to build** |
| 1.6 | **Auth** (API key, project scoping) | ❌ | `init()` has no `api_key` argument yet |
| 1.7 | **Batching / compression / backpressure on the wire** | ❌ | |
| 1.8 | **Ingest endpoint** (`services/collector`) | ❌ | Still a single `.gitkeep` |
| 1.9 | **Server-side idempotency** | 🟡 | Keys exist and are now populated on every event: `event_id`, `WRITER_META_KEY`, `hashing.idempotency_key()`. No server consumes them |
| 1.10 | **Object-store landing** (raw layer) | ❌ | ADR 0002 defines the contract; no code |
| 1.11 | `TelemetryEvent` → API | 🟡 | Still **dead code**: 2 grep hits, both its own definition, zero call sites. Targets a `POST /api/v1/telemetry/events` and a `push_events()` that do not exist |
| 1.12 | **Retention / TTL** | ❌ | Nothing prunes a drained spool. `Spool.close()` releases handles; it does not delete shards |

`HttpSink` needs no new abstraction — it is one class with one `send()` method,
and `Spool.push()` / `IntervalDrainer` / the CLI all drive it unchanged. See
[§11 Extension points](#11-extension-points).

---

### Step 2 — Raw traces, immutable

| # | Item | Status | Evidence |
|---|---|---|---|
| 2.1 | Append-only local layout | ✅ | `<root>/journeys/<jid>/NNN.jsonl` + `watermarks.json` |
| 2.2 | Shard rotation | ✅ | Survives the handle cache — `test_rotation_still_happens_with_a_cached_handle` |
| 2.3 | Versioned wire format | ✅ | `SCHEMA_VERSION = "1.0"`, unknown MAJOR refuses to parse |
| 2.4 | Truncated-writer tolerance | ✅ | Killed mid-append → every complete event returned |
| 2.5 | Per-line rejection | ✅ | One bad line → one `Rejection`, file still readable |
| 2.6 | Path-traversal / symlink containment | ✅ | `safe_child()` → `SpoolPathError`, on the cold path where it belongs |
| 2.7 | Fold: dedupe · sort · terminal cut · gap detect | ✅ | 31 tests |
| 2.8 | `trainable_status` labelling | ✅ | 5-rule precedence |
| 2.9 | Cumulative step projection | ✅ | Never stored, computed at read time |
| 2.10 | Content hash + idempotency key | ✅ | Canonical JSON, SHA-256 |
| 2.11 | Cross-project contract test | ✅ | Golden fixture (12 events) + parsed-import gate |
| 2.12 | **Writer-conflict detection** | ✅ | `FoldResult.writers` / `.writer_conflict` / `.incomplete_reason`; CLI exits 3 |
| 2.13 | **Bounded open file descriptors** | ✅ | `max_open_shards` (default 256), LRU eviction, closed on terminal |
| 2.14 | Retention / TTL on the raw layer | ❌ | Disk grows forever — same gap as 1.12 |
| 2.15 | **Content-level PII scrub** | 🟡 | Key-based masking works. `PiiPolicy` / `RedactionPreview` are still types with no implementation — email/phone/card **in prose** is not scrubbed |

This step is the strongest part of the repo. 2.14–2.15 are real but not blocking.

---

### Step 3 — `data_preparation` (7 stages)

`data_preparation/src/odyssey_dataprep/` — **nine `.gitkeep` files, zero code.**
No `pyproject.toml`, so it is not a workspace member yet.

| # | Stage | Status | What it needs |
|---|---|---|---|
| 3.1 | `collection/` | ❌ | Pull from spool / collector / object store → raw layer |
| 3.2 | `cleaning/` | ❌ | Dedupe (keys exist), dead-turn drop, encoding repair, PII scrub (needs 2.15) |
| 3.3 | `normalization/` | 🟡 | **The engine exists** — `fold()` + `builders/messages.py` already do schema coercion and role canon. Needs a stage wrapper, not new logic |
| 3.4 | `annotation/` | 🟡 | `Signal`, `Reward`, `build_reward_from_scalar()` exist and are now *populated by the SDK*. Human-in-loop queue adapters do not exist |
| 3.5 | `augmentation/` | ❌ | Paraphrase, synthetic negatives, tool-call perturbation |
| 3.6 | `validation/` | ❌ | Schema assert, leakage check, drift, PII assert. **Must exit 3 on breach** (ADR 0003) |
| 3.7 | `splitting/` | ❌ | **By session/group key, never by row.** A test must enforce this |
| 3.8 | `flows/` | ❌ | Prefect orchestration |
| 3.9 | `recipes/*.yaml` | ❌ | Declarative + hashed — `recipe_hash` is half the corpus version |

Cheapest real win in the whole repo: **3.3 is mostly a wrapper over code that is
already tested.**

---

### Step 4 — Corpus: `version = sha(recipe_hash + curated_watermark)`

| # | Item | Status | Note |
|---|---|---|---|
| 4.1 | Stable canonical hashing | ✅ | `hashing.content_hash()` |
| 4.2 | Per-journey delivery watermark | ✅ | `Spool.watermark()` |
| 4.3 | **`curated_watermark`** | ❌ | A different concept from the delivery watermark. Still undefined anywhere |
| 4.4 | **`recipe_hash`** | ❌ | Needs 3.9 |
| 4.5 | **Corpus version function** | ❌ | The `sha(recipe_hash + curated_watermark)` composition |
| 4.6 | `datasets/registry.yaml` | ❌ | `.gitkeep` only |
| 4.7 | `datasets/manifests/<name>/v1.json` | ❌ | shards + sha256 + row counts + recipe hash |
| 4.8 | `datasets/cards/` | ❌ | provenance, license, PII posture, splits, intended use |

⚠️ **Still unresolved: "curated_watermark" is used in the chain and in
`README.md` but never defined.** Highest annotated `seq`? A timestamp cut-off? A
count of human-approved journeys? Three different implementations. Needs a
decision before 4.5 — ideally in the missing
`openspec/changes/add-journey-schema/design.md`.

---

### Step 5 — Training

`training/` — nine `.gitkeep`s, no `pyproject.toml`.

| # | Item | Status | Note |
|---|---|---|---|
| 5.1 | `trainable` gate on export | ✅ | `FoldResult.trainable`, and now `.incomplete_reason` says why not |
| 5.2 | Per-turn `trainable_status` | ✅ | Only assistant turns carry gradient by default |
| 5.3 | Preference chain (chosen / rejected) | ✅ | `Signal` with `regen_order` + `edited_output`, **now emitted by the SDK** — `test_a_regeneration_supersedes_the_earlier_answer` |
| 5.4a | **Trajectory JSON export writer** | ✅ | `export.py` + `odyssey export` — one `{conversation_id}.json` per conversation, the shape `tj.save()` produces and the platform consumes. `--last-step` / `last_step_only=True` writes the final step alone: every step is a prefix of the next, so all N cost O(N**2) bytes and the last one already holds the whole conversation |
| 5.4b | **SFT export writer** | ❌ | Nothing converts a `Journey` into a messages-only SFT file yet |
| 5.5 | **DPO/KTO/ORPO pair extractor** | ❌ | The signals are there and populated; the extractor is not |
| 5.6 | **soup-cli adapter** | ❌ | The reason for the `<3.13` Python pin |
| 5.7 | `configs/{sft,dpo,grpo}` | ❌ | `.gitkeep` only |
| 5.8 | `experiments/<exp_id>.yaml` | ❌ | config sha + corpus version + metrics ref |
| 5.9 | Checkpoint → object store | ❌ | ADR 0002 contract, no code |

**L10 lives here, and it is now the second-biggest gap.** The schema was designed
for DPO from day one — that is why `Signal` carries an *ordering* rather than
just a scalar — and the SDK now produces those signals end to end. Nothing yet
turns them into a training file.

---

### Step 6 — Models registry

| # | Item | Status |
|---|---|---|
| 6.1 | `models/registry.yaml` | ❌ |
| 6.2 | `models/cards/<model>-v1.md` | ❌ |
| 6.3 | Weights stay out of git | ✅ (`.gitignore` + `.gitkeep`, ADR 0002) |
| 6.4 | Promote / export commands | ❌ |

`model_id` is tracked **per event** and the Anthropic wrapper now populates it
from the provider response, so `fold()` sets a journey-level `model_id` only when
the journey never switched models. Provenance is correct at the source; the
registry that consumes it does not exist.

---

### Step 7 — Evaluation

| # | Item | Status |
|---|---|---|
| 7.1 | `evaluation/src/odyssey_eval/` harness | ❌ `.gitkeep` |
| 7.2 | Frozen eval sets, never trained on | ❌ |
| 7.3 | Benchmarks + metric code | ❌ |
| 7.4 | `dataset-audit.yml` no-overlap gate | ❌ |
| 7.5 | Reports | ❌ dirs exist, empty |

Journey-level metrics **are** computed today — `steps`, `num_tool_calls`,
`num_tool_failures`, `tool_error_rate`, `num_tool_response_none`,
`aggregated_reward`, `total_time` — and the Anthropic wrapper now feeds real
`usage` into them. A head start on 7.3.

---

### Step 8 — Serving: api → sdk → web

| # | Item | Status |
|---|---|---|
| 8.1 | `packages/odyssey-schemas` (pydantic DTOs) | ❌ |
| 8.2 | `services/api` (FastAPI) | ❌ |
| 8.3 | `services/api/openapi.json` | ❌ |
| 8.4 | `sdk/python` (generated) | ❌ |
| 8.5 | `sdk/javascript` (`@odyssey/sdk`) | ❌ |
| 8.6 | `apps/web` (Next.js dashboard) | ❌ |
| 8.7 | `scripts/codegen.sh` + CI drift gate | ❌ |

Note the overlap: **8.2 and 1.8 are the same server.** Plan the ingest endpoint
and the read API together or the "one place" ends up being two places.

⚠️ **Naming collision to settle before 8.4:** `STRUCTURE.md` reserves the
distribution name `odyssey-sdk` / package `odyssey_sdk` for the *generated
OpenAPI client*. The capture layer that people will call "the SDK" now lives in
`odyssey-core`. Two different things, one obvious name.

---

### Step 9 — Repo hygiene

| # | Item | Status | Cost |
|---|---|---|---|
| 9.1 | `src/odyssey/__init__.py` public API | ✅ **done** — 131 LOC, 50 exports | — |
| 9.2 | CI (`.github/workflows/ci-core.yml`) | ❌ still `.gitkeep` | small — 468 tests pass, nothing locks it in |
| 9.3 | `cli/` single entrypoint (Phase 2, ADR 0003) | ❌ | medium. Now also needs to expose `health` |
| 9.4 | `NOTICE` copyright holder | ❌ | **blocks public release** — see [§10](#10-known-gaps) |
| 9.5 | Stale `src/odyssey/build/` path in `NOTICE` + `pyproject` | ❌ | trivial |
| 9.6 | `openspec/.../design.md` (cited by code, absent) | ❌ | small — needed for 4.3 |
| 9.7 | `.pre-commit-config.yaml`, `CHANGELOG.md`, `SECURITY.md`, `CODEOWNERS` | ❌ | trivial |
| 9.8 | Two no-op contract tests | 🟡 | trivial — see [§10](#10-known-gaps) |
| 9.9 | **ADR for the capture layer** | ❌ | The design in §1 has no ADR. It also documents a deliberate exception to the `packages/ = no side effects` rule, which is exactly what an ADR is for |
| 9.10 | 77 pyrefly errors in `tests/` | ❌ | **Not re-verified in the 2026-08-25 pass** — `pyrefly check` now stops at *“No `pyrefly.toml` found”* and asks for `pyrefly init`, so the count below is the last one actually measured. Latent: `task types` uses pyrefly's auto-config, which checks `src` + `scripts` only. Adding any `[tool.pyrefly]` key switches to explicit config and surfaces them |

---

### Recommended order

The dependency graph, not the wish list. Step 0 is done, so:

```
9.2 CI  (half a day — 468 tests pass, lock them in before anything else lands)
   ↓
1.5 HttpSink  +  1.6 auth        ← "one place" starts being real
   ↓
1.8 services/collector           ← the destination itself
   ↓
5.4 + 5.5 SFT writer + DPO pair extractor   ← the payoff: an actual training file
   ↓
3.3 normalization stage (thin wrapper over fold)   ← cheapest pipeline win
   ↓
0′.1 OpenAI wrapper · 9.3 cli/ · 9.9 ADR
```

Everything in Steps 4, 6, 7, 8 can still wait. The shortest path from "an agent
runs" to "a model trains" is now: **a real sink, a real destination.** The export
writer landed — `export.py`, item 5.4a.

---

## 4. Mental model: event-sourced, projection at read time

Three decisions explain almost every line of core.

**1. `JourneyEvent` is the only unit written to disk or sent over a network.**
Append-only, ordered by `seq` within a `journey_id`, idempotent on `event_id`.

**2. Cumulative state is never stored or transmitted.** A `Step` holds the whole
conversation up to that point. Shipping N cumulative steps costs O(N²) bytes;
shipping N events and folding them costs O(N). So `Step[]` is *computed* by
`fold()` at read time and never encoded — `test_no_step_record_is_ever_encoded`
enforces that `Step` is not part of the wire vocabulary at all.

**3. Recording never touches the network.** The agent appends an event and
returns. A separate drain ships batches on an interval, on a CLI command, or on
an explicit `push()`. Inference stays off the remote-latency path, and recording
works with no server reachable.

Consequence of (3): **the local shard is the retry queue.** No backoff scheduler,
no in-memory buffer to lose. A shard stays on disk until the sink acknowledges
it; only then does the watermark advance. Stricter than Langfuse's in-memory
queue — a crash loses nothing.

### Write path vs read path

```
WRITE (hot, local, ~23us)
  journey() → ContextVar          ← L4: journey_id and seq come from here
    ↓
  _emit() → JourneyEvent          ← the library builds it; no call site names a seq
    ↓
  redact → append to <root>/journeys/<jid>/NNN.jsonl   (cached handle)
    ↑ writer_id stamped into metadata, so a two-writer conflict is provable

DRAIN (out of band, at-least-once)
  spool.undrained(jid) → sink.send(..., header) → on success: watermark = max(seq)
    ↑ three triggers, one code path: Spool.push() · IntervalDrainer · CLI push
    ↑ TODAY: only FileSink exists                    ← the L7 gap, item 1.5

READ (cold, wherever the events landed)
  read_events(*.jsonl) → fold() → FoldResult{ Journey, complete, gaps, writers }
                                    ↑ dedupe · sort · terminal cut · gap detect
                                    ↑ writer-conflict detection
                                    ↑ trainable_status labelling
                                    ↑ build_cumulative_steps() ← Step[] born here

EXPORT (the artifact, not the transport)
  export.save() → <out>/<conversation_id>.json    ← CLI: odyssey export
    ↑ Trajectory shape: task · steps · reward · metrics · execution_metrics
    ↑ this is where cumulative state is ALLOWED to exist — it never hits the wire
    ↑ incomplete journeys are written and flagged under `_odyssey`, not dropped
    ↑ --last-step drops the N-1 prefix steps; `_odyssey.steps_written = "last"`
```

---

## 5. Module reference

24 files, 6 806 LOC, **zero third-party dependencies**. `stdlib` only — verified
by scanning every import. That is a constraint, not an accident: a dependency
nothing imports is a phantom dep, and the change that needs one adds it.

### The capture layer

| Module | LOC | Responsibility |
|---|---|---|
| `__init__.py` | 131 | Public API. 50 exports; the one place a user imports from |
| `context.py` | 228 | `ContextVar` journey stack, `SeqAllocator`, `bind()`. **No I/O at all** |
| `config.py` | 123 | `ODYSSEY_*` env → `Config`. Explicit args win; a bad env value falls back rather than failing startup |
| `client.py` | 427 | The singleton: spool, allocator, drainer, `atexit`, opt-in SIGTERM, counters, `health()`. `init(sink=...)` accepts any destination |
| `capture.py` | 534 | `journey()`, `JourneyHandle`, `observe()`, `_emit()`. The never-raise boundary |
| `diagnostics.py` | 295 | `scan()` a spool, `render_journey()` for `show`, formatters |
| `integrations/_base.py` | 283 | Request+response → events: prefix dedup, unknown-block handling |
| `integrations/anthropic.py` | 249 | Drop-in sync/async client, opt-in patch. Provider imported **inside** `__init__` |
| `integrations/livekit.py` | 939 | One `attach()` per `AgentSession`. Coalesces streamed utterances into one message per turn, pairs tool calls with their outputs, reads the system prompt off the live agent — or skips it entirely under `record_instructions=False`. `.tool()` records a call LiveKit never ran, which is how flow- and playbook-driven deployments get tool turns at all |

**`context.py`** — the piece that made everything else possible.
`SeqAllocator.next()` holds its lock across the seed call deliberately: releasing
it would let two threads seed the same journey and hand out the same number. The
cost is one directory read on a journey's first event, never again. An 8-thread ×
200-event test asserts 1 600 unique, hole-free numbers.

`ContextVar` propagates into `asyncio` tasks automatically but **not** into a new
`threading.Thread` or `run_in_executor` — those start from defaults. That is why
`bind()` is public, and both behaviours are pinned by tests.

**`client.py`** — `init()` twice warns and returns the existing client, because a
second one would start a second drainer. `flush_on_exit` registers an `atexit`
hook bound to *that* client, so a `force`-replaced client is still flushed.
SIGTERM is opt-in and *chains* to the previous handler rather than replacing it.

**`capture.py`** — `_jsonable()` coerces anything into something `json.dumps`
accepts, depth-capped at 12, falling back to `repr()`. Auto-capture sees whatever
the application returns; one non-serializable object would otherwise lose the
event.

`journey()` nesting joins the parent, so a decorated helper called mid-journey
cannot split the conversation. Only the outermost block emits the terminal.

**`integrations/_base.py`** — if the caller's message list *shrinks*, the
recorded offset is meaningless. It **resyncs without re-recording**: a duplicated
turn is silent corruption (the fold dedupes on `event_id`, not content), while a
skipped one is merely a hole. The discrepancy is counted and shows in `health()`.

### Outside `src/`

| Path | LOC | What it is |
|---|---|---|
| `examples/booking_agent.py` | 120 | A runnable agent loop with odyssey in it three times. Faked LLM, so no API key and no network. The fastest way to see what the layer does — run it, then `show` it |
| `scripts/manual_check.sh` | — | 13 hands-on checks, evidence rather than assertions. See [§9](#9-verification--prove-every-claim-yourself) |
| `scripts/run_tests.sh` | — | The test module map. New module → extend its `case` |
| `scripts/make_golden.py` | 191 | Regenerates the cross-project golden fixture, deterministically |
| `scripts/reformat_equivalence.py` | 221 | Proves a `black`/`isort` run did not change behaviour |

### The core (unchanged in behaviour)

| Module | LOC | Responsibility |
|---|---|---|
| `primitives.py` | 412 | `JourneyEvent` and the vocabulary it validates against |
| `spool.py` | 626 | Append-only capture, cached shard handles, watermark, `drain()` |
| `jsonl.py` | 416 | Versioned codec: truncation handling, per-line rejection, header written once per file |
| `fold.py` | 341 | Event fold + projection + writer-conflict detection |
| `builders/messages.py` | 665 | Provider *parsers* (OpenAI, Anthropic, Vercel, flat) |
| `builders/journey.py` | 213 | Journey assembly, metrics, content hash |
| `builders/steps.py` | 161 | One cumulative step per turn, copy-on-write system prefix |
| `builders/metrics.py` | 57 | Tool counts, error rate, elapsed time |
| `builders/reward.py` | 42 | Scalar → `Reward` |
| `hashing.py` | 43 | Canonical JSON → SHA-256 |
| `cli.py` | 202 | `push` · `export` · `status` · `show` · `health` |
| `sinks.py` | 50 | `FileSink` — moved out of `cli.py` so the library never imports the CLI. Any object with `send(journey_id, events, header)` is a sink, so a deployment that wants no wire copy at all passes one that keeps nothing |
| `export.py` | 353 | The artifact: `Journey` → `{conversation_id}.json`, `--last-step` trimming, `_odyssey` diagnostics, atomic write, filename sanitisation |

> ⚠️ `builders/messages.py` **parses payloads you already captured**. It does not
> intercept calls. Auto-capture is `integrations/`. Two different jobs — the
> parser is what `integrations/_base.py` calls, not a substitute for it.

**`primitives.py`** — validation raises `ValueError` on a negative `seq`, an
unknown `kind`, a missing payload, or a payload belonging to another kind. A
`kind="message"` event carrying a `reward=` is a hard error, not a silently
ignored field.

`model_id` is per-event on purpose: one journey spans model switches, retries and
routing fallbacks, so journey-level attribution would silently mix models under
one label.

**Declared but unused:** `TelemetryEvent`, `PiiPolicy`, `RedactionPreview`,
`ConversationSummary`. Items 1.11 and 2.15.

**`spool.py`** — `record()` keeps a per-journey `_ShardState` (path, open handle,
tracked size) instead of re-deriving the filesystem per event. `flush()` still
runs per event, so the killed-process guarantee is unchanged — verified by a test
that SIGKILLs a real child. Handles are capped by `max_open_shards` with LRU
eviction and closed when a journey terminates.

Redaction deliberately never touches `message.content` — that is the training
data, and blanket-redacting prose would quietly destroy the corpus. Credentials
end up in the structured corners: `metadata`, tool `arguments`, tool `response`.
Empty values pass through unmasked, so a `[REDACTED]` marker always means a real
value existed.

**`fold.py`** — six phases: dedupe on `event_id` → terminal cut (lowest `seq`
wins) → gap detection → payload partition (last reward wins) → `trainable_status`
labelling → projection. `complete` requires no gaps, a terminal, **and at most
one writer**.

`derive_trainable_status` precedence, highest first: `summarization_boundary` →
`interrupted` → `superseded` → `thumbs_down` → `thumbs_up` → role default
(assistant trainable, everything else not). The last rule is the point: only the
model's own outputs carry gradient. The two structural flags outrank the human
signals because they describe what the turn *is*, not how good it was — a
barged-in half-utterance is not a valid target even if someone approved it.

---

## 6. End-to-end example (verified)

Real output, from the code as it stands. Note what the caller does **not** do:
name a `seq`, name a target for the signal, or call `flush()`.

```python
import odyssey
from odyssey.primitives import Message, ToolCall, ToolResponse

odyssey.init(spool_dir="./.odyssey", out_dir="./out", drain_interval=None)

with odyssey.journey(id="call_8891", user_id="u_42") as j:
    j.message(Message(role="system", content="You book appointments."))
    j.message(Message(role="user", content="Book me for Tuesday at 3."))
    j.message(Message(role="assistant", tool_calls=[
        ToolCall(id="tc_1", name="book",
                 arguments={"day": "tue", "api_key": "sk-SECRET"})]))
    j.message(Message(role="tool", tool_response=ToolResponse(
        id="tc_1", name="book", arguments={}, response={"ok": True})))
    j.message(Message(role="assistant", content="Booked for 3pm."),
              model_id="claude-opus-5")
    j.signal("thumbs_up")      # no target_seq — resolves to the turn above
    j.reward(0.9)
# terminal emitted here; atexit drains at process end
```

Observed:

```
events         8
seqs           [0, 1, 2, 3, 4, 5, 6, 7]     ← allocated, never typed
kinds          [message ×5, signal, reward, terminal]
metadata       {'_odyssey_writer': 'dc3540c9e570', 'user_id': 'u_42'}
redacted       {'api_key': '[REDACTED]', 'day': 'tue'}
signal target  [4]                          ← resolved from context

complete       True
trainable      True
writers        ['dc3540c9e570']
reason         None
steps          1
statuses       ['trainable']
agg reward     0.9
```

Multi-turn provider capture, three calls each resending the whole conversation:

```
seq kind     role      dir      detail
0   message  system    request  You book appointments.
1   message  user      request  Book me an appointment.
2   message  assistant response Sure, which day?
3   message  user      request  Tuesday at 3.
4   message  assistant response tool_call book{'day':'tue'}  [reasoning: ...]
5   message  tool      request  tool_resp 'ok'
6   message  assistant response Booked for 3pm.
7   signal                      thumbs_up target_seq=6
8   terminal                    ENV_DONE

system count    1     ← sent 3 times, recorded once
tool_defs on    [0]   ← sent 3 times, recorded once
capture errors  0
trainable       True
```

Five things worth noticing:

- **No `seq` anywhere in the calling code.** That is the whole layer.
- **`api_key` was redacted before it hit disk.** `day` survived untouched.
- **8 events → 1 step.** Steps were never recorded; the fold produced them. One
  is right: the caller asked once, so the whole call-a-tool-then-answer sequence
  is a single turn.
- **The system prompt and tool schema were each recorded once** despite being
  resent on every call.
- **`flush()` was never called.** `atexit` drained it.

Remove the terminal and `complete` flips to `False` with
`incomplete_reason == "no terminal event: journey may still be running"`.

### The three shapes, with real bytes

odyssey writes two formats and they are not competing. Everything below is real
output from `tests/fixtures/golden_journey.jsonl`, the fixture both projects
check against.

**1 — the wire.** Append-only JSONL, one header line then one line per event.
Cumulative state is deliberately absent: a step holds the whole conversation up
to its point, so shipping N steps costs O(N²) bytes where shipping N events costs
O(N). `test_no_step_record_is_ever_encoded` enforces it.

```json
{"data_source":"golden","journey_id":"j_golden_0001","journey_metadata":{"channel":"voice","tenant":"acme"},"odyssey_schema_version":"1.1","started_at":"2026-01-01T09:00:00+00:00","trace_id":"trace_golden_0001"}
{"event_id":"golden-e00","journey_id":"j_golden_0001","kind":"message","message":{"content":"You book appointments.","role":"system"},"seq":0,"ts":"2026-01-01T09:00:00+00:00"}
{"event_id":"golden-e01","journey_id":"j_golden_0001","kind":"message","message":{"content":"Book me for Tuesday at 3.","role":"user"},"seq":1,"ts":"2026-01-01T09:00:01+00:00"}
```

The header carries identity the events do not repeat — `data_source`, `trace_id`,
`journey_metadata`, the schema version — and it is written once per file, so a
resumed drain appending the tail does not produce a second one.

**2 — the artifact.** One `{conversation_id}.json` per conversation. This is the
shape `tj.save()` produces and the platform consumes, and it is where cumulative
state is *allowed* to exist because it never hits the wire.

```json
{
  "task": {
    "conversation_id": "j_golden_0001",
    "data_source": "golden",
    "id": "golden:j_golden_0001",
    "num_steps": 3,
    "num_turns": 3
  },
  "steps": [ { "messages": [ ... ], "trainable_status": "trainable" } ],
  "metrics": {
    "aggregated_reward": 0.92,
    "num_tool_calls": 1,
    "num_tool_failures": 0,
    "num_tool_response_none": 0,
    "steps": 3,
    "tool_error_rate": 0.0
  },
  "execution_metrics": { "termination_reason": "ENV_DONE" },
  "_odyssey": {
    "complete": true,
    "terminated": true,
    "journey_id": "j_golden_0001",
    "model_ids": ["openai/gpt-4.1-mini"],
    "schema_version": "1.1"
  }
}
```

A tool turn keeps its correlation across both shapes — the call and its result
share an `id`, which is what makes the pair readable without guessing:

```json
{ "role": "assistant",
  "finish_reason": "tool_calls",
  "trainable_status": "trainable",
  "usage": {"prompt_tokens": 42, "completion_tokens": 18},
  "tool_calls": [{"id": "call_slot_1", "name": "check_slot",
                  "arguments": {"day": "tuesday", "hour": 15}}] }

{ "role": "tool",
  "trainable_status": "not_trainable",
  "tool_response": {"id": "call_slot_1", "name": "check_slot",
                    "arguments": {"day": "tuesday", "hour": 15},
                    "response": {"available": true}} }
```

Everything odyssey knows that the platform's schema has no field for lives under
one reserved key, `_odyssey`. `complete` is written even when it is `True`: a flag
that appears only on failure is a flag consumers forget to check.

**3 — the artifact, trimmed.** `--last-step` / `last_step_only=True` writes the
final step alone.

```
save([fold_shard(golden)], out)                      → 6 041 bytes, 3 steps
save([fold_shard(golden)], out, last_step_only=True) → 3 230 bytes, 1 step, 7 messages
```

The saving is small on a three-turn fixture and large on a real call, because the
waste is quadratic. A recorded twelve-turn phone call went **54 522 → 9 426
bytes**, and the surviving step still holds all 24 messages, greeting to goodbye.
The trimmed file says so:

```json
"_odyssey": { "complete": true, "terminated": true, "steps_written": "last", ... },
"task":     { "num_steps": 12, "num_turns": 12 }
```

`task.num_turns` still reports the conversation, not the file. Nothing about the
call is lost — only the eleven prefixes of the step that was kept.

### What a real voice call looks like

From a LiveKit deployment recording a phone booking, with the entry checkpoint's
own HTTP tools captured through `LiveKitRecorder.tool()`:

```
seq  kind      role       detail
0    message   assistant  tool_calls [action-auth-token]
1    message   tool       tool_response action-auth-token → {access_token: [REDACTED]}
2    message   assistant  tool_calls [action-players-search]
3    message   tool       tool_response action-players-search → {player_id: player_32ff87, ...}
...
10   message   assistant  "Good afternoon, Sanyam! Welcome back to GolfAI TeeTime..."
11   message   user       "Book there again."
...
33   terminal             ENV_DONE
```

Two things that read as odd until you know the shape. The tool block sits *before*
the greeting because those tools are the checkpoint's `on_enter` work — auth,
profile lookup, booking history — which runs before the agent speaks at all; each
HTTP request is one `assistant` message carrying `tool_calls` plus one `tool`
message carrying the response, the same pairing OpenAI and Anthropic use. And a
tool call with no preceding speech stands alone, whereas "let me check
availability" followed by a lookup is folded into *one* message with both
`content` and `tool_calls`, because that was one generation.

---

## 7. Commands

### Repo root

```bash
task                 # list every available task
task setup           # uv sync --all-packages --extra dev
task check           # fmt + lint + types + tests, every member
task test            # tests only
```

The root `Taskfile.yml` is a pure delegator — a task appears there only once the
member it routes to exists. Today every route points at `core:`.

### `packages/odyssey-core`

```bash
cd packages/odyssey-core

task sync            # uv sync --extra dev
task fmt             # isort + black
task lint            # flake8 --max-line-length=88 --extend-ignore=E203,E501,W503,F541,F841
task types           # pyrefly check     ← the house checker, NOT mypy
task check           # fmt + lint + types + test
task test            # scripts/run_tests.sh all
task test-modules    # list test modules
```

Test module map — add a new module to the `case` in `scripts/run_tests.sh`, not
just to `tests/`:

```bash
bash scripts/run_tests.sh list          # print the map
bash scripts/run_tests.sh schema        # fold, projection, JourneyEvent validation
bash scripts/run_tests.sh build         # message adapters, metrics, reward, steps
bash scripts/run_tests.sh jsonl         # codec: truncation, per-line rejection
bash scripts/run_tests.sh spool         # capture, watermark, drain, handle cache
bash scripts/run_tests.sh context       # ambient context and seq allocation
bash scripts/run_tests.sh sdk           # init/journey/observe/health; never raises
bash scripts/run_tests.sh integrations  # provider capture, dedup, patching
bash scripts/run_tests.sh cli           # drain trigger and health report
bash scripts/run_tests.sh contract      # golden fixture + no-coupling gate
bash scripts/run_tests.sh all
```

Anything after the module name forwards to pytest:
`bash scripts/run_tests.sh spool -k watermark -x`.

### The runnable example — start here

```bash
mkdir -p /tmp/demorun && cd /tmp/demorun
python ~/odyssey/packages/odyssey-core/examples/booking_agent.py
python -m odyssey.cli --spool ./.odyssey show call_7781
```

`examples/booking_agent.py` is an ordinary agent loop with a faked LLM, so it
needs no API key and no network. odyssey appears in it three times: the import,
`init()`, and one `with odyssey.journey(...)` block. Nothing else in the file
names a `journey_id`, a `seq`, or a `flush()`.

Read the file next to the `show` output below — that pairing is the fastest way
to understand what the layer actually does.

### The CLI

```bash
python -m odyssey.cli --spool .odyssey status
python -m odyssey.cli --spool .odyssey push   --out ./out [--journey <id>]
python -m odyssey.cli --spool .odyssey export --out ./artifacts [--journey <id>] [--events ./out] [--last-step]
python -m odyssey.cli --spool .odyssey show [<journey_id>]
python -m odyssey.cli --spool .odyssey health [--journey <id>] [--json]
```

`push` and `export` are the two halves of the same pipeline and they fail
differently, which is why they are separate commands: a drain that cannot reach
its sink is retried, an export that cannot fold is a data problem. `export` reads
the **spool** by default rather than requiring a `push` first — reading moves no
watermark, so a later drain still ships every event. `--events` points it at a
directory of already-drained `*.jsonl` instead.

`--last-step` writes the final step alone; see [§6](#6-end-to-end-example-verified)
for what that costs and what it keeps.

`show` answers the question `health` cannot: not "is it recording?" but "show me
what you recorded, and which of it a model would learn from."

```
journey call_7781
  14 events · 4 steps · model claude-opus-5 · TRAINABLE

    0 · system    You are a booking assistant.
    1 > user      Hi, I need an appointment.
    2 < assistant Sure — what day works for you?              ★ trainable
    3 > user      Tuesday afternoon please.
    4 < assistant Tuesday works. What time?                   ★ trainable
    5 > user      Yes, 3pm.
    6 < assistant call book({'day': 'tue', 'time': '15:00'})  ★ trainable
    7 = tool      result {'confirmed': True, 'ref': 'BK-4417'}
    8 < assistant Booked for Tuesday 3pm.                     [superseded]
    9 ! signal    regenerated → seq 8
   10 < assistant Done — Tuesday 3pm, ref BK-4417.            ★ trainable
   11 ! signal    thumbs_up → seq 10
   12 $ reward    0.9 (identity)
   13 . terminal  ENV_DONE

training view
  SFT candidates : 3
  superseded     : 1 turn(s) at seq [8] — rejected side of a preference pair
  reward         : 0.9 · tool calls 1 · failures 0
  NOTE: no exporter writes these to an SFT/DPO file yet (items 5.4b / 5.5).
```

The four steps fall on the three user turns plus the regenerated answer: seq 8
ends a step of its own because an answer that was regenerated away is an
alternative at one decision point, not the next thing the agent said.

Three things that view makes concrete: `★` marks the turns that carry gradient
(assistant outputs only — a user turn or a tool result is context, not a target);
seq 8 against seq 10 is a `(rejected, chosen)` pair a DPO exporter can read; and
the closing NOTE is honest about *which* exporter is still missing — `odyssey
export` writes the Trajectory artifact (item 5.4a), but nothing yet writes the
messages-only SFT file or extracts the preference pairs (5.4b, 5.5).

`trainable_status` is **derived at read time**, not read off disk — the recorded
field holds whatever the producer set, and the real label depends on signals that
arrive later. Reading the stored value instead of recomputing it reports every
turn as `not_trainable`; that bug existed in the first cut of `show` and is now
pinned by `test_show_marks_the_trainable_turn`.

`health` answers "is it actually recording?" — read-only, so it is safe against a
spool a live process is writing to. It reports, per journey, whether a fold would
produce something exportable and why not if it would not:

```
process:  odyssey.init() has not run in this process

journey                            events undrained writers trainable  problem
call_1                                  3         0       1      True
```

Exit codes are the ADR 0003 contract: `0` ok · `1` runtime failure · `2` usage
error · `3` contract or lineage violation. **`health` exits 3 on a writer
conflict** — the case CI must catch, because a silent interleave of two
conversations is the one corruption that reads as valid data.

### Environment

| Variable | Effect |
|---|---|
| `ODYSSEY_SPOOL` | spool root (default `.odyssey`) |
| `ODYSSEY_OUT` | `FileSink` destination (default `odyssey-out`) |
| `ODYSSEY_ENABLED` | `0`/`false`/`no`/`off` disables recording entirely |
| `ODYSSEY_DRAIN_INTERVAL` | background drain seconds; a bad value falls back to 30 |
| `ODYSSEY_DEBUG` | `1` re-raises capture failures instead of counting them |
| `ODYSSEY_MAX_OPEN_SHARDS` | cached file-handle cap (default 256) |

`ODYSSEY_OUT` names where the artifact lands. The **drain** destination is a
separate decision and is passed to `init(sink=...)`: pointing a `FileSink` at
`out_dir` puts a second copy of every event beside the artifact, which is right
when a collector is going to read it and wrong when the spool is already the log.
The LiveKit deployment in `super` reads three of its own variables on top of
these — `ODYSSEY_WIRE_DIR` (drain the JSONL somewhere, or nowhere),
`ODYSSEY_ALL_STEPS` (skip the `--last-step` trim) and `ODYSSEY_SYSTEM_PROMPT`
(record the prompt as a `system` message). Those live in the integration, not in
core, because they are policy about one deployment's artifacts.

### Maintenance scripts

```bash
bash scripts/manual_check.sh            # 13 hands-on checks — see §9
PY=/path/to/python bash scripts/manual_check.sh

python scripts/make_golden.py           # regenerate the golden fixture
python scripts/make_golden.py --check   # exit non-zero if it would change

python scripts/reformat_equivalence.py > /tmp/before.json
task fmt
python scripts/reformat_equivalence.py > /tmp/after.json
diff /tmp/before.json /tmp/after.json   # a reformat that changed behaviour cannot match
```

`make_golden.py` is deterministic — no clock, no uuid, no network — so the
committed bytes are stable and any diff is a real change. It builds
`JourneyEvent`s directly rather than through the SDK, which is why the capture
layer did not change the fixture.

---

## 8. The cross-project contract

`superdialog` produces the JSONL, odyssey consumes it, **and neither imports the
other.** `tests/test_contract.py` enforces that with 21 tests, in four groups.

**The golden fixture** (12 events) — committed and readable, not stale
(regenerating is a no-op), covers every event kind, preserves
`tool_call_id ↔ tool-result` correlation, folds to a complete journey, yields a
usable preference chain, and carries its reward through the wire.

**Round-trip** — reserialises to events only, never a `Step`; spool → drain →
fold is lossless; the projection is cumulative and monotonic; the schema version
is readable from the header alone.

**No import coupling** — asserted by *parsing imports*, not grepping text.
odyssey does not depend on superdialog; superdialog does not depend on odyssey
(skipped when the sibling checkout is absent); the shared surface is a file
format, not an import.

**Docs don't rot** — the quickstart example is executed as a test. (Two of these
tests are currently no-ops — item 9.8.)

The capture layer was built to leave all of this untouched: `writer_id` rides in
existing `metadata` rather than adding a field, and `make_golden.py --check`
still reports the fixture current. `SCHEMA_VERSION` is `1.1` — the minor bump
that added the header line carrying `data_source`, `trace_id` and
`journey_metadata`, which is what lets a shard be folded without the caller
supplying identity it should not have to know.

### The deployment-side half — what lives in `super`, and why

A second contract runs the other way: odyssey provides the recorder, and the
LiveKit deployment provides the things only it can know. Three of them, all in
`super/core/voice/observability/odyssey.py`, none of which belong in core:

- **Tool capture for the engines LiveKit does not drive.** `function_tools_executed`
  fires only for tools LiveKit itself ran. Flow mode's `llm_node` returns `None`,
  so its tools are HTTP actions executed by superdialog's `ActionExecutor`
  (`adapter.execute_action`); playbook mode runs its own
  `PlaybookRuntime._executor.execute`. Both are wrapped there and replayed into
  `LiveKitRecorder.tool()`, which writes bytes identical to the event path. Before
  that, every flow- or playbook-driven call exported `num_tool_calls: 0` no matter
  how many tools ran, so a booking that died on a 503 read as a clean conversation.
- **What counts as a tool call at all.** A skipped `condition`, a `run_once` cache
  hit and a GET-cache replay all return without a request leaving the process.
  Recording those would inflate the metric past the number of requests that
  actually happened, so they are deliberately not recorded.
- **Artifact policy.** Which of `ODYSSEY_WIRE_DIR` / `ODYSSEY_ALL_STEPS` /
  `ODYSSEY_SYSTEM_PROMPT` a deployment sets is a statement about its own corpus,
  not about the format. Core supplies the switches — `init(sink=...)`,
  `last_step_only=`, `record_instructions=` — and stays out of the decision.

There is also a probe, `install_livekit_tool_probe`, which logs the tools an agent
holds and every tool turn LiveKit executes. It records nothing; it exists because
`num_tool_calls: 0` reads identically whether no tool was needed or capture missed
one, and that ambiguity is not answerable from the artifact alone.

---

## 9. Verification — prove every claim yourself

### The one command

```bash
cd packages/odyssey-core
bash scripts/manual_check.sh
```

Thirteen checks, each printing what actually happened rather than asserting it,
each able to fail. It needs no network, no server, and no provider SDK installed.
Several checks sabotage the process on purpose — `SIGKILL` mid-write, a dead
spool, two processes fighting over one journey — because those are the paths that
matter and the ones a green test run is easiest to disbelieve.

```
 1. init() with zero arguments — config from env only
 2. seq is allocated — the caller never types one
 3. a secret never reaches disk
 4. recording performs no network I/O          (socket() sabotaged)
 5. events survive a hard kill                 (SIGKILL, no cleanup)
 6. atexit flushes — app never calls flush()
 7. a broken spool does NOT take the app down
 8. ODYSSEY_ENABLED=0 turns recording off entirely
 9. two processes on one journey = detected     (exit 3)
10. a restarted process resumes the sequence
11. how fast record() is, measured on your machine
12. auto-capture: 3 turns, provider resends history every time
13. misusing journey() degrades honestly instead of lying
```

Expected tail:

```
===============================================
 passed: 13    failed: 0
===============================================
```

### The individual gates

```bash
cd packages/odyssey-core
uv run pytest tests -q                 # → 468 passed, 1 skipped
task lint                              # → exit 0
task types                             # → 0 errors
python scripts/make_golden.py --check  # → golden fixture is current

# core still has zero third-party deps
grep -A3 '^dependencies' pyproject.toml   # → dependencies = []

# The invariant is module-scope purity: `import odyssey` must not pull a
# provider in. Note the anchored `^` — it excludes indented imports on purpose.
grep -rhE '^(import|from) ' src/ | grep -v odyssey | sort -u   # → stdlib only

# The only third-party imports are lazy, inside a wrapper's own __init__.
# That is what makes `odyssey[anthropic]` an extra rather than a dependency.
grep -rn '^\s\+\(from\|import\) anthropic' src/   # → 3 hits, all in integrations/
```

`test_importing_odyssey_does_not_import_the_provider` asserts the same thing at
runtime: it drops `anthropic` from `sys.modules`, reloads `odyssey`, and checks it
did not come back.

```bash
# --- what Step 0 closed. These now RESOLVE. ---
grep -rl "ContextVar"  src/    # → src/odyssey/context.py
grep -rl "atexit"      src/    # → src/odyssey/client.py
grep -rl "def init"    src/    # → src/odyssey/client.py
grep -rl "def observe" src/    # → src/odyssey/capture.py
wc -c src/odyssey/__init__.py  # → 2838 bytes (was 0)

# the library now builds events for the caller — that IS the layer
grep -rn "JourneyEvent(" src/  # → 2: jsonl.decode_event + capture._emit

# --- what is still open. These must return NOTHING. ---
grep -rE "httpx|requests|urllib" src/   # → none.  L7/item 1.5 — HttpSink
grep -rn "opentelemetry" src/           # → none.  item 0'.3
grep -rn "openai" src/odyssey/integrations/   # → none.  item 0'.1

# still dead code — item 1.11
grep -rn "TelemetryEvent" src/ tests/   # → 2 hits, both inside its own
                                        #   definition. Zero call sites.
```

```bash
# scaffolding is genuinely empty, not "some code"
cd /home/sanyam/odyssey
git ls-files | grep -c gitkeep                # → 70
git ls-files | grep -v gitkeep | grep -v '^packages/odyssey-core' \
  | grep -E '\.(py|ts|tsx|yaml)$' | wc -l     # → 0  (no code outside core)
```

That last command is still the honest summary of the repo: **outside
`packages/odyssey-core`, there is no code at all.**

### Prove the capture layer end to end

```bash
mkdir -p /tmp/odyssey-demo && cd /tmp/odyssey-demo
cat > app.py <<'PY'
import odyssey
from odyssey.primitives import Message, ToolCall
odyssey.init(spool_dir="./.odyssey", out_dir="./out", drain_interval=None)
with odyssey.journey(id="call_8891", user_id="u_42") as j:
    j.message(Message(role="user", content="Book me for Tuesday at 3."))
    j.message(Message(role="assistant", tool_calls=[
        ToolCall(id="tc_1", name="book",
                 arguments={"day": "tue", "api_key": "sk-SECRET"})]))
    j.message(Message(role="assistant", content="Booked for 3pm."),
              model_id="claude-opus-5")
    j.signal("thumbs_up")
print("app done — flush() was never called")
PY
python app.py

grep -c "sk-SECRET" out/call_8891.jsonl   # → 0   never written
grep -c "REDACTED"  out/call_8891.jsonl   # → 1   masked before disk
python -m odyssey.cli --spool ./.odyssey health
```

### Prove the artifact, and the trim

```bash
cd packages/odyssey-core

python - <<'EOF'
from odyssey.export import fold_shard, save
r = fold_shard("tests/fixtures/golden_journey.jsonl")
print(save([r], "/tmp/odyssey-full").written[0].stat().st_size, "bytes, all steps")
print(save([r], "/tmp/odyssey-last", last_step_only=True).written[0].stat().st_size,
      "bytes, last step only")
EOF
# → 6041 bytes, all steps
# → 3230 bytes, last step only

# the trimmed file still holds the whole conversation, and admits the trim
python -c "
import json; d = json.load(open('/tmp/odyssey-last/j_golden_0001.json'))
print(len(d['steps']), 'step', len(d['steps'][0]['messages']), 'messages',
      d['_odyssey']['steps_written'], 'num_turns', d['task']['num_turns'])"
# → 1 step 7 messages last num_turns 3

# the same thing through the CLI
python -m odyssey.cli --spool ./.odyssey export --out ./artifacts --last-step
```

### Prove the system prompt can be left out

```bash
cd packages/odyssey-core
pytest tests/test_livekit.py -q -k "kept_out_of_the_journey or skipped_when_recording_is_off"
# → 2 passed
```

`attach(session, journey_id=..., record_instructions=False)` records the
conversation, the greeting and every tool turn, and writes no `system` message at
all. The switch exists because a deployment's prompt can run to several thousand
tokens of business rules, identical on every call, and copying that into every
exported artifact is a decision the deployment should make rather than inherit.

---

## 10. Known gaps

### Blocker — legal, not technical

**`packages/odyssey-core/NOTICE` names an unresolved copyright holder.** Portions
of `primitives.py` and the builders derive from `trajectory-sdk` 0.5.2 (MIT). MIT
requires the copyright notice be reproduced verbatim, and the holder cannot be
recovered from the source we hold: the vendored tree ships no LICENSE file, its
PKG-INFO declares `License-Expression: MIT` with no `License-File:` header, and
the SOURCES.txt manifest has no license entry.

**Public distribution is blocked until the holder's name is obtained upstream and
substituted, or the derived code is rewritten. Internal use is unaffected.**

This is now more urgent, not less: the whole point of a one-line `init()` is that
people `pip install` it. Whoever owns this should either chase the upstream author
or schedule a rewrite of `primitives.py`.

### Dead code and stale references

- **`TelemetryEvent`** — a `to_api_dict()` targeting
  `POST /api/v1/telemetry/events` and a docstring citing a `push_events()`
  pipeline. Neither exists. Zero call sites. Wire it into Step 1 or delete it.
- **`PiiPolicy` / `RedactionPreview`** — types with no implementation (2.15).
- **`ConversationSummary`** — declared, unused.
- `NOTICE` and `pyproject.toml` both point at `src/odyssey/build/*`. The
  directory is `src/odyssey/builders/`. For `NOTICE` that is the attribution's
  own "Derived files" line, so it should be exact.
- `openspec/changes/add-journey-schema/design.md` is cited by `pyproject.toml`
  and by `fold.py` docstrings ("design.md Decision 4", "Decisions 1 and 8") but
  the path holds only a `.gitkeep`. Item 4.3 needs it.

### Still missing from Phase 1

- **No CI.** `.github/workflows/` holds only a `.gitkeep`, while
  `CONTRIBUTING.md` requires a path-filtered workflow per member. 468 tests
  pass and nothing locks it in. This is the cheapest high-value item in the repo.
- **No ADR for the capture layer** (9.9). It also encodes a deliberate exception
  to the `packages/ = no side effects, no framework imports` rule from
  `STRUCTURE.md`: `init()` installs a global singleton, a background thread and
  an `atexit` hook. That exception should be written down, not discovered.

### Tests that are currently no-ops

Both in `test_contract.py`, passing trivially:

- `test_docs_reference_only_symbols_that_exist` globs
  `packages/odyssey-core/docs/*.md`. That directory does not exist, so the loop
  never runs. (Root `docs/` — including this file — is a different tree.)
- `test_docs_quickstart_still_works` documents itself as a mirror of
  `docs/README.md`. No such file exists.

### Limitations in what *is* built

- **A rebuilt message list can skip a turn.** If the application throws its
  history away mid-journey, the recorded offset is meaningless. The wrapper
  resyncs without re-recording, because a duplicated turn is silent corruption
  while a skipped one is a hole. Counted and visible in `health()`. Pinned by
  `test_a_rebuilt_message_list_resyncs_instead_of_duplicating`.
- **An abandoned `journey()` scope stops recording.** `journey()` is a context
  manager; keeping only the handle and dropping the manager lets CPython
  garbage-collect the suspended generator, which ends the scope. That is API
  misuse, and it degrades honestly rather than lying: the journey closes as
  `STALE` (not a fake application `ERROR`), the error text names the mistake, and
  every subsequent event increments `events_dropped` where `health()` shows it.
  Found by `scripts/manual_check.sh` check 13, not by the test suite — which is
  the argument for having both.
- **SIGTERM loses the spool tail unless opted in.** `atexit` covers normal exit
  and SIGINT, not SIGTERM. Containers get SIGTERM. Pass
  `init(handle_sigterm=True)` in a container, or accept that a stopped pod leaves
  undrained events on disk for the next drain.
- **Async streaming is not wrapped** (0′.5). Sync `messages.stream()` is.
- **No sampling** (0′.6). Every call is recorded.
- **No retention** (1.12 / 2.14). Nothing prunes a drained spool.
- **Content-level PII is not scrubbed** (2.15). Only secret-looking *keys* are.
- **`writer_id` detects a conflict, it does not prevent one.** Two processes
  recording one journey still corrupt it; the fold refuses the result rather than
  exporting it. Per-writer sequences would prevent it and cost a
  `SCHEMA_VERSION` major bump.
- **77 pyrefly errors in `tests/`** (9.10), last measured before the type
  checker lost its config — `pyrefly check` exits asking for `pyrefly init`
  today, so treat the number as stale until 9.2 pins the toolchain in CI.
  Invisible either way because auto-config covers `src` + `scripts` only.
- **The artifact is one training example per call, not per turn, by default in
  the LiveKit deployment.** `last_step_only=True` keeps the final step, which is
  the whole conversation; the per-turn steps that a curriculum wanting one example
  per decision point would use are still available (`ODYSSEY_ALL_STEPS=1`, or just
  omit the flag), but they are not what lands by default.
- **A tool response is stored whole.** A course catalogue or a booking history
  comes back in full, and on a call that lists fifteen courses that single
  response is most of the artifact's bytes. Nothing caps or summarises it yet.

### Files referenced by docs but not written

```
pnpm-workspace.yaml · package.json · pnpm-lock.yaml
docker-compose.yml · .pre-commit-config.yaml
CHANGELOG.md · SECURITY.md · .github/CODEOWNERS
scripts/codegen.sh
docs/architecture.md · docs/journey-schema.md
datasets/registry.yaml · models/registry.yaml
services/api/openapi.json
```

### Toolchain

| Tool | Required | Present |
|---|---|---|
| Python | `>=3.12,<3.13` | 3.12.12 ✓ |
| `uv` | yes | ✓ |
| `task` | yes | ✓ |
| Node | `.nvmrc` = 22 | **v20.20.0 — mismatch** |
| `pnpm` | yes (JS side) | **not installed** |

The Python upper bound is load-bearing: soup (soup-cli, the trainer adapter this
project targets) pins `>=3.10,<3.13` and enforces it with a test that parses its
own CI matrix; the super workspace is `>=3.12`. The intersection is exactly 3.12.

Node and pnpm block nothing yet — the JS side starts at Step 8.

---

## 11. Extension points

**A new drain destination — this is where `HttpSink` (item 1.5) goes.**

```python
class HttpSink:
    def send(self, journey_id: str, events: list[JourneyEvent]) -> None:
        ...   # raise on failure — never return False
```

Pass it to `odyssey.init(sink=HttpSink(...))`, `spool.push(sink)`,
`drain(spool, sink)` or `IntervalDrainer(spool, sink, 30.0)`. All four share one
code path, so a sink behaves identically under every trigger. **The retry is
free:** on an exception the watermark does not advance and the next drain
re-sends the same events. No changes to the spool, the fold or the codec.

Keeping `dependencies = []` means `urllib.request` rather than `httpx`, or an
optional extra with a lazy import — the pattern `integrations/anthropic.py`
already uses.

**A new provider integration** (item 0′.1, OpenAI). Three pieces:

1. A parser — `builders/messages.messages_from_openai_chat` already exists.
2. Request/response capture in `integrations/_base.py`-shape: track how much of
   the message list has been recorded, record the system prompt and tool schema
   only when they change, and separate unknown content types instead of letting
   the parser refuse them.
3. A wrapper that *wraps* rather than subclasses, importing the provider inside
   `__init__`. Subclassing would need the import at class-definition time and
   break `dependencies = []`.

Then add `optional-dependencies` for it and a `sys.modules`-injected fake in the
tests, so the core test path needs no real install.

**A new provider *parser*.** Write
`messages_from_myformat(raw) -> list[Message]` on top of `normalize_role`,
`flatten_text_content` and `parse_tool_arguments`. Raise on anything
unrecognised — that is right for a batch import. On an auto-capture path, filter
unknown shapes first, the way `_base.split_blocks` does.

**A new event kind.** Add the literal to `EventKind`, the payload dataclass, and
an entry in `_PAYLOAD_FIELD` in `primitives.py`; add a decoder branch in
`jsonl.decode_event`; handle it in the `fold()` payload partition. If the on-wire
shape of an existing kind changes incompatibly, bump `SCHEMA_VERSION`'s MAJOR —
old readers then refuse the file instead of mis-parsing it. **This is the path
for voice events (item 0′.4) if `metadata` turns out not to be enough.**

**A new CLI command.** Per ADR 0003, not in core. `cli/` owns the `odyssey`
console script and dispatches plugins lazily from the `odyssey.commands`
entry-point group; core registers `spool = "odyssey.cli:register"` and keeps
working standalone as `python -m odyssey.cli`. The CLI holds no logic — a command
parses arguments, calls a member's public API, and renders.

---

## 12. Further reading

- [`STRUCTURE.md`](STRUCTURE.md) — the full planned tree, organiser rules, CLI surface
- [`adr/0001-monorepo-layout.md`](adr/0001-monorepo-layout.md) — why a monorepo
- [`adr/0002-artifacts-out-of-git.md`](adr/0002-artifacts-out-of-git.md) — git holds the recipe and the hash; the store holds the bytes
- [`adr/0003-single-cli-entrypoint.md`](adr/0003-single-cli-entrypoint.md) — one console script, plugin-dispatched
- [`../CONTRIBUTING.md`](../CONTRIBUTING.md) — setup, adding a member, tier rules, commit format
- [`../packages/odyssey-core/README.md`](../packages/odyssey-core/README.md) — module table and test map

**Missing and needed:** `openspec/changes/add-journey-schema/design.md` (cited by
code), and an ADR for the capture layer — the design in §1 has none, and it
carries a deliberate exception to a tier rule that should be recorded (item 9.9).
