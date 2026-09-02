# odyssey — session handoff

## Metrics exposed end to end: services/collector → GET /metrics → apps/web dashboard — done

Built exactly what the previous handoff below designed, verified against
real code before it was written: `MetricsSnapshotOut` DTO
(`packages/odyssey-schemas`), `repositories/filesystem.list_metrics` +
`domain/metrics.py` + `routers/metrics.py` in `services/api` (registered
behind `require_api_key`, same as every other data route), regenerated
`services/api/openapi.json` + both SDKs' new `metrics` resource (their
hand-written `client.py`/`client.ts` needed a manual one-line addition
each — codegen only emits `resources/*.py`/`.ts`, not the top-level
client's resource registration, a gap the original design note didn't
call out), and `apps/web/src/app/(dashboard)/metrics/page.tsx` + a `Nav.tsx`
link, mirroring the `runs`/`exports` pages exactly. TDD'd throughout (new
integration tests for the list route + malformed-line skip, SDK
empty-list tests for both languages) — every test written and watched
failing before the implementing code landed. Also lowered
`ODYSSEY_METRICS_INTERVAL`'s default from 300s to 30s (user request,
mid-session), updated everywhere it was documented.

**Verified end to end for real** (not just unit tests): a real
`services/collector` instance running, a real host snapshot posted to it
via `odyssey.metrics.build_snapshot()` + `HttpTransport`, landed on disk
at `<data-dir>/metrics/<date>.jsonl`; a real `services/api` instance
pointed at that dir returned it from `GET /metrics`; a real `apps/web`
production build + `next start` rendered it in the `/metrics` page's
server-rendered HTML (confirmed hostname/project/disk-usage columns all
present in the actual response bytes, not just that the route existed).
One non-obvious thing hit along the way, worth knowing for next time:
`apps/web`'s list pages are statically prerendered at `next build` time,
not fetched fresh per request under `next start` — if the API isn't
already running when you build, the build bakes in the "fetch failed"
error and a `next start` restart alone won't fix it; you have to rebuild
with the API up.

Full per-member `task test` green across all 8 workspace members after
this, `openapi --check` reports fresh, `codegen-drift` clean.

**Old handoff below, now fully closed — kept for the exact design
rationale in case it's useful again:**

## Product/Project rename, opt-in metrics, services/api auth — done. Metrics-through-services/api + apps/web UI — also now done (see above)

Big session, all pushed to `origin/main`, HEAD is `9a48c25`. Three separate pieces of work, in order:

**1. Product/Project rename + opt-in metrics capture** (full design doc + implementation plan + subagent-driven-development execution — read these two files for the complete rationale, they're still accurate/current):
- `docs/superpowers/specs/2026-09-02-product-project-metrics-design.md`
- `docs/superpowers/plans/2026-09-02-product-project-metrics.md`

Shipped: `services/collector`'s `Project` auth concept renamed to `Product` (clean break, `--products-file`/`ODYSSEY_COLLECTOR_PRODUCTS_FILE`, `GET /products`); new `packages/odyssey-core/src/odyssey/project.py` (`odyssey.init(project=...)`, auto-detected from git remote/dirname, tags `journey_metadata["project"]`); new `packages/odyssey-core/src/odyssey/metrics.py` + `services/collector`'s `POST /metrics` (opt-in host telemetry — hostname/OS/CPU/disk, off by default via `ODYSSEY_COLLECT_METRICS`, `public_ip` recorded server-side from the real TCP peer address); `HttpTransport` base class extracted from `HttpSink` so the metrics poster reuses it. A final whole-branch review caught and fixed a real bug: `services/api`'s journey listing was scanning the new `metrics/` directory as if it were journey data (`services/api/src/odyssey_api/domain/journeys.py`'s `list_journeys_with_status` now delegates to the already-guarded `filesystem.list_journeys` instead of its own unguarded directory walk — this is the precedent to follow for the metrics-UI work below).

**2. `apps/web` added to the production runbook** — `docs/runbooks/run-services.md` previously only covered `services/api`/`services/collector`; now has a verified `pnpm build` + `next start` section + systemd unit for `apps/web` too.

**3. `services/api` gained optional bearer-token auth** (it had none before this session). `ODYSSEY_API_AUTH_KEY` env var / `--api-key` flag on `odyssey api serve`, `Authorization: Bearer <key>` required on every route except `GET /health`, open by default when unset. Deliberately not named `ODYSSEY_API_KEY` — that's a different, pre-existing client-side setting (`packages/odyssey-core`'s `HttpSink`). Implemented with `fastapi.security.HTTPBearer` + `secrets.compare_digest` (a security review caught that the first pass used a raw `Header()` param, which polluted the OpenAPI schema and left `services/api/openapi.json` stale against CI's drift gate — fixed). Both SDKs and `apps/web` wired up with a matching env var fallback; confirmed the key can never reach `apps/web`'s browser bundle (no `NEXT_PUBLIC_` prefix, and the app has zero client components anyway).

### Next up — designed, not started: expose `POST /metrics` data through `services/api` + a dashboard page

The user asked for this and I'd fully designed it (verified against real code) when the session ended for a handoff instead. Nothing has been written yet. The design, so the next session doesn't have to re-derive it:

**Why this doesn't already exist:** the original Product/Project/metrics design spec explicitly scoped `services/api` changes out ("no route reads `/metrics` data"). The user now wants that closed.

**Backend (do this first — everything else depends on it):**
- `packages/odyssey-schemas/src/odyssey_schemas/__init__.py` — new `MetricsSnapshotOut(BaseModel)`: `ts: str`, `hostname: str`, `os: str`, `cpu_count: Optional[int] = None`, `memory_total_bytes: Optional[int] = None`, `memory_available_bytes: Optional[int] = None`, `disk_total_bytes: Optional[int] = None`, `disk_free_bytes: Optional[int] = None`, `project: Optional[str] = None`, `public_ip: Optional[str] = None` — matches `packages/odyssey-core/src/odyssey/metrics.py`'s `build_snapshot()` output shape plus the `public_ip` field `services/collector`'s `_do_metrics_post` adds server-side. Add to `__all__`.
- `services/api/src/odyssey_api/repositories/filesystem.py` — new `list_metrics(journeys_dir: Path) -> List[Dict[str, Any]]`: reads `<journeys_dir>/metrics/*.jsonl` (mirrors `services/collector`'s single-key/open-mode storage path exactly), parses each line as JSON, skips malformed lines rather than raising (same defensiveness as `list_journeys_with_status`'s per-shard fold failure handling), sorts by `ts` descending. **Deliberately only the flat, non-product-scoped layout** — same documented scope cut `list_journeys`/`find_journey_path` already have (see their docstrings), don't try to add product-awareness in this pass.
- `services/api/src/odyssey_api/domain/metrics.py` — thin `list_metrics(journeys_dir) -> ...` passthrough to the repository, matching `domain/exports.py`'s exact shape (the simplest existing precedent — a directory listing, no registry, no folding).
- `services/api/src/odyssey_api/routers/metrics.py` — `GET /metrics` → `List[MetricsSnapshotOut]`, matching `routers/exports.py`'s exact shape.
- `services/api/src/odyssey_api/main.py` — register the new router with `dependencies=[Depends(require_api_key)]` (protected, same as every other data route).
- Tests: mirror `services/api/tests/integration/test_api.py`'s existing exports/journeys test patterns — write a real `metrics/<date>.jsonl` file via the `_client(settings)` fixture, hit `GET /metrics`, assert the response.
- **Regenerate after the schema/route lands**: `./scripts/codegen.sh` from repo root (regenerates `services/api/openapi.json`, then both SDKs — verify `git diff --stat` afterward, the metrics DTO/route should produce a new `MetricsResource`/`metrics.py` resource file in both `sdk/python/src/odyssey_sdk/resources/` and `sdk/javascript/src/resources/`, with zero other resource files changing).

**Frontend, after the backend + codegen lands (needs the generated SDK types):**
- `apps/web/src/app/(dashboard)/metrics/page.tsx` — new page, copy `runs/page.tsx`'s exact shape (`apiClient().metrics.list()` in a try/catch, `<DataTable>` with columns for `ts`/`hostname`/`os`/`cpu_count`/disk usage/`project`/`public_ip` — check `apps/web/src/components/DataTable.tsx` for the `Column<T>` interface first).
- `apps/web/src/components/Nav.tsx` — add `{ href: "/metrics", label: "Metrics" }` to the `LINKS` array.
- Verify end to end: real `services/collector` with `ODYSSEY_COLLECT_METRICS=1` on a test SDK process, real `services/api`, real `pnpm dev`, curl/browser-check the `/metrics` page actually shows a posted snapshot.

**Docs to update after this lands:** `docs/COMPONENTS.md` (mention the new route/page), `docs/data-contracts.md` (the metrics DTO joins the generated-client chain), `services/api/README.md` (new route), `apps/web/README.md` (new page), `CHANGELOG.md`.

## Documentation pass, continued — 4 new top-level docs + runnable SDK examples

Still no engineering, docs only. Added `sdk/examples/{python,javascript}`
— `basic_usage.py` / `basic-usage.mjs`, both actually run against a real
`services/api` instance (verified locally, not just written). Added the
four docs `docs/STRUCTURE.md`'s original plan named but nobody had
written yet: `architecture.md` (system-level: the two pipelines +
8 design principles), `journey-schema.md` (the `JourneyEvent` wire format
field by field, cross-checked against `primitives.py`/`fold.py`),
`data-contracts.md` (the codegen/drift-check chain + both SDK generators'
shared narrowness rules), `model-lifecycle.md` (corpus → training →
registry → eval as a sequence of real commands, cross-checked against
`training/src/odyssey_training/{cli,model_cli}.py`). Deliberately did
**not** write `docs/runbooks/*` or `sdk/docs/*` — no real deployment
target exists for the former, and the latter's topics (pagination, SDK
auth, a versioning policy) don't apply to anything built yet; both stay
empty on purpose, same explicit-deferral pattern as everything else in
this repo. Fixed a stale tracking list in `docs/WORKING.md` ("files
referenced by docs but not written") that still listed several files
that had shipped since it was written.

**Next up:** still **9.4** — `NOTICE` copyright holder, unchanged.

---

## Documentation pass — `docs/COMPONENTS.md` added, stale READMEs fixed

No engineering this session, docs only. Added `docs/COMPONENTS.md` — one
page per app/service/package (what it does, real CLI/API surface verified
against `pyproject.toml` entry points and `cli.py` registrations, how to
run it, deliberate scope cuts). Root `README.md` gained a "Run the whole
stack" section and a full "Documentation" index (every doc in the repo,
topic-grouped, priority-ordered within each group). Fixed the Phases
checklist and layout table (both still said Steps 3-6 unbuilt/scaffold
despite `docs/WORKING.md` having been fully ✅ there for a while) and six
member READMEs (`cli`, `data_preparation`, `evaluation`,
`packages/odyssey-schemas`, `sdk/python`, `services/collector`) that still
described an earlier, unbuilt state. Also fixed a real CI bug found along
the way: `ci-web.yml` ran apps/web's tests before building `@odyssey/sdk`,
which apps/web imports via `dist/` — added a build step ahead of
lint/test/build, reproduced locally first.

**Next up:** still **9.4** — `NOTICE` copyright holder, unchanged by this
session. No other open engineering items anywhere in `docs/WORKING.md`.

---

## Step 8 is fully closed — item 8.5 (`sdk/javascript`) built this session

Built `sdk/javascript` (`@odyssey/sdk`), the last open Step 8 item —
mirrors `sdk/python` 1:1 (`client.ts`/`errors.ts`/`codegen.ts`
hand-written, `types.generated.ts` + `resources/*.ts` generated from
`services/api/openapi.json`, `tsup` build to ESM+CJS+`.d.ts`). Converted
the whole JS side of the repo to a single root `pnpm-workspace.yaml` +
`pnpm-lock.yaml` (pnpm is available via corepack; `apps/web`'s prior
npm/`package-lock.json` setup is gone). Then rewired `apps/web` onto
`@odyssey/sdk`: deleted `src/lib/api/{types,client}.ts`, every page now
imports `@odyssey/sdk` types + a one-function `apiClient()` wrapper.
`ci-web.yml`, `ci-sdk.yml` (both matrix legs now), and
`codegen-drift.yml` updated accordingly. Verified end to end the same
way 8.6 was: real `uvicorn`-served `services/api` + `pnpm dev` + `curl`
against every route including the 404 path, resolved SSR HTML inspected
for real API data. **Step 8 (`api → sdk → web`) has no open items.**

**Next up:** **9.4** — `NOTICE` copyright holder. The only remaining
hard blocker for public distribution; needs a human, not more
engineering.

---

## Step 8 items 8.1-8.4, 8.6, 8.7 are now closed

Built this session, on top of 8.1-8.3 below: `sdk/python`
(`odyssey-sdk`) — hand-written `client.py`/`errors.py`/`models.py`/
`codegen.py`, generated `resources/{journeys,datasets,models,runs,
exports}.py` from `services/api/openapi.json`, stdlib-`urllib`-only
transport (no runtime dependency on `odyssey-core`/`odyssey-api`). New
`odyssey sdk codegen`/`check-drift` CLI commands, `odyssey api openapi
--check`, `scripts/codegen.sh`, `codegen-drift.yml`/`ci-sdk.yml` CI
(item 8.7). Then `apps/web` — a real Next.js 16 dashboard
(`journeys/datasets/models/runs/exports`), server-rendered against
`services/api`, verified via a live `uvicorn` instance + `curl` (no
browser tool in this environment). The `odyssey-sdk` naming-collision
flag is resolved by documentation, not a rename. `apps/web` uses its own
temporary `src/lib/api/` client instead of `@odyssey/sdk` since
`sdk/javascript` (8.5) wasn't in this session's scope — replace it the
same commit 8.5 lands.

**Only 8.5 (`sdk/javascript`, `@odyssey/sdk`) remains open in Step 8.**

**Next up, in dependency order:**

1. **9.4** — `NOTICE` copyright holder. Still the only remaining hard
   blocker for public distribution, still needs a human, not more
   engineering. LlamaIndex hooks (0.10) are still bundled with this item.
2. **8.5** — `sdk/javascript` (`@odyssey/sdk`, pnpm member per
   `docs/STRUCTURE.md` — this session used plain npm for `apps/web` since
   pnpm isn't installed in this environment; confirm before assuming pnpm
   for 8.5 too). Once built, `apps/web/src/lib/api/*` should be replaced
   by `@odyssey/sdk` imports, not kept alongside it.

---

## Step 8 items 8.1-8.3 (services/api) are now closed

Built this session: two new workspace members, `packages/odyssey-schemas`
(pure pydantic DTOs, no `fastapi`/`odyssey-core` dependency) and
`services/api` (`odyssey-api`, FastAPI), layered `routers/` ->
`domain/` (zero fastapi imports) -> `repositories/filesystem.py`. Routes:
`/health`, `/journeys`+`/journeys/{id}`, `/datasets`+`/datasets/{name}`,
`/models`+`/models/{name}`, `/runs`, `/exports`. New `odyssey api
serve/openapi/routes` CLI commands; `services/api/openapi.json` generated
and committed. 25 new tests, full workspace (919 tests, 8 members) green.

**Explicitly not merged with `services/collector`** (the "8.2 and 1.8 are
the same server" note `docs/WORKING.md` already carried) — `services/api`
is a pure read layer over the files the collector already writes;
merging ingest into FastAPI would mean rewriting the collector's
idempotency/project-scoping/backoff handling for no functional gain today.
Also deliberately not built this session, same explicit-deferral pattern
`judges.py` got: `repositories/mongo.py`/`postgres.py`/`objectstore.py`,
`workers/drain_consumer.py` (no Kafka anywhere in this repo),
`migrations/` (alembic — no relational schema). Each has its own README
documenting the deferral.

**Next up, in dependency order:**

1. **9.4** — `NOTICE` copyright holder. Still the only remaining hard
   blocker for public distribution, still needs a human, not more
   engineering. LlamaIndex hooks (0.10) are still bundled with this item.
2. **8.4-8.7** — `sdk/python` (generated OpenAPI client — settle the
   `odyssey-sdk` naming collision with the capture-layer "SDK" first, see
   `docs/WORKING.md`'s ⚠️ note), `sdk/javascript`, `apps/web`,
   `scripts/codegen.sh` + CI drift gate. Bigger scope, and the naming
   collision needs resolving before 8.4 specifically.

---

## Step 7 (evaluation harness) is now closed

Built this session per the plan below (kept for reference — every item it
names is now done, except the one it explicitly recommended deferring).
`evaluation/` is a real uv workspace member (`odyssey-eval`), `odyssey eval
run/compare/build-set/card/check-overlap` mounted, 21 new tests passing,
full workspace (894 tests, 6 members) still green. `judges.py` was
deliberately **not built** — documented deferral, see `evaluation/src/
odyssey_eval/harness.py`'s module docstring.

**Next up, in dependency order:**

1. **9.4** — `NOTICE` copyright holder. Still the only remaining hard
   blocker for public distribution, still needs a human, not more
   engineering. LlamaIndex hooks (0.10) are still bundled with this item.
2. **`services/api`** (8.1–8.3) — the next unbuilt major piece per
   `docs/STRUCTURE.md`, now that Steps 0–7 are all closed. `odyssey-schemas`
   + FastAPI + OpenAPI is the described shape; nothing started here yet.

---

## Step 7 — evaluation harness: the plan this session executed (historical)

Interrupted before any code was written — this is a plan for the next session,
not a status report. Steps 0–6 are now fully closed (see the 2026-08-28
session summary below); Step 7 (`evaluation/`) is the next real gap. Do not
start Step 8 (`services/api`) first — `docs/NEXT.md`'s own dependency-order
note above previously said `services/api` was next, but the user redirected
to Step 7 mid-session; re-confirm with the user if priorities have shifted
again by the time this is picked up.

### What already exists

- `evaluation/` is scaffolded but empty — every subdirectory is a bare
  `.gitkeep`: `src/odyssey_eval/`, `tests/`, `benchmarks/`, `metrics/`,
  `datasets/`, `reports/templates/`. No `pyproject.toml` yet, so it is not a
  uv workspace member (root `pyproject.toml`'s `[tool.uv.workspace] members`
  list explicitly excludes it — the root file's own comment names
  `evaluation · sdk/python` as "add the same commit as its pyproject.toml").
- **Journey-level metrics are already computed** — `JourneyMetrics`
  (`aggregated_reward`, `num_tool_calls`, `num_tool_failures`,
  `tool_error_rate`, `num_tool_response_none`) and `ExecutionMetrics`
  (`total_time`) in `packages/odyssey-core/src/odyssey/primitives.py`,
  populated by `fold.py` from real signals/usage. This is explicitly called
  out in `docs/WORKING.md` as "a head start on 7.3" — reuse, don't
  reimplement.
- `data_preparation/src/odyssey_dataprep/validation/__init__.py`'s
  `check_leakage(splits: Dict[str, List[str]]) -> List[str]` is generic (just
  named lists of ids, "any id in more than one split is a leak") — directly
  reusable for 7.4's no-overlap gate: build `{"eval": [...eval journey
  ids...], "train": [...training corpus journey ids...]}` and call it as-is,
  no new leakage logic needed.
- `data_preparation/src/odyssey_dataprep/datasets.py`'s
  `next_version`/`build_manifest`/`write_manifest`/`update_registry`/
  `write_card` is the exact shape `evaluation/datasets/`'s "TRACKED
  manifests + cards only; frozen eval sets, never trained on" (per
  `docs/STRUCTURE.md`) needs — mirror this module for eval sets rather than
  inventing a new manifest schema. An eval set doesn't have a
  `recipe_hash`/`curated_watermark` in the training-corpus sense, so the
  mirrored version should drop those fields, not force them.
- `docs/STRUCTURE.md` (search `evaluation/`) is the authoritative shape:
  ```
  evaluation/                       ← uv member. name "odyssey-eval", pkg odyssey_eval
    pyproject.toml · src/odyssey_eval/{runner,judges,harness}.py
    datasets/     TRACKED manifests + cards only; frozen eval sets, never trained on
    benchmarks/   TRACKED suite defs yaml + task prompts
    metrics/      TRACKED metric implementations (code, not numbers)
    reports/      **ignored** generated html/json (.gitkeep); templates/ tracked
  ```
  CLI surface (STRUCTURE.md's "Command surface" section): `eval run · compare
  · report`.

### Open design decision to resolve first — what does the harness *run against*?

There is no live model-serving path in this repo (`services/api` is Step 8,
not built) and `training/` never runs inference itself (`soup-cli` is a
config writer, item 5.6). Two options, and the offline one fits this repo's
existing "real but narrow, no speculative heavy dependency" discipline far
better:

1. **Offline scoring (recommended)** — the harness takes a benchmark suite
   (prompts + reference/rubric, `evaluation/benchmarks/*.yaml`) and a
   caller-produced completions file (`{"id": ..., "response": ...}` JSONL,
   generated however the caller likes — a `soup-cli`-trained model run
   through any inference tool, a raw API call, whatever), and scores the
   pairing. No live API calls from this repo, no new heavy dependency,
   deterministic and unit-testable — the same shape `odyssey sft`/`odyssey
   dpo` already have (operate on an already-produced shard, not a live
   process).
2. **Live provider calls** — the harness itself calls an Anthropic/OpenAI/
   Gemini client to generate responses. Heavier (needs real API keys in CI,
   network flakiness, cost), and this repo already has drop-in provider
   wrappers (item 0.9) a harness could reuse for this *if* asked — but
   nothing today names a concrete need for it. Do not build this speculatively;
   if the user wants it, confirm first.

`judges.py` (LLM-as-judge scoring, named in STRUCTURE.md's own file list) is
a strong candidate for the **same explicit deferral treatment** items 0.11
(OTel bridge) and 3.5 (LLM augmentation) already got: it needs a real LLM
dependency in the loop with no concrete consumer named yet. Recommend
building `runner.py`/`harness.py`/deterministic `metrics/` first, and
documenting `judges.py` as scoped-out (not silently dropped) until someone
names a real use.

### Suggested phased plan

1. **Scaffold the workspace member** — `evaluation/pyproject.toml` (name
   `odyssey-eval`, pkg `odyssey_eval`, `dependencies = ["odyssey"]` — no
   torch/transformers, this is scoring code not training code), `dev` extras
   matching every other member's exact pin set (see `training/pyproject.toml`
   for the reference list: pytest/black/isort/flake8/pyrefly). Add
   `evaluation` to root `pyproject.toml`'s `[tool.uv.workspace] members`.
   Copy `training/Taskfile.yml` verbatim (task fmt/lint/types/test/check is
   identical across every member). New `.github/workflows/ci-eval.yml`
   mirroring `ci-training.yml` exactly (path-filtered on `evaluation/**` +
   `packages/odyssey-core/**`).
2. **7.2 frozen eval sets** — mirror `datasets.py` into
   `odyssey_eval/eval_datasets.py` (or similar name — avoid colliding with
   the stdlib-shaped `datasets` name odyssey_dataprep already owns):
   manifest + registry + card, no `recipe_hash`/`curated_watermark`. "Frozen"
   is a property enforced by 7.4's gate (never appearing in a training
   split), not by any write-protection in this module itself.
3. **7.3 benchmarks + metric code** — `evaluation/benchmarks/*.yaml` (suite
   defs: task prompts + reference answers or a rubric — decide the schema
   against a real example benchmark, not speculatively). `metrics/` starts
   with deterministic metrics only (exact-match, tool-call accuracy reusing
   `JourneyMetrics`' existing fields when scoring captured journeys rather
   than raw text) — no LLM-judge yet, see the deferral note above.
   `src/odyssey_eval/harness.py`/`runner.py` orchestrate: load benchmark +
   completions + metrics, write a report.
4. **7.1 wire the CLI** — `odyssey eval run/compare/report`, new
   `[project.entry-points."odyssey.commands"] eval = "odyssey_eval.cli:register"`
   entry, following the exact `register(app)` pattern every other member
   uses (`training/src/odyssey_training/cli.py` is the cleanest reference).
5. **7.5 reports** — `evaluation/reports/templates/` tracked (a real
   template, not a placeholder), `evaluation/reports/` itself gitignored
   with `.gitkeep`, mirroring ADR 0002's treatment of `training/checkpoints/`
   etc.
6. **7.4 no-overlap gate** — build id-list overlap check reusing
   `check_leakage` as described above, wire into a new `dataset-audit.yml`
   CI workflow (STRUCTURE.md names this file explicitly) that exits non-zero
   on a breach — match the exit-code-3 convention `odyssey data validate`
   already established for lineage/contract violations, so CI can grep for
   `3` specifically per STRUCTURE.md's own "Exit codes" rule.

### Verification checklist (once built)

- `task check` green in `evaluation/` (new member) and no regression in the
  other 5 (`packages/odyssey-core`, `services/collector`, `data_preparation`,
  `cli`, `training`).
- `uv sync` at the repo root picks up the new member cleanly (workspace
  members list + lockfile).
- Real CLI smoke test: `uv run odyssey eval --help` and at least one real
  `odyssey eval run` against a hand-built tiny benchmark + completions file,
  not just unit tests — this repo's own convention (see how 5.9/6.1/6.4 were
  each smoke-tested against a real filesystem path/registry before being
  called done).
- `docs/WORKING.md` Step 7 table, `docs/NEXT.md`, `CHANGELOG.md` updated with
  real technical rationale per item, matching the level of detail already
  set for Steps 5/6 in this file.

---

## Session summary (2026-08-28)

Closed all 8 of the "smaller, still open" items the 2026-08-27 handoff listed,
plus 5.7/5.8 (`configs/{sft,dpo,grpo}` and `experiments/<exp_id>.yaml`, done
earlier the same session). One of the 8 was a deliberate breaking change,
approved explicitly rather than deferred:

- **0′.4 voice events — real `SCHEMA_VERSION` major bump, 1.1 → 2.0.** A new
  `"voice"` `EventKind`, `VoiceEvent` dataclass, `JourneyEvent.voice`,
  `FoldResult.voice_events` (kept out of the SFT/DPO export path — no
  `trainable` notion). `integrations/livekit.py` now emits real
  `stt_transcript`/`barge_in` events from data it already computed. Golden
  fixture regenerated at 2.0 with a `voice` event; `test_contract.py` and
  `test_livekit.py` updated for the new event mixed into the seq space. **No
  migration tool** — a schema-1.x shard on disk no longer parses; this is the
  documented, one-way consequence, not a bug.
- **0.10 LangChain** — `integrations/langchain.py`, `OdysseyCallbackHandler()`
  factory (lazy `langchain_core` import, `odyssey[langchain]` extra). One flat
  journey per top-level `run_id`, following `livekit.py`'s explicit-context
  pattern rather than `_base.py`'s wrapped-client one, since LangChain's
  callback API is `run_id`/`parent_run_id`-tree-shaped. **0.11 OTel bridge
  deferred, documented only** — no concrete consuming backend named.
- **0′.6 sampling** — `ODYSSEY_SAMPLE_RATE`/`Config.sample_rate`, one coin-flip
  per journey at open (inherited by nested joins via `JourneyContext.state`),
  dropped before the spool is touched.
- **1.7 wire batching/compression/backpressure** — `HttpSink` gzips by
  default; a 429's `Retry-After` sets a client-side backoff window. Collector
  decompresses. Cross-journey batching explicitly not attempted (documented
  scope cut — would need a `drain()` redesign).
- **1.10 object-store landing** — `collect_from_object_store()` (S3-compatible
  via `boto3`, `odyssey-dataprep[s3]` extra), wired into `odyssey data collect
  --bucket`.
- **1.12/2.14 retention/TTL** — `spool.gc()` + `odyssey spool prune` CLI;
  collector's `prune.py` + `python -m odyssey_collector.prune`. Both
  operator-invoked, no auto-GC timer.
- **2.15 content-level PII scrub** — `odyssey.pii` (regex `scan_pii`/
  `redact_pii`, Luhn-checked credit cards), wired into `data_preparation`'s
  `clean_dir`/`validate_dir` as opt-in (`--pii-rules`).
- **9.10 pyrefly** — the one real error (`livekit.py`, a `getattr`+`callable`
  narrowing gap) fixed with a documented suppression comment. The 157
  `tests/` narrowing gaps are unchanged — a scope decision, not attempted.

Full workspace `task test` green after the schema bump (737 tests across
core/collector/dataprep/cli/training). See `docs/WORKING.md` for the
per-item table updates.

## Session summary (2026-08-27)

Started from a codebase where recording worked locally but nothing shipped
data anywhere, no training file existed, and half the planned workspace
members were still `.gitkeep`s. Ended with a real, tested, end-to-end path:
an agent call gets recorded → shipped over the network → landed on a durable
server, date-partitioned → turned into an actual SFT or DPO training file —
all reachable through one real `odyssey` console script. Nine feature/infra
commits plus three docs-only commits, all pushed to `main`. Full detail per
item is in the checklist below; this is the two-minute version:

- **CI** on all four workspace members (`ci-core`/`ci-collector`/
  `ci-dataprep`/`ci-cli.yml`) — 468 tests that enforced nothing now do.
- **`HttpSink`** (stdlib `urllib`, no new dependency) + **`services/collector`**
  (new member, stdlib `http.server`) — the local-only spool now has a real
  network destination, verified with a live SDK → spool → HTTP → collector →
  readable-file round trip, not just unit tests.
- **`odyssey sft` / `odyssey dpo`** — the actual training-file writers. The
  DPO pair extractor's first design (prefix-hash grouping) was wrong and the
  test suite caught it; the fix (walk `journey.steps` in order) is documented
  in the module itself so it isn't silently reintroduced.
- **Date-partitioned storage**, timezone-configurable (`ODYSSEY_TIMEZONE` /
  `ODYSSEY_COLLECTOR_TIMEZONE`, UTC default) — both the local spool and the
  collector, so neither a shard nor a stored file grows unbounded forever.
- **`data_preparation/normalization`** — first real code in that member. Found
  and fixed a genuine bug while building it (BYOD-parsed journeys had every
  message stuck `not_trainable`, including assistant replies) by inspecting a
  live smoke test's output before writing the regression test for it.
- **OpenAI drop-in client** (+ every OpenAI-*compatible* provider for free —
  same SDK, different `base_url`) — verified against the real installed
  `openai` package, not just a fake.
- **`cli/`**, the real `odyssey` console script (ADR 0003) — lazy plugin
  discovery via entry points, verified `odyssey --help` never imports a
  member's own code. Hit and documented two real, non-obvious typer 0.27
  bugs along the way (see `cli/src/odyssey_cli/registry.py`'s docstring).
- **ADR 0004** (the capture layer's design, never written down before) and
  **`openspec/changes/add-journey-schema/design.md`** (cited by code since
  the original port, never actually committed until now) — including a real
  definition for `curated_watermark`, the one piece of the corpus-versioning
  formula that had never been specified anywhere.

**What's still open, deliberately not started:** `NOTICE`'s copyright-holder
question (item 9.4) — researched this session (PyPI metadata is genuinely
empty; there's a lead, parked, not written down here — see git history
around the "picked up later" commit if you need it) but it needs a human to
actually reach out, not more engineering.

## Start here next session

Since the summary above was written, `data_preparation`'s entire Step 3
(all seven stages) plus Step 4's `datasets/` layer shipped:

- **3.9 → 4.4 → 4.5** — `recipes/` (`Recipe`/`recipe_hash`) and
  `versioning.py` (`compute_curated_watermark`/`corpus_version`), via
  `odyssey data recipe-hash` / `odyssey data corpus-version`.
- **4.6 → 4.7 → 4.8** — `datasets.py` (manifest/registry/card writers), via
  `odyssey data build-corpus` / `odyssey data card`. `next_version` doubles
  as `curated_watermark.seq` — one counter, not two to keep in sync.
- **3.1 collection** — `collect_from_spool`/`collect_from_collector`
  reassemble rotated or date-partitioned shards into one flat `*.jsonl` per
  journey (grouped by each event's own `journey_id`, not filename — the
  collector's filename stem is not guaranteed reversible).
- **3.2 cleaning** — dedupe by `content_hash`, dead-turn drop (splices the
  dropped delta out of every later step's cumulative history — a naive
  per-message filter would have corrupted the prefix invariant), NFC +
  control-char encoding repair. Content-level PII scrub intentionally not
  here — still needs item 2.15, which doesn't exist.
- **3.4 annotation** — a local-JSONL human-review queue (`build_queue`) and
  decision-apply (`apply_reviews`, reuses `build_reward_from_scalar`).
- **3.5 augmentation** — `perturb_tool_calls`, deterministic synthetic
  negatives via a dropped required argument. Paraphrase/general synthetic
  negatives need an LLM and are deliberately not implemented — flagged 🟡,
  not faked.
- **3.6 validation** — schema/PII-redaction/leakage/drift checks; `odyssey
  data validate` exits 3 on breach (ADR 0003's lineage-violation code).
  PII check reuses `odyssey.spool._is_secret`'s exact matching rule.
- **3.7 splitting** — groups by `trace_id` (session), assigns via a
  deterministic hash, never `random`. Ships the test 3.7 explicitly
  demanded: same group key never lands in two splits.
- **3.8 flows** — `run_recipe`, a stdlib sequencer (deliberately not
  Prefect — no scheduling/retry/UI need to justify the dependency) chaining
  `collection → normalization → cleaning → validation → splitting`.
  `annotation`/`augmentation` don't fit the uniform dir-in/dir-out contract
  and are called directly, not auto-sequenced.
- **9.5 / 9.7 / 9.8** — stale `src/odyssey/build/` path fixed;
  `.pre-commit-config.yaml`/`CHANGELOG.md`/`SECURITY.md`/`CODEOWNERS`
  written; `packages/odyssey-core/docs/README.md` written, closing both
  contract tests that were previously no-ops.
- **9.10 re-verified, not fixed** — opting `tests/` into pyrefly surfaces
  158 errors today (was 77, stale), one real, 157 narrowing gaps in test
  files. `task types`/CI unaffected either way. Left open — a scope
  decision (~150+ touch points), not a bug fix.

70 tests added this session across `data_preparation` (7 → 77) — every
new stage verified with a real end-to-end `odyssey data <cmd>` run, not
just unit-tested: `collect → normalize → clean → validate → split` and
`queue → apply-reviews`, `augment`, all exercised against real files on
disk with real exit codes checked (including `validate`'s exit 3).

**Update — 5.6 (soup-cli adapter) shipped since the above was written:**

New `training/` workspace member (`odyssey-training`), `soup-cli>=0.73,<1`
as a real dependency (light install — no `[train]` extra, no torch in this
member). Researched Unsloth first (it isn't a separate thing to integrate —
it's one of soup-cli's own `backend` choices, alongside `transformers`/
`mlx`, nothing extra to build), then verified the real, installed soup-cli
0.73.3 source (`soup_cli/config/schema.py`, `soup_cli/data/formats.py`)
rather than trusting scraped docs, which turned out to matter: the docs
implied DPO wants flat strings; the actual code (`_convert_dpo`) accepts
conversational message lists straight through to `trl.DPOTrainer`.

- `training/src/odyssey_training/soup_adapter.py` — `write_sft_config`
  (`odyssey sft`'s `{"messages": [...]}` is already soup-cli's `chatml`
  format verbatim, zero translation), `translate_dpo_shard` +
  `write_dpo_config` (`odyssey dpo`'s `chosen`/`rejected` are a single
  message; soup-cli/TRL want a message *list* — the one real gap, now
  closed). Every config is validated against the real, installed
  `soup_cli.config.schema.SoupConfig` before being written.
- `odyssey train sft-config` / `odyssey train dpo-config`, verified
  end-to-end against a real spool → `odyssey sft`/`odyssey dpo` → generated
  `soup.yaml`, then round-tripped through soup-cli's own real
  `load_config()` and `soup_cli.data.formats._convert_chatml`/`_convert_dpo`
  — not just our own schema import.
- 7 new tests in `training/tests/`. `ci-training.yml` added; `ci-cli.yml`
  now also triggers on `training/**` (a new dependency there changes what
  `odyssey doctor` discovers and how long cold `--help` takes).
- **Side effect worth watching**: cold `--help` went from 172ms to 346ms
  once soup-cli's own dependencies (httpx, pydantic, huggingface-hub) landed
  in the shared venv. Still comfortably under the 400ms budget (see 9.3),
  but the margin is real now, not enormous — a good reason to be
  deliberate about which future members get a comparably heavy light-install
  dependency.

**What's next, in dependency order:**

1. **9.4** — `NOTICE` copyright holder. The only remaining hard blocker
   (public distribution), needs a human, not more engineering. **LlamaIndex
   hooks (0.10) are intentionally bundled with this item** — picked up
   together in a later pass, not attempted now.
2. **`services/api`** (8.1–8.3) — bigger scope (`odyssey-schemas` +
   FastAPI + OpenAPI), the next major unbuilt piece per `docs/STRUCTURE.md`.
   Step 5 is now fully closed (5.9 — checkpoint → object store — done later
   the same session).

**Smaller, closed this session (2026-08-28):** 0.10 (LangChain), 0′.4 (voice
events — breaking `SCHEMA_VERSION` 1.1 → 2.0), 0′.6 (sampling), 1.7
(batching/compression/backpressure), 1.10 (object-store landing), 1.12/2.14
(retention/TTL), 2.15 (content-level PII scrub), 9.10 (the one real pyrefly
error, suppressed).

**Also closed later the same session:** 0′.5 (async streaming for Anthropic),
1.9 (server-side idempotency in `services/collector`), 1.11 (removed dead
`TelemetryEvent` code), 0.9 (Gemini drop-in client), 0′.2 (LangGraph
compatibility — no additional code, only verification it dispatches the same
callback tree LangChain already does), 1.6 (project scoping — a `projects`
roster of `{slug, name, api_key}` in `services/collector`, structural storage
isolation per project, `GET /projects`), 0.11/0′.3 (OTel bridge —
`integrations/otel.py`'s `OdysseySpanProcessor()`, one journey per trace,
`gen_ai.*` content only — a documented scope cut against other
instrumentation vocabularies, not silent data loss), 1.7 (connection reuse
across `send()`/`send_batch()` calls via HTTP/1.1 keep-alive, **plus**
cross-journey *payload* batching itself — `HttpSink.send_batch()` /
`POST /batch/events`, opt-in via `batch_size=N`, every journey's outcome
independent so the earlier "needs a redesign of `drain()`'s per-journey
semantics" concern is resolved rather than avoided), 3.5
(`paraphrase_journey`/`generate_synthetic_negative`, optional
`odyssey-dataprep[llm]` extra, both opt-in and off by default — the
synthetic-negative chain's `superseded`-then-`trainable` step order was
verified against `odyssey.dpo.dpo_pairs`'s real ordering rule), 9.10
(`tests/` now permanently in `pyrefly`'s `project-includes`; 200 errors
surfaced, not the ~157 estimated — 4 were real `src/` type bugs, fixed
properly; the other 196 were narrowing gaps, fixed per call site), 1.7
(cross-journey payload batching — `HttpSink.send_batch()` /
`POST /batch/events`), 5.9 (checkpoint → object store —
`odyssey_training.checkpoints.upload_checkpoint`, S3-compatible via
`boto3`, same lazy-import/`client=` injection seam as 1.10's
`collect_from_object_store`; `odyssey train upload-checkpoint` prints the
`checkpoint_uri`/`checkpoint_sha256` pair `odyssey train
record-experiment` now accepts, closing Step 5 entirely), 6.1 (models
registry — `odyssey_training.models_registry.register_model()`, `name ->
version -> sha256 -> URI -> base model -> corpus version` per
`docs/STRUCTURE.md`'s own schema, idempotent on `(name, version)`
mirroring `datasets.update_registry`'s replace-in-place rule; new
top-level `odyssey model register` CLI group, its own entry-point group
name distinct from `train` even though both are currently backed by
`odyssey_training`).

Also closed: 6.2 (model cards — `models_registry.write_model_card()`, mirrors
`datasets.write_card`'s shape) and 6.4 (promote/export — `promote_model()`
points a named alias at a registered version; `export_model()` downloads a
version's checkpoint bytes back via `checkpoints.download_checkpoint()`, the
inverse of 5.9's upload, verified against the registry's own recorded
sha256 — deliberately does not convert to a serving format like GGUF/ONNX,
same documented scope-cut treatment as 0.11/3.5 before those had a named
consumer). **Step 6 is now fully closed.**

**Still open:** LlamaIndex hooks (a genuinely different, non-LangChain-compatible
instrumentation API — **deliberately deferred, bundled with item 9.4**, not
started here).

---

# Detailed log — what shipped, item by item

Verified against code on 2026-08-27 (`task test` → core 553 passed/1 skipped,
collector 17 passed, dataprep 77 passed, cli 16 passed; all four CI
workflows green on `main`, including `ci-cli`'s `doctor` cold-start check
after its 200ms→400ms/best-of-3 fix).
Ordered by dependency, not by section number.

## 1. Lock in what already works
- [x] **9.2** `.github/workflows/ci-core.yml` — path-filtered to `packages/odyssey-core/**`,
      runs isort/black (check-only) + flake8 + pyrefly + full test suite + golden-fixture
      staleness check on push/PR to `main`. Fixed two pre-existing black-version-drift
      files (`client.py`, `test_livekit.py`) so CI starts green, not red.

## 2. Make "one destination" real (the actual critical path)
- [x] **1.5** `HttpSink` in `packages/odyssey-core/src/odyssey/sinks.py` — stdlib
      `urllib` only (core stays `dependencies = []`). POSTs the same JSONL bytes
      a shard on disk would hold to `{endpoint}/journeys/{journey_id}/events`.
      Raises `HttpSinkError` on any non-2xx or transport failure, which `drain()`
      already treats as retryable — no changes needed to `Spool.push()` /
      `IntervalDrainer` / the CLI. 18 new tests against a real local
      `http.server`, exported as `odyssey.HttpSink`.
- [x] **1.6** Auth (client half) — `HttpSink(api_key=...)` / `ODYSSEY_API_KEY` env
      fallback sends a `Bearer` token. **Still open:** project scoping, which
      needs a server to scope against (see 1.8).
- [x] **1.8** `services/collector` — new workspace member, stdlib `http.server`
      (not FastAPI — deliberately deferred to `services/api`, see its README).
      Receives exactly what `HttpSink` posts, round-trips through
      `odyssey.jsonl.read_events`/`write_events` so there's one codec, not two,
      writes `<journey_id>.jsonl` byte-identical to `FileSink`'s shape. Optional
      `Authorization: Bearer` gate, `GET /health`. 10 tests (dogfooding
      `odyssey.HttpSink` as the client) + a live manual smoke test: SDK record
      → spool → HttpSink → collector → readable file, verified end to end.
      CI added (`ci-collector.yml`). **Still open:** project scoping
      (multi-tenant auth beyond one shared key) and object-store backing
      (still local disk) — noted in the README's "Not done here" section.

## 3. Ship the actual training artifact (the payoff)
- [x] **5.4b** SFT export writer — `sft.py` + `odyssey sft`. One JSON line
      (`{"messages": [...]}`) per step whose final turn is `trainable`, one
      combined `.jsonl` shard (not one-file-per-conversation like Trajectory
      JSON — that's what an SFT trainer actually wants to point at). Gated on
      `result.trainable` (== complete), same as every other exporter. 13 tests.
- [x] **5.5** DPO pair extractor — `dpo.py` + `odyssey dpo`. Walks
      `journey.steps` in order (not prefix-hash grouping — a first attempt at
      that broke, because a regenerated candidate's own step carries the
      earlier rejection in its cumulative history, so literal prefixes never
      match across candidates). A run of `superseded` steps immediately
      followed by one `trainable` step is a decision point; every rejected
      candidate pairs against the winner. Verified against the golden
      fixture's real regenerated→user_edit→thumbs_up chain (2 pairs, matching
      `test_contract.py`'s independently-asserted status labels) plus a live
      CLI smoke test. 15 tests. **KTO/ORPO not done** — different (unpaired)
      data shape, noted explicitly in the module docstring.
- Refactored `export.py`: extracted `_gather_from_dir`/`_gather_from_spool`
  so all three exporters (Trajectory JSON, SFT, DPO) share one "gather
  FoldResults from a directory or a spool" implementation instead of three.

## 4. Cheapest real pipeline win
- [x] **3.3** `data_preparation/normalization` — new workspace member
      (`odyssey-dataprep`). `normalize_odyssey_dir` (thin wrapper over
      `odyssey.export.export_dir`) and `normalize_byod_dir` (dispatches to
      `builders/messages` parsers by format name, then
      `build_journey_from_messages`). Found and fixed a real gap while
      building it: BYOD-built journeys had every `trainable_status` stuck at
      the dataclass default (`not_trainable`), including assistant replies,
      since `build_journey_from_messages` runs no `fold()`. Fixed by reusing
      `fold.derive_trainable_status` directly (empty signal list) — no new
      logic, same rule a signal-less odyssey-recorded journey gets. 12
      tests + a live smoke test (verified the fix visually: assistant turn
      correctly labelled `trainable` before writing the regression test).
      CI added (`ci-dataprep.yml`). Sibling stages (`collection`,
      `cleaning`, `annotation`, `augmentation`, `validation`, `splitting`,
      `flows`, `recipes`) are untouched, still `.gitkeep` scaffolding.

## 5. Round out collection
- [x] **0′.1** OpenAI drop-in client + patch — `integrations/openai.py` +
      `integrations/_openai_base.py`, mirroring `integrations/anthropic.py`.
      Simpler in one respect: OpenAI's system prompt is `messages[0]`, not a
      separate kwarg, so the existing "record only the unrecorded tail" logic
      already covers it with no special case. `messages_from_openai_chat`
      raises on a malformed entry (right for a batch import, wrong on an
      auto-capture path) — `_safe_openai_messages` degrades to a best-effort
      message instead of losing the turn. **OpenAI-compatible providers**
      (Groq, Together, local vLLM/Ollama, ...) work automatically: they speak
      the same SDK with a different `base_url`, and the wrapper forwards
      constructor kwargs untouched — no per-provider code needed. 24 tests
      against a fake SDK plus a live smoke test against the real installed
      `openai` package (request build + response parse, not just the fake),
      and `instrument()`'s default patch target verified against the real
      SDK's internal module layout. `stream=True` passes through unrecorded
      (open item, same as Anthropic's async streaming gap).
- [x] **9.3** `cli/` real entrypoint (ADR 0003) — new `typer`+`rich` workspace
      member. Lazy plugin registry (`odyssey.commands` entry-point group):
      `list_commands` reads entry-point names via `importlib.metadata` (no
      import), `get_command` only calls `entry_point.load()` for the one
      group actually dispatched — verified `odyssey --help` never imports
      `odyssey.cli`. All 7 `odyssey-core` subcommands mounted under
      `odyssey spool`, `odyssey data normalize` from `odyssey-dataprep`,
      deprecated `odyssey push`/`odyssey status` top-level aliases (warn to
      stderr), `odyssey doctor` (cold `--help` measured at 172ms, under the
      200ms budget). Core drops `[project.scripts]`, registers
      `spool = "odyssey.cli:register"` instead; `python -m odyssey.cli`
      unaffected. Hit two real typer 0.27 quirks while building this (both
      documented in `registry.py`'s docstring): a raw `click.Group` root
      breaks nested `--help` once mixed with typer-built subcommands (typer
      no longer shares one exception hierarchy with installed `click`), and
      a lazily-returned command has no `.name` unless set explicitly. 16
      tests, `ci-cli.yml` added.
- [x] **9.9** ADR for the capture layer — `docs/adr/0004-capture-layer.md`.
      Covers the event-sourced core (JourneyEvent as the sole wire unit,
      Step[] as a read-time projection), ambient ContextVar-based journey
      tracking, the single-writer-per-journey contract with detection
      (writer_id, fold's writer_conflict, CLI exit 3), the never-raise
      boundary, and local-only recording (the spool as retry queue). Also
      names its own deliberate exception to ADR 0001 rule 1 (packages/ = no
      side effects) explicitly, which is what item 9.9 actually asked for.
      Fixed two other stale "no ADR" / "no CI" references elsewhere in
      WORKING.md that had drifted since those items shipped.

## Blocking, separate from the roadmap
- [ ] **9.4** `NOTICE` copyright holder unresolved — blocks public release regardless
      of feature work. `packages/odyssey-core/NOTICE` exists; holder line needs checking.
- [x] **4.3** Define `curated_watermark` — written up in
      `openspec/changes/add-journey-schema/design.md` Decision 9 (also closes
      item 9.6, the previously-cited-but-absent design.md itself). `{seq, hash}`:
      `hash = content_hash` over the sorted `(journey_id, journey_content_hash)`
      set — the correctness guarantee (changes iff the curated set or its
      content actually changes; a timestamp or bare count both fail that,
      explicitly ruled out) — `seq` a per-corpus incrementing run counter for
      the human-facing story. Reuses `odyssey.hashing.content_hash` directly,
      no new hashing primitive. **Definition only — not yet implemented in
      code.** That's item 4.5 (the corpus version function itself), still open
      and now unblocked.

## Untouched, downstream of the above (do not start yet)
`training/`, `models/`, `evaluation/`, `services/api`, `apps/web`, `sdk/python`,
`sdk/javascript`, `datasets/` — all scaffolding, all wait on §2–3 above landing first.
