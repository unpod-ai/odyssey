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
| L5 | **Auto-instrumentation** | drop-in clients, decorator, framework callbacks | `@observe`, `openai` shim, LangChain handler, OTel | ✅ Anthropic + OpenAI + Gemini + LiveKit + LangChain/LangGraph + OTel bridge done; LlamaIndex still open |
| L6 | **One-line init** | `odyssey.init()` + env vars + `atexit` flush | `langfuse.init()` | ✅ **done** (`client.py`) |
| L7 | **HTTP transport** | ship to a server, not a folder | `/api/public/ingestion` | ❌ **0%** (only `FileSink`) |
| L8 | **Collector / server** | the "one place" everything lands | Langfuse server | ❌ **0%** |
| L9 | **Dashboard** | look at what landed | Langfuse UI | ❌ **0%** |
| L10 | **Training export** | corpus → SFT/DPO files | (Langfuse: dataset export) | ✅ **Trajectory JSON, SFT, and DPO all ship** (`odyssey export` / `sft` / `dpo`) |

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
python -m odyssey.cli --help        → push · export · sft · dpo · status · show · health
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
- [x] **2** `cli/` — root app, plugin registry, `spool` group (ADR 0003) — also `data` group
- [ ] **3** `odyssey-schemas` + `services/api` + `openapi.json` + `sdk/python`
- [ ] **4** `data_preparation` stages + `datasets/` registry — `normalization` done
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
| 0.9 | **Drop-in provider client** | ✅ | Anthropic, OpenAI, and Gemini (sync + async + patch) done — OpenAI's wrapper also covers OpenAI-*compatible* providers (Groq, Together, local vLLM/Ollama) for free, since they speak the same SDK with a different `base_url`. Gemini (`odyssey.integrations.gemini`, optional `odyssey[gemini]` extra) is a genuinely different SDK shape (`contents`/`parts`, not `messages`; `role="model"` not `"assistant"`; system prompt/tools under `config`, not top-level kwargs) — its own `messages_from_gemini` parser and `_gemini_base.py` capture module, not a reuse of `_base.py`. `google.genai.Client()` exposes both sync (`client.models`) and async (`client.aio.models`) off one object, so there is one `Client` wrapper, not a `Client`/`AsyncClient` pair. Streaming (`generate_content_stream`) not wrapped yet, same open item as 0′.5 for the other two providers |
| 0.10 | **Framework hooks** (LangChain/LangGraph, LlamaIndex) | 🟡 | `integrations/langchain.py` — `OdysseyCallbackHandler()`, lazy `langchain_core` import (`odyssey[langchain]` extra). One flat journey per top-level `run_id`; nested chain/agent graph structure is not modeled as separate journeys or sub-spans, an explicit scope cut. **LangGraph now covered with zero additional code** (item 0′.2) — a compiled graph's `invoke()`/`ainvoke()` and every node/`ToolNode` dispatch through the identical `on_chain_start`/`on_chain_end`/`on_tool_*` callback tree LangChain itself uses, verified against real installed `langgraph`/`langchain-core`. **LlamaIndex hooks not written** — a genuinely different, non-LangChain-compatible instrumentation API, real new work rather than verification like LangGraph. Deliberately deferred, picked up together with item 9.4 (`NOTICE` copyright holder) next |
| 0.11 | **OTel bridge** | ✅ | `integrations/otel.py` — `OdysseySpanProcessor()`, an `opentelemetry.sdk.trace.SpanProcessor` (optional `odyssey[otel]` extra). One journey per **trace** (`trace_id` is OTel's own flattening, no root-tracking of our own needed unlike LangChain's `run_id` tree); a span becomes a `Message` only when it carries `gen_ai.*` content (checked in priority order: `gen_ai.input.messages`/`gen_ai.output.messages` attributes, `gen_ai.content.prompt`/`gen_ai.content.completion` events, then the legacy `gen_ai.prompt`/`gen_ai.completion` attributes — verified against a real span from the installed `opentelemetry-sdk`), the root span's `StatusCode` becomes the termination reason. Explicit scope cut, documented in the module docstring: only the official `gen_ai.*` vocabulary is handled — OpenInference (Arize/LlamaIndex's own OTel integration) and other instrumentation vocabularies use different attribute names entirely and are not covered; a span in an unrecognized vocabulary still gets correct journey lifecycle, just no turn content |
| 0.12 | **Flush on exit** | ✅ | `atexit` by default. SIGTERM is **opt-in** (`init(handle_sigterm=True)`) — `atexit` does not run on SIGTERM, and hijacking a signal from a library is rude |
| 0.13 | Background drain thread | ✅ | `IntervalDrainer`, now started by `init()` |
| 0.14 | Voice-agent turn capture | ✅ | `integrations/livekit.py` — one `attach()` per `AgentSession`: streamed utterances coalesced into one turn, tool calls paired with outputs, the system prompt read off the live agent and followed through handoffs (or skipped with `record_instructions=False`), and `.tool()` for engines LiveKit does not drive. STT confidence and barge-in now emit real `voice` events alongside the turn (item 0′.4) |
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
| 0′.1 | OpenAI drop-in + patch | ✅ | `integrations/openai.py` + `integrations/_openai_base.py`. Simpler than Anthropic's: OpenAI's system prompt is `messages[0]`, not a separate kwarg, so no special dedup case is needed — the existing "record only the unrecorded tail" logic already covers it. Verified against the real `openai` SDK (not just the fake), including `instrument()`'s default patch target. `stream=True` passes through unrecorded (open item, same as Anthropic's async streaming) |
| 0′.2 | LangChain / LangGraph callback handler | ✅ | LangChain done (item 0.10, above). LangGraph needs no additional code — verified against real `langgraph`/`langchain-core` that a compiled graph's own run and every node (including `ToolNode`) dispatch through the same `on_chain_start`/`on_chain_end`/`on_llm_*`/`on_tool_*` tree the existing handler already records; `tests/test_langchain_integration.py`'s LangGraph-compatibility tests replay those exact run shapes |
| 0′.3 | OTel bridge | ✅ | Same as item 0.11 above — `integrations/otel.py`'s `OdysseySpanProcessor()` |
| 0′.4 | **Voice events** | ✅ | Real breaking change: `SCHEMA_VERSION` bumped `1.1` → `2.0`, a new `"voice"` `EventKind`, `VoiceEvent` (`voice_kind: stt_transcript\|tts_output\|barge_in\|latency`, `text`, `confidence`, `latency_ms`), `JourneyEvent.voice`. `fold()` accumulates them into `FoldResult.voice_events`, kept separate from `Journey.messages`/`Step[]` — a voice event has no `trainable` notion. `integrations/livekit.py` now emits `stt_transcript` (turn-level weighted transcript confidence) and `barge_in` (the existing `INTERRUPTED_FLAG`) events. Golden fixture regenerated at 2.0 with a `voice` event. **No migration tool**: a 1.x shard on disk no longer parses under this reader — one-way major bump, documented in CHANGELOG.md |
| 0′.5 | Streaming coverage | ✅ | Both `messages.stream()` (sync `MessageStreamManager`) and the async counterpart (`AsyncAnthropic.messages.stream()`) are wrapped — `_AsyncStreamProxy`/`_AsyncStreamBody` mirror the sync `_StreamProxy`/`_StreamBody` shape exactly, capturing the assembled final message on `get_final_message()`, never per-chunk |
| 0′.6 | Sampling | ✅ | `ODYSSEY_SAMPLE_RATE` / `Config.sample_rate` (default `1.0`, clamped `[0,1]`). The coin-flip happens once per journey at `journey()` open, stored on `JourneyContext.state["_sampled"]` so a nested join inherits the parent's decision rather than re-rolling; `_emit()` drops before touching the spool. `client.count_journey_sampled_out()` surfaced in `health()` |

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
| 1.5 | **`HttpSink`** | ✅ | `sinks.py` — stdlib `urllib` only (core stays `dependencies = []`). One POST per journey batch to `{endpoint}/journeys/{journey_id}/events`, body is the same JSONL bytes a shard on disk holds. Raises `HttpSinkError` on failure, which `drain()` already treats as retryable — no new abstraction needed |
| 1.6 | **Auth** (API key) + project scoping | ✅ | `HttpSink(api_key=...)` / `ODYSSEY_API_KEY` sends a `Bearer` token. `services/collector` now supports two mutually exclusive auth modes: the original single shared `api_key` (unscoped, unchanged), or a `projects` roster (`--keys-file`/`ODYSSEY_COLLECTOR_KEYS_FILE`, a JSON `{"projects": [{"slug", "name", "api_key"}, ...]}` file) — each registered project has a unique `slug` (what actually names its storage partition, `<data_dir>/<slug>/<date>/...`) and a human-readable `name` (for `GET /projects` and operator legibility), so isolation is structural, not just an access check on shared storage. `GET /projects` (any registered key) lists `{slug, name}` for the roster, never keys. A stopgap, documented as such: the roster is a flat file loaded once at startup, not a database — real key/project management belongs to `services/api` (Step 8, not built) |
| 1.7 | **Batching / compression / backpressure on the wire** | ✅ | `HttpSink` gzips the JSONL body by default (`Content-Encoding: gzip`, `compress=False` to opt out); the collector decompresses. A 429 response's `Retry-After` (seconds or HTTP-date) sets a client-side backoff window — `send()` raises without a network call if called again before it elapses. **Connection reuse**: `HttpSink` holds an `http.client.HTTPConnection` across `send()`/`send_batch()` calls (HTTP/1.1 keep-alive; `services/collector`'s `_Handler.protocol_version = "HTTP/1.1"` opts in), so draining N journeys in one process pays for one TCP/TLS handshake, not N. A dropped keep-alive connection is retried once transparently. **Cross-journey payload batching**: `HttpSink.send_batch()` posts several journeys' events in one request to `{endpoint}/batch/events` (a JSON `{"journeys": {jid: "<header line>\n<event line>...", ...}}` envelope — each journey's blob is byte-identical to what a lone `send()` would post); `services/collector` validates and stores each journey independently through the same `_store()` a single-journey POST uses and reports a per-journey `{"ok": true\|false, ...}` result, so `drain()` advances or retries each journey's watermark off *that journey's own* reported outcome — a failure on journey 3 never touches journeys 1/2's already-advanced watermarks, resolving the "needs a real redesign of drain()'s per-journey semantics" concern without weakening it. `drain(spool, sink, batch_size=N)` / `Spool.push(sink, batch_size=N)` / `odyssey.init(drain_batch_size=N)` (`ODYSSEY_DRAIN_BATCH_SIZE`) are all opt-in — default `batch_size=1` never calls `send_batch` at all, byte-for-byte the pre-batching behavior, and a plain `Sink` without `send_batch` is unaffected regardless of what `batch_size` is passed. `send()`/`send_batch()` are not safe to call concurrently from two threads on the same sink (shared connection state) — guarded by an internal lock |
| 1.8 | **Ingest endpoint** (`services/collector`) | ✅ | `services/collector` — stdlib `http.server`, not FastAPI (deliberately; see its README). Receives exactly what `HttpSink` posts, round-trips it through `odyssey.jsonl.read_events`/`write_events` (one codec, not two), writes `<journey_id>.jsonl` files identical in shape to `FileSink`'s output. `GET /health`, optional `Authorization: Bearer` gate. Verified end-to-end with a live smoke test (SDK → spool → HttpSink → collector → readable file) in addition to its own test suite |
| 1.9 | **Server-side idempotency** | ✅ | `services/collector`'s `_store()` reads the destination file's existing `event_id`s before appending and drops any already present, so a retried `HttpSink` POST (lost response, already-committed batch) does not double-write the raw layer. `services/api` (Step 8, not built) will need its own consumer of `WRITER_META_KEY`/`hashing.idempotency_key()` for its own storage layer — this closes the collector's half |
| 1.10 | **Object-store landing** (raw layer) | ✅ | `data_preparation`'s `collection/collect_from_object_store()` — S3-compatible via `boto3` (`odyssey-dataprep[s3]` extra, lazily imported only when no `client=` double is injected). Paginates `list_objects_v2`, reads each `*.jsonl` key, merges by each event's own `journey_id` through the same `_write_merged()` helper `collect_from_collector` uses. Wired into `odyssey data collect --bucket/--prefix/--endpoint-url` |
| 1.11 | `TelemetryEvent` → API | ✅ | **Removed as dead code** rather than wired to a `push_events()` pipeline and `POST /api/v1/telemetry/events` backend that don't exist anywhere in this repo (`services/api` is Step 8, not built) — building a client against a guessed shape for an unbuilt server would be worse than no code. `Telemetry` (no suffix, `JourneyEvent.telemetry`) is unrelated and stays — it is used |
| 1.12 | **Retention / TTL** | ✅ | `spool.gc(spool, *, min_age_seconds, journey_id=None, dry_run=False)` — deletes shards for fully-drained journeys (`watermark(jid) == highest_seq(jid)`) older than the cutoff, skips journeys with an open handle. `odyssey spool prune --older-than-days N [--journey ID] [--dry-run]` CLI. Operator-invoked only — no auto-GC on a timer |

`HttpSink` needed no new abstraction — it is one class with one `send()` method,
and `Spool.push()` / `IntervalDrainer` / the CLI all drive it unchanged, exactly
as predicted. See [§11 Extension points](#11-extension-points).

---

### Step 2 — Raw traces, immutable

| # | Item | Status | Evidence |
|---|---|---|---|
| 2.1 | Append-only local layout | ✅ | `<root>/journeys/<jid>/NNN.jsonl` + `watermarks.json` |
| 2.2 | Shard rotation | ✅ | Survives the handle cache — `test_rotation_still_happens_with_a_cached_handle` |
| 2.3 | Versioned wire format | ✅ | `SCHEMA_VERSION = "2.0"` (bumped from 1.1 for item 0′.4's voice events), unknown MAJOR refuses to parse |
| 2.4 | Truncated-writer tolerance | ✅ | Killed mid-append → every complete event returned |
| 2.5 | Per-line rejection | ✅ | One bad line → one `Rejection`, file still readable |
| 2.6 | Path-traversal / symlink containment | ✅ | `safe_child()` → `SpoolPathError`, on the cold path where it belongs |
| 2.7 | Fold: dedupe · sort · terminal cut · gap detect | ✅ | 31 tests |
| 2.8 | `trainable_status` labelling | ✅ | 5-rule precedence |
| 2.9 | Cumulative step projection | ✅ | Never stored, computed at read time |
| 2.10 | Content hash + idempotency key | ✅ | Canonical JSON, SHA-256 |
| 2.11 | Cross-project contract test | ✅ | Golden fixture (13 events, incl. one `voice` event) + parsed-import gate |
| 2.12 | **Writer-conflict detection** | ✅ | `FoldResult.writers` / `.writer_conflict` / `.incomplete_reason`; CLI exits 3 |
| 2.13 | **Bounded open file descriptors** | ✅ | `max_open_shards` (default 256), LRU eviction, closed on terminal |
| 2.14 | Retention / TTL on the raw layer | ✅ | `services/collector/prune.py` — `prune_dir(data_dir, older_than_days, dry_run=False)` deletes whole date-partition directories older than the cutoff; `python -m odyssey_collector.prune` CLI. Same operator-invoked-only shape as 1.12. Unaware of project scoping (item 1.6) — in project-scoped mode, point `--data-dir` at `<data_dir>/<project_id>` once per project rather than at the root |
| 2.15 | **Content-level PII scrub** | ✅ | `odyssey.pii` — regex `scan_pii`/`redact_pii` for EMAIL/PHONE/CREDIT_CARD (Luhn-checked)/SSN, matching the existing `PiiRule` Literal. `data_preparation`'s `clean_dir(..., pii_policy=...)` (opt-in, off by default) and `validate_dir(..., content_pii_rules=...)` wire it in; `odyssey data clean/validate --pii-rules email,phone,...` CLI. Regex-based pattern matching, not NER — documented as such |

This step is the strongest part of the repo, and 2.14–2.15 are now closed.

---

### Step 3 — `data_preparation` (7 stages)

All nine stages have real code now, reachable via `odyssey data <cmd>`.

| # | Stage | Status | What it needs |
|---|---|---|---|
| 3.1 | `collection/` | ✅ | `collect_from_spool`/`collect_from_collector`/`collect_from_object_store` (item 1.10) — reassembles rotated (spool), date-partitioned (collector), or S3-key-listed (object store) shards into one flat `*.jsonl` per journey, grouped by each event's own `journey_id`, not filename |
| 3.2 | `cleaning/` | ✅ | `dedupe_journeys` (by `content_hash`), `drop_dead_turns` (splices a dead delta out of every later step's cumulative history, not just a naive per-message filter), `repair_encoding` (NFC + strip C0 controls), `scrub_pii_content` (item 2.15, opt-in via `pii_policy=`) |
| 3.3 | `normalization/` | ✅ | `data_preparation/src/odyssey_dataprep/normalization/` — `normalize_odyssey_dir` (thin wrapper over `export_dir`) and `normalize_byod_dir` (parse via `builders/messages` + `build_journey_from_messages`, dispatched by format name). Also fixes a real gap found while building it: `build_journey_from_messages` runs no `fold()`, so BYOD messages kept the dataclass default `trainable_status="not_trainable"` including the assistant's own replies — useless to every later stage. Now reuses `fold.derive_trainable_status` directly (empty signal list) to label them, same rule an odyssey-recorded journey with no signals gets |
| 3.4 | `annotation/` | ✅ | `build_queue` (one JSONL line per journey, with a preview) + `apply_reviews` (a decision's `score` becomes the journey's `Reward` via `build_reward_from_scalar`, reused not re-derived; `approved`/`notes` land under `telemetry.data.annotation`). No external queue system — a local JSONL file is the queue |
| 3.5 | `augmentation/` | ✅ | `perturb_tool_calls` — deterministic synthetic negatives via a dropped required argument, always on. `paraphrase_journey`/`generate_synthetic_negative` (optional `odyssey-dataprep[llm]` extra) add the two LLM-backed techniques, both opt-in (off by default — an LLM call per journey is a real cost this stage does not spend unless asked): `paraphrase_journey` rewords only the real user turns, keeping the assistant's trainable output byte-identical; `generate_synthetic_negative` asks a model for a plausible-but-wrong response to a real prompt and emits it as a `superseded`-then-`trainable` step chain, the exact shape `odyssey.dpo.dpo_pairs` looks for — verified against that extractor's real ordering rule, not guessed. Both take a caller-injected `client` (an `openai.OpenAI`-shaped object), the same dependency-injection seam `collect_from_object_store` uses for `boto3`; wired into `odyssey data augment --paraphrase N --synthetic-negatives --llm-model` |
| 3.6 | `validation/` | ✅ | `validate_schema`, `check_pii_redaction` (reuses `odyssey.spool._is_secret`'s exact matching rule, not a re-derived one), `check_leakage`, `check_drift`. `odyssey data validate` exits 3 on breach — the lineage-violation code CI greps for (ADR 0003) |
| 3.7 | `splitting/` | ✅ | `split_dir` — groups by `trace_id` (falls back to the journey's own id), assigns via a deterministic hash of the group key, never `random`. `test_split_dir_never_splits_a_group_across_two_splits` is the test 3.7 explicitly demanded |
| 3.8 | `flows/` | ✅ | `run_recipe` — a stdlib sequencer over `collection`/`normalization`/`cleaning`/`validation`/`splitting` (uniform dir-in/dir-out contract), reading a `Recipe`. Deliberately not Prefect — no scheduling/retry/UI need to justify the dependency. `validation` is a gate (does not advance the working directory, aborts the run on breach); `splitting` fans out and must be last. `annotation`/`augmentation` don't fit the uniform contract and are called directly, not sequenced |
| 3.9 | `recipes/*.yaml` | ✅ | `data_preparation/src/odyssey_dataprep/recipes/` — `load_recipe`/`Recipe`/`RecipeStage`; declarative, order-sensitive, no stage-name validation (a recipe can name a stage before it exists) |

77 tests in `data_preparation/tests/`, all real (dead-turn splicing, leakage
detection, deterministic split assignment, cross-date-partition merging —
each verified against a case that would actually fail if the logic were
wrong, not just "runs without crashing").

---

### Step 4 — Corpus: `version = sha(recipe_hash + curated_watermark)`

| # | Item | Status | Note |
|---|---|---|---|
| 4.1 | Stable canonical hashing | ✅ | `hashing.content_hash()` |
| 4.2 | Per-journey delivery watermark | ✅ | `Spool.watermark()` |
| 4.3 | **`curated_watermark`** | ✅ | `openspec/changes/add-journey-schema/design.md` Decision 9 — `{seq, hash}`, `hash = content_hash` over the sorted `(journey_id, journey_content_hash)` set. Implemented: `data_preparation/src/odyssey_dataprep/versioning.compute_curated_watermark` |
| 4.4 | **`recipe_hash`** | ✅ | `odyssey_dataprep.recipes.recipe_hash` — `content_hash` over the recipe's own dict, reused not reinvented |
| 4.5 | **Corpus version function** | ✅ | `odyssey_dataprep.versioning.corpus_version` — `content_hash({"recipe": recipe_hash, "watermark": curated_watermark})` per Decision 9. Reachable via `odyssey data recipe-hash` / `odyssey data corpus-version`, verified end-to-end against a real recipe file and a curated directory |
| 4.6 | `datasets/registry.yaml` | ✅ | `odyssey_dataprep.datasets.update_registry` — `name -> versions -> manifest sha -> URI`, per `docs/STRUCTURE.md`. `uri` falls back to the manifest's own git-tracked path; the object store landed for raw-layer collection (1.10), not wired into this stage's URI resolution |
| 4.7 | `datasets/manifests/<name>/v1.json` | ✅ | `odyssey_dataprep.datasets.build_manifest`/`write_manifest` — shards + sha256 + row counts + `recipe_hash`, computed from the actual shard files, not trusted from the caller. `next_version` doubles as `curated_watermark.seq` (design.md Decision 9: one curation run = one seq) |
| 4.8 | `datasets/cards/` | ✅ | `odyssey_dataprep.datasets.write_card` — provenance (from the manifest) + license/PII posture/intended use (caller-supplied, policy calls no code can infer) + splits (defaults to "not yet split", 3.7 doesn't exist). Reachable via `odyssey data build-corpus` / `odyssey data card`, verified end-to-end |

---

### Step 5 — Training

`training/` — new workspace member (`odyssey-training`).

| # | Item | Status | Note |
|---|---|---|---|
| 5.1 | `trainable` gate on export | ✅ | `FoldResult.trainable`, and now `.incomplete_reason` says why not |
| 5.2 | Per-turn `trainable_status` | ✅ | Only assistant turns carry gradient by default |
| 5.3 | Preference chain (chosen / rejected) | ✅ | `Signal` with `regen_order` + `edited_output`, **now emitted by the SDK** — `test_a_regeneration_supersedes_the_earlier_answer` |
| 5.4a | **Trajectory JSON export writer** | ✅ | `export.py` + `odyssey export` — one `{conversation_id}.json` per conversation, the shape `tj.save()` produces and the platform consumes. `--last-step` / `last_step_only=True` writes the final step alone: every step is a prefix of the next, so all N cost O(N**2) bytes and the last one already holds the whole conversation |
| 5.4b | **SFT export writer** | ✅ | `sft.py` + `odyssey sft` — one JSON line per trainable step (`{"messages": [...]}`), one combined `.jsonl` file (a training shard, not one-file-per-conversation like Trajectory JSON). Only `trainable_status == "trainable"` steps in a `trainable` (complete) journey are emitted |
| 5.5 | **DPO pair extractor** | ✅ | `dpo.py` + `odyssey dpo` — walks `journey.steps` in order; a run of `superseded` steps immediately followed by a `trainable` one is a chain, and every rejected candidate in it pairs against the winner. Verified against the golden fixture's own regenerated→user_edit→thumbs_up chain (2 pairs). **KTO/ORPO not done** — those want unpaired single-response labels, a different data shape |
| 5.6 | **soup-cli adapter** | ✅ | `training/src/odyssey_training/soup_adapter.py` — `write_sft_config`/`write_dpo_config`/`translate_dpo_shard`, reachable via `odyssey train sft-config`/`odyssey train dpo-config`. Checked against the *real, installed* `soup-cli` 0.73.3 (`soup_cli.config.schema.SoupConfig`, `soup_cli.data.formats`), not guessed from docs: `odyssey sft` already writes soup-cli's `chatml` format verbatim (`_convert_chatml` is a literal passthrough); `odyssey dpo`'s `chosen`/`rejected` are a single message, soup-cli's `dpo` format wants a message *list* (matching `trl.DPOTrainer`'s conversational contract) — `translate_dpo_shard` does that one wrap. Every generated config round-trips through soup-cli's own real `load_config`, not just our own schema import. `soup-cli` installed light-only (no `[train]` extra — no torch in this member) |
| 5.7 | `configs/{sft,dpo,grpo}` | ✅ | `soup_adapter.write_grpo_config` (mirrors 5.6's `write_sft_config`/`write_dpo_config`), reachable via `odyssey train grpo-config`; `base.yaml`/`sft/example.yaml`/`dpo/example.yaml`/`grpo/example.yaml` replacing the `.gitkeep`s |
| 5.8 | `experiments/<exp_id>.yaml` | ✅ | `odyssey_training.experiments.write_experiment_manifest` — config sha + corpus version + metrics ref, reachable via `odyssey train record-experiment` |
| 5.9 | **Checkpoint → object store** | ✅ | `odyssey_training.checkpoints.upload_checkpoint(checkpoint_dir, bucket, prefix, ...)` — S3-compatible via `boto3` (`odyssey-training[s3]` extra, lazily imported only when no `client=` double is injected, mirroring item 1.10's `collect_from_object_store`). Uploads every file under a `soup train --output` dir in sorted relative-path order, sha256'd locally before each `put_object` (never trusted from the response); returns `{uri, files, manifest_sha256}` — `manifest_sha256` is `content_hash` over the sorted `(key, sha256)` set, deterministic across repeat uploads of the same checkpoint. `experiments.write_experiment_manifest` gained `checkpoint_uri`/`checkpoint_sha256` params recording that pointer alongside `metrics_ref`, exactly the "git holds the recipe and the hash, the object store holds the bytes" split ADR 0002 already applies to the corpus/model layers. `odyssey train upload-checkpoint` / `odyssey train record-experiment --checkpoint-uri/--checkpoint-sha256` |

**On Unsloth** (researched while building 5.6): it is not a separate framework
to integrate — it patches Transformers/PEFT/TRL for faster training and is
one of soup-cli's own three `backend` choices (`transformers` default,
`unsloth`, `mlx`), selected by a config field our adapter already exposes.
Nothing to build here beyond what 5.6 already does.

**Step 5 is now fully closed** — 5.9 was the last open item.

**L10 lives here, and it is now the second-biggest gap.** The schema was designed
for DPO from day one — that is why `Signal` carries an *ordering* rather than
just a scalar — and the SDK now produces those signals end to end. Nothing yet
turns them into a training file.

---

### Step 6 — Models registry

| # | Item | Status | Note |
|---|---|---|---|
| 6.1 | **`models/registry.yaml`** | ✅ | `odyssey_training.models_registry.register_model(registry_path, name, *, sha256, uri, base_model, corpus_version, version=None)` — `name -> version -> sha256 -> URI -> base model -> corpus version`, per `docs/STRUCTURE.md`'s own schema. `version` defaults to `next_version(...)` (highest existing + 1, mirroring `odyssey_dataprep.datasets.next_version`'s rule for corpus versions); passing one explicitly is idempotent on `(name, version)` — replaces in place rather than duplicating, the same discipline `datasets.update_registry` already applies. `sha256`/`uri` are meant to be `checkpoints.upload_checkpoint`'s own `manifest_sha256`/`uri` (item 5.9) — this module does not re-verify them, the same caller-trust boundary `datasets.write_card`'s license/PII fields already accept. New top-level CLI group: `odyssey model register` |
| 6.2 | **`models/cards/<model>-v1.md`** | ✅ | `models_registry.write_model_card(entry, name, cards_root, *, license, intended_use, limitations, eval_summary=None)` — mirrors `datasets.write_card`'s own shape: provenance pulled from the registry entry itself, license/intended-use/limitations as the caller's own policy claims (no code can infer them). `eval_summary` defaults to "not yet evaluated" since `evaluation/` (Step 7) doesn't exist yet — the same honesty `write_card`'s `splits` default already used before item 3.7 existed. `odyssey model card` |
| 6.3 | Weights stay out of git | ✅ | `.gitignore` + `.gitkeep`, ADR 0002 |
| 6.4 | **Promote / export commands** | ✅ | `models_registry.promote_model(registry_path, name, version, *, alias="production")` points a named alias at an already-registered version — kept as a separate, deliberate act from `register_model` (minting a version and deciding it's the one to serve are different decisions). `resolve_model(registry_path, name, *, version=None, alias=None)` looks either up (exactly one required, `KeyError` on an unknown name/version/alias). `export_model(registry_path, name, out_dir, *, version=None, alias=None, ...)` resolves the entry, then `checkpoints.download_checkpoint()`s it (the inverse of item 5.9's upload) and verifies the freshly downloaded `manifest_sha256` against the registry's own recorded `sha256`, raising on mismatch. Downloads the checkpoint's original files as uploaded — **does not** convert to a serving format (GGUF/ONNX/safetensors, `models/exported/`'s own stated purpose); that's real, format-specific ML tooling with no consumer named yet, an explicit scope cut in the same spirit as 0.11's OTel bridge and 3.5's LLM augmentation extra before those had a named consumer. `odyssey model promote` / `odyssey model export` |

`model_id` is tracked **per event** and the Anthropic wrapper now populates it
from the provider response, so `fold()` sets a journey-level `model_id` only when
the journey never switched models. Provenance is correct at the source, and
**Step 6 is now fully closed**: the registry (6.1) that consumes it exists,
along with model cards (6.2) and promote/export (6.4). Weight-format
conversion for serving remains a documented, deliberate scope cut.

---

### Step 7 — Evaluation

| # | Item | Status |
|---|---|---|
| 7.1 | `evaluation/src/odyssey_eval/` harness | ✅ `runner.py`/`harness.py`/`cli.py` |
| 7.2 | Frozen eval sets, never trained on | ✅ `eval_datasets.py` (manifest/registry/card) |
| 7.3 | Benchmarks + metric code | ✅ `benchmarks/*.yaml` + `metrics/*.py` (`exact_match`, `tool_call_accuracy`) |
| 7.4 | `dataset-audit.yml` no-overlap gate | ✅ `overlap.py` + `audit.py`, wired into CI |
| 7.5 | Reports | ✅ `reports/templates/`, gitignored `reports/` output |

`odyssey-eval` is a new uv workspace member (name `odyssey-eval`, pkg
`odyssey_eval`), `odyssey eval run/compare/build-set/card/check-overlap`
mounted via the standard entry-point plugin contract. **Offline scoring
only** — no live model-serving path exists in this repo yet, so the harness
takes a benchmark suite (`evaluation/benchmarks/*.yaml`: prompts +
references + which metric) and a caller-produced completions JSONL, and
scores the pairing; it never calls a model itself. `judges.py`
(LLM-as-judge, named in `docs/STRUCTURE.md`) is deliberately **not built** —
same explicit-deferral treatment items 0.11/3.5 got before a concrete
consumer existed, documented in `harness.py`'s own docstring; the metric
interface (`metrics/*.py`'s `score` function, loaded via `importlib`, not
baked into the package) is designed so a future `judges.py` metric slots in
with zero harness changes.

7.2's `eval_datasets.py` mirrors `odyssey_dataprep.datasets`' manifest/
registry/card shape exactly, minus `recipe_hash`/`curated_watermark` (those
describe *how a training corpus was curated*, not applicable to a frozen or
hand-built eval set). "Frozen" is enforced downstream by 7.4's
`overlap.check_no_overlap`, which reuses `odyssey_dataprep.validation.
check_leakage` directly (its generic `{split: [ids]}` shape already covers
"eval vs train"), not by any write-protection in `eval_datasets.py` itself.
7.4's `audit.py` additionally gates manifest `sha256` integrity for both
registries (`data_preparation/datasets/registry.yaml` and `evaluation/
datasets/registry.yaml`) — new `dataset-audit.yml` CI workflow, exits 3 on
either kind of breach, the lineage-violation code CI already greps for
(ADR 0003). Journey-level metrics that already existed pre-Step-7 —
`steps`, `num_tool_calls`, `num_tool_failures`, `tool_error_rate`,
`num_tool_response_none`, `aggregated_reward`, `total_time` — are consumed
by `tool_call_accuracy` rather than re-derived.

New `ci-eval.yml`, path-filtered on `evaluation/**` + `packages/odyssey-core/**`
+ `data_preparation/**` (the last because `overlap.py`/`audit.py` import
`odyssey_dataprep`). 21 new tests, all passing; full workspace (894 tests
across 6 members) re-verified green. Verified against a real end-to-end
`odyssey eval run` (a hand-built 3-task arithmetic benchmark, 2/3 correct)
and a real `check-overlap` breach/no-breach pair, not just unit tests.

---

### Step 8 — Serving: api → sdk → web

| # | Item | Status |
|---|---|---|
| 8.1 | `packages/odyssey-schemas` (pydantic DTOs) | ✅ |
| 8.2 | `services/api` (FastAPI) | ✅ |
| 8.3 | `services/api/openapi.json` | ✅ |
| 8.4 | `sdk/python` (generated) | ✅ |
| 8.5 | `sdk/javascript` (`@odyssey/sdk`) | ✅ |
| 8.6 | `apps/web` (Next.js dashboard) | ✅ |
| 8.7 | `scripts/codegen.sh` + CI drift gate | ✅ |

Note the overlap: **8.2 and 1.8 are the same server**, per this file's own
earlier note. Resolved by *not* merging them: `services/collector` (1.8)
keeps owning ingest (stdlib, idempotency, project-scoping, gzip/`Retry-After`
backoff — a real rewrite risk with no functional gain today), and
`services/api` (8.2) is a pure read layer over the exact files the collector
already writes. If a genuine "one server" need shows up later, this is a
swap of `services/api`'s `repositories/filesystem.py`, not a rewrite of its
routers/domain layer.

**Naming collision (was ⚠️, now resolved):** `STRUCTURE.md` reserves the
distribution name `odyssey-sdk` / package `odyssey_sdk` for the *generated
OpenAPI client*. The capture layer that people will call "the SDK" lives in
`odyssey-core` (distribution name `odyssey`) — no actual Python package-name
clash, just a naming ambiguity. Resolved by keeping `STRUCTURE.md`'s
names as-is and documenting the distinction in `sdk/python/README.md`
rather than renaming either package.

`packages/odyssey-schemas` (item 8.1) — pure pydantic DTOs (`HealthOut`,
`JourneySummaryOut`/`JourneyDetailOut`/`StepOut`/`JourneyMetricsOut`,
`DatasetOut`/`DatasetVersionOut`, `ModelOut`/`ModelVersionOut`, `EvalRunOut`,
`ExportArtifactOut`), no `fastapi` or `odyssey-core` dependency — a stable
wire contract a future generated SDK (8.4) can depend on without pulling in
the service's own `fastapi`/`uvicorn` dependencies.

`services/api` (item 8.2) — new FastAPI workspace member (`odyssey-api`),
layered per `docs/STRUCTURE.md`: `routers/` (parse/validate/render only) ->
`domain/` (use-cases, zero fastapi imports) -> `repositories/filesystem.py`
(the only repository built — see below). Routes: `GET /health`,
`/journeys` + `/journeys/{id}` (folds a collector-written shard through
`odyssey.export.fold_shard`, the same path every exporter uses),
`/datasets` + `/datasets/{name}` and `/models` + `/models/{name}` (read
`data_preparation`'s / `training`'s own `registry.yaml` files directly, no
new registry), `/runs` (reads `odyssey eval run`'s own `*.json` reports),
`/exports` (lists `*.jsonl` shards in a caller-configured directory,
sha256/row-count computed fresh — no export registry exists anywhere in
this repo). New `odyssey api serve/openapi/routes` CLI commands.

**Deliberately not built**, same explicit-deferral treatment `judges.py`
(item 7) and 0.11/3.5 got before a named consumer existed: `repositories/
mongo.py`/`postgres.py`/`objectstore.py` (only `filesystem.py` — every
store this service reads is a real file today, same state
`odyssey_dataprep.datasets` is already in), `workers/drain_consumer.py`
(Kafka -> spool drain — no Kafka broker/topic exists anywhere in this
repo), `migrations/` (alembic — no relational schema to migrate). Each has
its own README documenting the deferral in place, not a silently-missing
`.gitkeep`.

`services/api/openapi.json` (item 8.3) — generated via `odyssey api
openapi` from the live `FastAPI.openapi()` schema, committed (the
"generated, committed — SDK + web codegen input" contract `docs/
STRUCTURE.md` names). Hit one real starlette-version quirk while building
`odyssey api routes`: this repo's pinned starlette (1.6.0, via fastapi
0.141.1) wraps each `include_router()` call as an opaque `_IncludedRouter`
on `app.routes` instead of flattening routes in place —
`.original_router.routes` is where the real `APIRoute`s live; `routes()`
walks both shapes so it isn't starlette-version-fragile.

25 new tests (7 `odyssey-schemas`, 18 `odyssey-api`), full workspace
re-verified green (919 tests across 8 members).
Verified against a real end-to-end run: a real journey shard written via
`odyssey.jsonl.write_events`, a real `uvicorn` process started via
`odyssey api serve`, `curl` against `/health`, `/journeys`,
`/journeys/{id}`, `/datasets`, `/models`, and the `/journeys/nope` 404
path — not just `TestClient` unit tests. `uv run odyssey doctor` confirms
cold `--help` is still 315ms, comfortably under budget, even with
`fastapi`/`uvicorn` now in the shared venv.

`sdk/python` (item 8.4) — new workspace member (`odyssey-sdk`, pkg
`odyssey_sdk`). `client.py`/`errors.py`/`models.py`/`codegen.py` are
hand-written; `resources/{journeys,datasets,models,runs,exports}.py` are
generated by `odyssey_sdk.codegen` from `services/api/openapi.json` — one
class per resource (`client.journeys.list()`/`.get(id)`, etc.), each
method's return type an `odyssey_schemas` DTO, `.model_validate()`d from
the real JSON response. `Transport` is stdlib `urllib` only, same
discipline as `odyssey.sinks.HttpSink` — this package has no dependency on
`odyssey-core` or `odyssey-api` at runtime (only `odyssey-schemas`), only
in `dev` extras (a real `uvicorn`-served `services/api` instance is what
its own test suite runs against). New `odyssey sdk codegen`/`check-drift`
CLI commands (item 8.7).

`services/api/cli.py`'s `openapi` command gained `--check` (exits 3 on
drift, same "drifted openapi" contract-violation code
`docs/STRUCTURE.md`'s CLI rules already name). New `scripts/codegen.sh`
(item 8.7) runs `odyssey api openapi` then `odyssey sdk codegen` (Python)
then `pnpm --filter @odyssey/sdk codegen` (JS, item 8.5) in sequence —
regenerating all three, in dependency order; a new `codegen-drift.yml`
CI workflow runs the `--check`/`check-drift` variants of the same steps
and fails the build on any kind of drift. `ci-sdk.yml` is the "sdk/**
(py + js matrix)" workflow `docs/STRUCTURE.md` names — both legs exist
now.

11 new tests (7 codegen/client, all against a real live `services/api`
instance for the client tests, not a mocked transport), full workspace
(926 tests across 9 members) green. Verified end to end: `./scripts/
codegen.sh` run twice back-to-back produces zero diff (idempotent), a
deliberately-broken `resources/journeys.py` edit is caught by `odyssey sdk
check-drift` (exit 3), and the generator raises rather than guessing on
`POST`/multi-path-param operations should `services/api` ever grow one
(narrowness by design, not an oversight).

`sdk/javascript` (item 8.5) — new pnpm workspace member (`@odyssey/sdk`),
built the same session as the `apps/web` rewire below. Mirrors
`sdk/python` 1:1: `client.ts`/`errors.ts`/`codegen.ts` are hand-written,
`types.generated.ts` + `resources/*.ts` are generated by `src/codegen.ts`
from `services/api/openapi.json` with the exact same narrowness
(`GET`-only, ≤1 path param, raise — don't guess — outside that shape).
`tsup` builds ESM + CJS + `.d.ts`. This pass also converted the whole JS
side of the repo from `apps/web`'s standalone npm setup (a documented
deviation from `docs/STRUCTURE.md`'s "pnpm (js)" — pnpm wasn't installed
when `apps/web` was scaffolded) to a single root `pnpm-workspace.yaml` +
`pnpm-lock.yaml` covering both `apps/web` and `sdk/javascript`, per the
organiser rule "one lockfile per ecosystem." `pnpm test` spawns a real
`uv run odyssey api serve` instance as a child process (same convention
`sdk/python`'s own tests use, not a mocked `fetch`).

`apps/web` (item 8.6) — new Next.js 16 (App Router, TypeScript, pnpm)
workspace at `apps/web/`, not a uv/Python member. `docs/STRUCTURE.md`'s
page list (`journeys/datasets/experiments/models/reports`) is adapted to
`journeys/datasets/models/runs/exports` — the resources `services/api`
actually exposes; "experiments"/"reports" have no backing endpoint. Now
consumes `@odyssey/sdk` directly, per `docs/STRUCTURE.md`'s rule
("`apps/web` consumes `@odyssey/sdk`, NOT its own generated client") —
the hand-written `src/lib/api/{types,client}.ts` stand-in that existed
until item 8.5 landed is gone; `src/lib/api/index.ts` is now just
`apiClient()`, building an `OdysseySDK` from `ODYSSEY_API_BASE_URL`. Every
page is a React Server Component fetching server-side through the SDK, no
client-side data-fetching library needed for a read-only dashboard; a
failed fetch renders inline rather than crashing.

Hit one real `eslint-config-next` rule while building the journey-detail
page: `react-hooks/error-boundaries` flags constructing JSX inside a
`try/catch` (errors thrown during React's actual render pass, later than
the JSX call, wouldn't be caught anyway) — fixed by separating "fetch
+ catch" from "render" in every page, not by suppressing the rule.

`ci-web.yml` (lint + vitest + `next build`, which also type-checks and
prerenders every route) now installs via the root pnpm workspace instead
of `npm ci`. **No browser/e2e runner wired up** (`tests/e2e/` stays
empty, documented) — this repo's sandbox has no browser tool, so
verification was instead: a real `services/api` instance started via
`uvicorn`, `pnpm dev` pointed at it via `ODYSSEY_API_BASE_URL`, and
`curl` against every route (`/`, `/journeys`, `/journeys/{id}` including
a 404 for a missing journey, `/datasets`, `/models`, `/runs`, `/exports`)
with the *resolved* server-rendered HTML inspected (React inserts
`<!-- -->` hydration-boundary comments between text nodes, which a naive
`grep` missed at first — worth knowing for anyone else grepping Next.js
SSR output) to confirm real API data actually reached the page through
the real `@odyssey/sdk` import, not a `TestClient`-only check. The
`src/lib/api` vitest suite now just checks `apiClient()` builds an
`OdysseySDK` from `ODYSSEY_API_BASE_URL` — the URL-building/error-mapping
logic it used to test now lives (and is tested) in `@odyssey/sdk` itself.

---

### Step 9 — Repo hygiene

| # | Item | Status | Cost |
|---|---|---|---|
| 9.1 | `src/odyssey/__init__.py` public API | ✅ **done** — 131 LOC, 50 exports | — |
| 9.2 | CI (`.github/workflows/ci-core.yml`) | ✅ **done** — fmt/lint/types/test + golden-fixture check, path-filtered | — |
| 9.3 | `cli/` single entrypoint (Phase 2, ADR 0003) | ✅ | New `typer`+`rich` workspace member; lazy plugin registry via `odyssey.commands` entry points. All 7 `odyssey-core` subcommands (`push`/`export`/`sft`/`dpo`/`status`/`show`/`health`) mounted under `odyssey spool`, plus `odyssey data normalize` from `odyssey-dataprep`. Deprecated `odyssey push`/`odyssey status` top-level aliases warn to stderr. Cold `--help` (`odyssey doctor`, best-of-3) budget moved twice — 200ms→400ms, then 400ms→700ms once `training/`'s `soup-cli` dependency pushed CI to 440ms — see `doctor()`'s own docstring: this is a shared-venv cost (entry-point discovery + import-path setup scale with every member's dependencies combined), not a per-command regression, and it will keep rising as more members gain real dependencies. Core drops `[project.scripts]`; `python -m odyssey.cli` unaffected |
| 9.4 | `NOTICE` copyright holder | ❌ | **blocks public release** — see [§10](#10-known-gaps) |
| 9.5 | Stale `src/odyssey/build/` path in `NOTICE` + `pyproject` | ✅ | Fixed to `src/odyssey/builders/`, the directory that actually exists |
| 9.6 | `openspec/.../design.md` (cited by code, absent) | ✅ | Written — Decisions 1/4/8 reconstructed from what the shipped code already establishes, Decision 9 (new) defines `curated_watermark`, closing 4.3 |
| 9.7 | `.pre-commit-config.yaml`, `CHANGELOG.md`, `SECURITY.md`, `CODEOWNERS` | ✅ | All four written — `.pre-commit-config.yaml` mirrors the isort/black/flake8 versions and args every member's own `Taskfile.yml` already runs; `CODEOWNERS` lives at `.github/CODEOWNERS`, GitHub's own convention |
| 9.8 | Two no-op contract tests | ✅ | `packages/odyssey-core/docs/README.md` written — the 60-second quickstart both tests were checking against but that never existed. Verified live: breaking a backticked symbol in it now fails `test_docs_reference_only_symbols_that_exist` |
| 9.9 | **ADR for the capture layer** | ✅ | [`adr/0004-capture-layer.md`](adr/0004-capture-layer.md) — the design in §1 (event-sourced core, ambient context, single-writer contract with detection, never-raise boundary, local-only recording), and the deliberate exception it carries to ADR 0001 rule 1 (`packages/` = no side effects) |
| 9.10 | pyrefly errors in `tests/` | ✅ | **Closed 2026-08-28.** `[tool.pyrefly] project-includes = ["src", "tests", "scripts"]` is now permanent in `pyproject.toml`, so `task types` checks all three every run, not `src`/`scripts` only. Turning that on surfaced 200 errors, not the ~157 previously estimated: 4 were real bugs in `src/` (a `Callable[[], None]` `_guard` signature in `langchain.py`/`otel.py` too narrow for the lambdas actually passed to it, a loosely-`str`-typed `_ROLE_BY_TYPE` dict and `_end(reason: str)` in `langchain.py` that should have been the real `Role`/`TerminationReason` literals) — all four fixed as real type-safety fixes, not suppressions. The remaining 196 were exactly the predicted `tests/` narrowing gaps — `Optional[...]`-typed fields (`Message.metadata`, `.tool_calls`, `.tool_response`, `Terminal.termination_reason`, journey `.metrics`/`.telemetry`/`.reward`, etc.) accessed without narrowing first. Fixed per call site: `assert x is not None` immediately before reuse, `(x or {})[...]`/`(x or [])[...]` for one-off accesses, and a few test-helper signatures widened from `str` to the real `Literal` type (`Role`, `TerminationReason`, `PiiRule`) rather than casting at every call site. Two genuine non-narrowing issues also surfaced and were fixed on their own terms: `test_sinks.py`'s `_CapturingHandler.log_message` had a signature that didn't match `BaseHTTPRequestHandler`'s (missing the `format` parameter), and `test_livekit.py`'s `FakeSession.current_agent` (set dynamically by several tests, overridden as a `@property` by `UnstartedSession`) needed a class-level `Any` type-hint-only annotation rather than a real `__init__` assignment, which would have collided with the property override. `uv run pyrefly check` is 0 errors (67 suppressed); `task check` fully green (643 passed, 1 skipped) |

---

### Recommended order

The dependency graph, not the wish list. Steps 0, 1.5/1.6, 1.8, 5.4/5.5, and
9.3 are done — record → spool → `HttpSink` → `services/collector` → durable
file → `odyssey sft`/`odyssey dpo`, all reachable through one real
`odyssey` console script, is now a real, verified, end-to-end path. 9.9
(ADR for the capture layer) is done too. Step 3 (`data_preparation`, all
seven stages plus recipes) and Step 4's whole `recipe → recipe_hash`,
`curated set → curated_watermark`,
`corpus_version = sha(recipe_hash + curated_watermark)`,
`datasets/registry.yaml` + manifests + cards chain (3.1–3.9, 4.3–4.8) are
now real, reachable via `odyssey data <cmd>` end to end: `collect` →
`normalize` → `clean` → `validate` → `split` → `build-corpus` → `card`,
or the whole thing sequenced by `run_recipe` (3.8). 5.6 (the soup-cli
adapter, `training/`) closes the loop — `odyssey sft`/`odyssey dpo` →
`odyssey train sft-config`/`dpo-config` → `soup train --config soup.yaml`,
verified against the real installed `soup-cli`. 5.7/5.8 (`write_grpo_config`,
`write_experiment_manifest`) are done too, closing Step 5 except 5.9
(checkpoint → object store, no code yet). What's next:

```
9.4 NOTICE copyright holder   ← blocks public release, needs a human
```

0′.1 (OpenAI drop-in) is done too — see Step 0′ below.

Everything else in Step 8 (`odyssey-schemas` + `services/api` + OpenAPI +
`sdk/python`/`sdk/javascript` + `apps/web`) is the next major unbuilt piece,
plus Steps 6–7 (models registry, evaluation harness), still empty. Live gaps
in the Step 1 destination itself: project scoping (multi-tenant auth beyond
one shared key) and object-store backing (`services/collector` still writes
local disk) — see its README's "Not done here".

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
| `integrations/_base.py` | 283 | Request+response → events: prefix dedup, unknown-block handling (Anthropic) |
| `integrations/anthropic.py` | 249 | Drop-in sync/async client, opt-in patch. Provider imported **inside** `__init__` |
| `integrations/_openai_base.py` | 235 | Same job, OpenAI's shape. No separate system-prompt case needed — it's `messages[0]`, covered by the same tail-tracking logic |
| `integrations/openai.py` | 225 | Drop-in sync/async client, opt-in patch. Also covers OpenAI-*compatible* providers via `base_url=...` — same SDK, same wrapper |
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
| `sinks.py` | 142 | `FileSink` (moved out of `cli.py` so the library never imports the CLI) and `HttpSink` — the network destination, stdlib `urllib` only. Any object with `send(journey_id, events, header)` is a sink |
| `export.py` | 374 | The artifact: `Journey` → `{conversation_id}.json`, `--last-step` trimming, `_odyssey` diagnostics, atomic write, filename sanitisation. `_gather_from_dir`/`_gather_from_spool` are shared by `sft.py`/`dpo.py` too |
| `sft.py` | 133 | SFT export — one JSON line per `trainable` step, one combined `.jsonl` shard |
| `dpo.py` | 144 | DPO pair extraction — walks `journey.steps`, pairs every `superseded` run against the `trainable` step that resolved it |

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

**Declared but unused:** `ConversationSummary`. `PiiPolicy`/`RedactionPreview`
(item 2.15) are now wired into `data_preparation`'s `cleaning`/`pii` modules;
`TelemetryEvent` (item 1.11) was removed as dead code — see
[§ Dead code](#dead-code-and-stale-references).

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

Two equivalent front ends. `python -m odyssey.cli` is core's own argparse
parser — standalone, zero deps, works with only `odyssey-core` installed:

```bash
python -m odyssey.cli --spool .odyssey status
python -m odyssey.cli --spool .odyssey push   --out ./out [--journey <id>]
python -m odyssey.cli --spool .odyssey export --out ./artifacts [--journey <id>] [--events ./out] [--last-step]
python -m odyssey.cli --spool .odyssey sft    --out ./train.jsonl [--journey <id>] [--events ./out]
python -m odyssey.cli --spool .odyssey dpo    --out ./prefs.jsonl [--journey <id>] [--events ./out]
python -m odyssey.cli --spool .odyssey show [<journey_id>]
python -m odyssey.cli --spool .odyssey health [--journey <id>] [--json]
```

`odyssey` (installed via `cli/`, ADR 0003) is the same seven subcommands
mounted under `spool`, plus every other member's own group — today just
`data normalize` from `odyssey-dataprep`:

```bash
odyssey spool status --spool .odyssey
odyssey spool push   --spool .odyssey --out ./out [--journey <id>]
odyssey spool export --spool .odyssey --out ./artifacts [--journey <id>] [--events ./out] [--last-step]
odyssey spool sft    --spool .odyssey --out ./train.jsonl [--journey <id>] [--events ./out]
odyssey spool dpo    --spool .odyssey --out ./prefs.jsonl [--journey <id>] [--events ./out]
odyssey spool show   --spool .odyssey [<journey_id>]
odyssey spool health --spool .odyssey [--journey <id>] [--json]
odyssey data normalize --out ./normalized [--raw <dir> --format <fmt> --data-source <name> | --events <dir> | --spool .odyssey]
odyssey doctor        # plugin discovery + cold `--help` timing (budget: 700ms, best-of-3)
```

Each `odyssey spool <cmd>` takes its own `--spool` rather than inheriting a
global one — a deliberate simplification to keep the lazy-plugin boundary
clean (no shared typer `Context` crossing into a member's sub-app). `odyssey
push`/`odyssey status` still work as deprecated top-level aliases for one
minor release, warning to stderr, exactly as ADR 0003 specifies.

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
| `ODYSSEY_TIMEZONE` | IANA name (e.g. `Asia/Kolkata`) for which day a shard rotation belongs to; default `UTC`, unrecognised names fall back to `UTC` |

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

# item 1.11 — dead code, removed rather than wired to a backend that
# doesn't exist (docs/WORKING.md's own "wire it or delete it" call)
grep -rn "TelemetryEvent" src/ tests/   # → none
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

- ~~**`TelemetryEvent`**~~ — removed (item 1.11). Its `to_api_dict()` targeted
  `POST /api/v1/telemetry/events` and a `push_events()` pipeline, neither of
  which exists anywhere in this repo.
- **`ConversationSummary`** — declared, unused.

### Formerly missing from Phase 1 (all three resolved)

- ~~No CI.~~ `ci-core.yml`/`ci-collector.yml`/`ci-dataprep.yml`/`ci-cli.yml`
  now exist, path-filtered per member (item 9.2).
- ~~No ADR for the capture layer.~~ [`adr/0004-capture-layer.md`](adr/0004-capture-layer.md)
  (item 9.9) — including the deliberate exception to the `packages/ = no
  side effects, no framework imports` rule from `STRUCTURE.md`: `init()`
  installs a global singleton, a background thread, and an `atexit` hook.
- ~~`openspec/changes/add-journey-schema/design.md` cited but absent.~~
  Written (item 9.6) — Decisions 1/4/8 reconstructed from what `fold.py`/
  `primitives.py` already establish, Decision 9 (new) defines
  `curated_watermark`, closing item 4.3.
- ~~`NOTICE`/`pyproject.toml` pointed at `src/odyssey/build/*`.~~ Fixed to
  `src/odyssey/builders/`, the directory that exists (item 9.5).
- ~~`.pre-commit-config.yaml`, `CHANGELOG.md`, `SECURITY.md`, `CODEOWNERS`
  missing.~~ All four written (item 9.7).
- ~~Two no-op contract tests.~~ `packages/odyssey-core/docs/README.md`
  written — both `test_docs_reference_only_symbols_that_exist` and
  `test_docs_quickstart_still_works` now check real content (item 9.8).

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
- **`writer_id` detects a conflict, it does not prevent one.** Two processes
  recording one journey still corrupt it; the fold refuses the result rather than
  exporting it. Per-writer sequences would prevent it and cost a
  `SCHEMA_VERSION` major bump — the same kind of major bump item 0′.4 just used
  for `voice` events.
- **A schema-1.x `*.jsonl` shard no longer parses** (0′.4's `SCHEMA_VERSION`
  1.1 → 2.0 major bump). No migration tool ships with this repo; a 1.x file on
  disk is stuck unless something else rewrites it forward.
- **LlamaIndex hooks are not started** (0.10). LangChain, LangGraph (0.10/0′.2),
  and the OTel bridge (0.11/0′.3) are all covered now. **LlamaIndex is
  intentionally deferred, to be picked up together with item 9.4** (`NOTICE`
  copyright holder) in a later pass — not attempted here.
- **The OTel bridge only understands the official `gen_ai.*` semantic
  convention** (0.11/0′.3). OpenInference (Arize/LlamaIndex's own OTel
  integration) and other instrumentation vocabularies use different
  attribute names entirely — an unrecognized span still gets correct
  journey lifecycle, just no turn content. Documented scope cut, not
  silent data loss.
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
docker-compose.yml
scripts/codegen.sh
docs/architecture.md · docs/journey-schema.md
models/registry.yaml
services/api/openapi.json
```

`.pre-commit-config.yaml`, `CHANGELOG.md`, `SECURITY.md`, `.github/CODEOWNERS`
(9.7) and `datasets/registry.yaml` (4.6) are now written — moved off this
list.

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

**A new provider integration.** OpenAI (item 0′.1) is the worked example, now
done — `integrations/openai.py` + `integrations/_openai_base.py`. Three pieces:

1. A parser — `builders/messages.messages_from_openai_chat` already existed.
2. Request/response capture in `_base.py`-shape: track how much of the message
   list has been recorded, record tool schemas only when they change, and
   degrade gracefully on a shape the parser refuses rather than losing the
   turn (`_safe_openai_messages`, since `messages_from_openai_chat` raises by
   design — right for a batch import, wrong on an auto-capture path). OpenAI
   needed no separate system-prompt tracking the way Anthropic's `_base.py`
   does: its system prompt is `messages[0]`, already covered by the same
   "record only the unrecorded tail" logic every other turn uses.
3. A wrapper that *wraps* rather than subclasses, importing the provider inside
   `__init__`. Subclassing would need the import at class-definition time and
   break `dependencies = []`.

`optional-dependencies` (`openai>=1.0`) and a `sys.modules`-injected fake in the
tests, so the core test path needs no real install — plus one live smoke test
against the real `openai` package (not just the fake) to catch response-shape
drift the fake can't. OpenAI-*compatible* providers (Groq, Together, local
vLLM/Ollama) are covered for free: they speak the identical SDK, just with a
different `base_url` — nothing extra to write per provider.

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
item 0′.4 actually took for voice events** (`"voice"`, `VoiceEvent`,
`SCHEMA_VERSION` 1.1 → 2.0) — a worked example of this section now that it's
shipped, not just a hypothetical.

**A new CLI command group.** ADR 0003 is built — `cli/` owns the `odyssey`
console script (item 9.3, done) and dispatches plugins lazily from the
`odyssey.commands` entry-point group; core registers `spool = "odyssey.cli:
register"`, `odyssey-dataprep` registers `data = "odyssey_dataprep.cli:
register"`, and core keeps working standalone as `python -m odyssey.cli`
(verified: it never depended on `[project.scripts]`, only `-m`). A new
member adds a group by declaring one entry point in its own
`pyproject.toml` and a `register(app)` function — no change to `cli/` itself.
`register(app)` does a *local* `import typer` inside the function, never at
module scope: that keeps the registering member's own `dependencies = []`
true, since typer is only ever imported by the process that already depends
on it (`cli/`) calling in. See `odyssey.cli.register` (delegates to the
existing, already-tested `main()` — pure argv-translation plumbing) and
`odyssey_dataprep.cli.register` (calls its module's functions directly,
since that member had no pre-existing CLI to delegate to) for the two
reference shapes.

Two typer 0.27 quirks worth knowing before touching `cli/registry.py`
again: subclass `typer.core.TyperGroup`, not raw `click.Group` — typer no
longer shares one exception hierarchy with the installed `click` package for
its own `ctx.exit()`, so a plain `click.Group` root mixed with
`typer.main.get_command()`-built subcommands breaks nested `--help`. And a
command returned from a custom `get_command` has no name of its own unless
you set `.name` explicitly — typer only assigns it via `add_typer(name=...)`,
which a lazy loader bypasses by construction.

---

## 12. Further reading

- [`STRUCTURE.md`](STRUCTURE.md) — the full planned tree, organiser rules, CLI surface
- [`adr/0001-monorepo-layout.md`](adr/0001-monorepo-layout.md) — why a monorepo
- [`adr/0002-artifacts-out-of-git.md`](adr/0002-artifacts-out-of-git.md) — git holds the recipe and the hash; the store holds the bytes
- [`adr/0003-single-cli-entrypoint.md`](adr/0003-single-cli-entrypoint.md) — one console script, plugin-dispatched
- [`adr/0004-capture-layer.md`](adr/0004-capture-layer.md) — event-sourced core, ambient context, single-writer contract, never-raise boundary
- [`../openspec/changes/add-journey-schema/design.md`](../openspec/changes/add-journey-schema/design.md) — the journey-schema decisions cited elsewhere as "design.md Decision N", plus `curated_watermark`'s definition
- [`../CONTRIBUTING.md`](../CONTRIBUTING.md) — setup, adding a member, tier rules, commit format
- [`../packages/odyssey-core/README.md`](../packages/odyssey-core/README.md) — module table and test map
