# Product/Project rename + opt-in server metrics — design

Status: implemented — see git log for the commits.

## Problem

Two unrelated gaps, both raised together because they touch the same
two members (`packages/odyssey-core`, `services/collector`):

1. `services/collector`'s multi-tenant auth concept is named `Project`
   (`slug`/`name`/`api_key` in a `--keys-file`), but "project" is the
   wrong word for it — it's the top-level, unique-key-per-tenant auth
   boundary. Nothing in this repo has publicly shipped yet
   (`docs/WORKING.md`'s only open item is `NOTICE`/9.4, a governance
   task, not engineering), so this is the point to fix the name before
   it calcifies.
2. There is no way to tag a captured journey with which codebase/repo
   it came from, and no opt-in way to capture host-level operational
   telemetry (hostname, OS, CPU/mem/disk) alongside journey capture.

## Decisions

Made during brainstorming (`superpowers:brainstorming`), each with the
alternative considered:

| Decision | Chosen | Rejected alternative, and why |
|---|---|---|
| Migration style | Clean rename — `--keys-file` becomes `--products-file`, JSON shape `{"projects":[...]}` becomes `{"products":[...]}`, old shape stops working | Additive/deprecated-alias — more code and surface for a feature nobody outside this repo has integrated against yet |
| Where "project" (new sub-category) lives | A metadata tag only — `JourneyHeader.journey_metadata["project"]` | A new storage partition (`<data_dir>/<product>/<project>/<date>/...`) — bigger change (touches `prune.py`, `services/api`'s filesystem repository, retention) for a purely descriptive field |
| Metrics transport | A separate channel — new `POST /metrics` on the collector, independent of journey capture | Attached to `JourneyEvent`/`JourneyHeader` fields — couples an ops concern to the training-data wire format, and metrics would only exist when a journey exists |
| Public IP source | Collector-derived — recorded server-side from the TCP peer address of the `/metrics` POST | SDK calls an external IP-lookup service (ifconfig.me, ipify, ...) — a new network dependency, breaking `odyssey-core`'s `dependencies = []` rule, plus a real privacy question about calling a third party |
| Metrics payload content | OS + CPU/mem/disk snapshot only, stdlib-sourced (`platform`, `os.cpu_count`, `shutil.disk_usage`) | Process-level resource usage (needs `psutil` or Linux-only `/proc` parsing) and odyssey-specific counters (`Client.stats`) — both explicitly excluded from this pass |

Explicitly **out of scope** for this design: any `services/api` change
(no route reads `/metrics` data; that's a future "server health"
dashboard concern if ever wanted), any relational/object-store backing
for `/metrics` data (stays a local `.jsonl` file, same storage
discipline the rest of `services/collector` already uses), converting
`project` into a queryable/filterable dimension in `services/api` (it's
a metadata field only in this pass).

## Component 1 — `services/collector`: `Project` → `Product`

Pure rename, same fields and same semantics — `Product(slug, name,
api_key)`. Ripples through every name that says "project" for the auth
concept:

| Before | After |
|---|---|
| `Project` (dataclass) | `Product` |
| `CollectorConfig.projects` | `CollectorConfig.products` |
| `--keys-file` / `ODYSSEY_COLLECTOR_KEYS_FILE` | `--products-file` / `ODYSSEY_COLLECTOR_PRODUCTS_FILE` |
| `{"projects": [{"slug","name","api_key"}]}` | `{"products": [{"slug","name","api_key"}]}` |
| `_load_keys_file` | `_load_products_file` |
| `--init-keys-file` (added this session, `_init_keys_file`) | `--init-products-file` (`_init_products_file`) |
| `GET /projects` | `GET /products` |
| `config.project_for_key()` | `config.product_for_key()` |
| Storage `<data_dir>/<slug>/<date>/...` | Unchanged — `slug` still names the partition, just belongs to a `Product` now |

`--api-key` (the single-shared-key, unscoped mode) is **not** renamed —
it was never project/product-scoped, it stays the simple single-tenant
mode it always was.

## Component 2 — `packages/odyssey-core`: `odyssey/project.py`

New module. One function, `resolve_project(explicit: Optional[str]) ->
Optional[str]`, explicit-beats-env-beats-detected (the same precedence
`config.resolve()` already uses everywhere else):

1. `odyssey.init(project=...)` — explicit argument
2. `ODYSSEY_PROJECT` env var
3. `.git/config` in the cwd (or nearest ancestor) — `[remote "origin"]`
   `url = ...`, take the last path segment, strip a trailing `.git`.
   Parsed with `configparser` (stdlib) or a small regex — no `git`
   subprocess, no new dependency.
4. cwd directory name — always succeeds, the terminal fallback

Any failure at step 3 (no `.git`, no `origin` remote, unreadable file)
falls through silently to step 4, never raises — given step 4 always
succeeds, `resolve_project` itself never returns `None` in practice.

Distinguishing "no `project` argument given, run the auto-detect chain"
from "explicitly disable the tag" needs the same sentinel trick
`config.py`'s `drain_interval_set` already uses for an identical
problem (`None` is a valid explicit value, not just "unset"):
`odyssey.init(project: Optional[str] | _UNSET = _UNSET)` — omitted →
run the chain; `project=None` → skip it, no `project` key is written to
`journey_metadata` at all; `project="foo"` → use `"foo"` literally, skip
env/detection.

Wired into `odyssey.init()`: the resolved value lands in
`JourneyHeader.journey_metadata["project"]`, computed once at `init()`
time (matches "project name" being a property of the *process*, not of
any individual journey). No `SCHEMA_VERSION` bump — `journey_metadata`
is already a free-form `Optional[Dict[str, Any]]`.

## Component 3 — `packages/odyssey-core`: `odyssey/metrics.py`

New module, fully opt-in, off by default. Config knobs (env-first,
explicit wins, same pattern as every other `odyssey.init()` argument):

| Argument | Env var | Default |
|---|---|---|
| `collect_metrics: bool` | `ODYSSEY_COLLECT_METRICS` | `False` |
| `metrics_interval: float` | `ODYSSEY_METRICS_INTERVAL` | `300` (seconds) |

When `collect_metrics` is false (the default), no code in this module
ever runs and no metadata leaves the process — this is the whole point
of "only the metrics, and only if enabled."

When true: a background thread, modeled directly on
`odyssey.spool.IntervalDrainer` (same shape — a daemon thread, sleeps
`metrics_interval`, wakes, does the one thing, repeats; joined on
`Client.shutdown()`), builds one snapshot and POSTs it via the same
stdlib `http.client` transport style `HttpSink` already uses (no new
dependency):

```json
{
  "ts": "2026-09-02T12:00:00+00:00",
  "hostname": "ip-10-0-1-23",
  "os": "Linux-6.8.0-138-generic-x86_64",
  "cpu_count": 8,
  "memory_total_bytes": 16777216000,
  "memory_available_bytes": 8321499136,
  "disk_total_bytes": 512110190592,
  "disk_free_bytes": 128027574272,
  "project": "odyssey"
}
```

`memory_total_bytes`/`memory_available_bytes` are Linux-only (parsed
from `/proc/meminfo`, stdlib file read, no `psutil`); on a platform
where that file doesn't exist, both fields are simply omitted rather
than the whole snapshot failing — a partial snapshot is more useful
than none. `disk_*` comes from `shutil.disk_usage(spool_dir)` — always
available on every platform Python's stdlib supports.

Failures follow ADR 0004's rule: counted (via the same `Client.stats`
counter style journeys already use), never raised, capture never
crashes the host over a metrics POST failing.

## Component 4 — `services/collector`: `POST /metrics`

New handler, same auth path every other POST already uses
(`_authenticate()` — single shared key or per-Product key, unchanged
logic, just reading `config.products` now instead of `config.projects`).

```
POST /metrics
Content-Type: application/json; charset=utf-8
Authorization: Bearer <api_key>          # only when the server requires one

{...the snapshot shape above...}

200 {"ok": true}
400 malformed body
401 missing/incorrect Authorization
500 storage failure
```

The handler adds one field the SDK never sends and never could
authoritatively know: `public_ip`, read from `self.client_address[0]`
(the actual TCP peer address of the connection) — this is what makes
"collector-derived public IP" real rather than trusted client input.

Storage: `<data_dir>/<product_slug>/metrics/<YYYY-MM-DD>.jsonl` in
product-scoped mode, `<data_dir>/metrics/<YYYY-MM-DD>.jsonl` in
single-shared-key mode — its own subdirectory, one line per snapshot,
never mixed into a journey shard file. `prune.py` is unaware of this
directory in this pass (same "not done here" treatment its own README
already gives other deferred pieces) — retention for `metrics/` is
future work if the volume ever justifies it.

## Testing (for the implementation plan to pick up)

- `odyssey/project.py`: explicit arg wins over env wins over git-remote
  wins over dirname; a fixture repo with no `.git`; a fixture repo with
  a `.git/config` but no `origin` remote; an unreadable/malformed
  `.git/config` falls through rather than raising.
- `odyssey/metrics.py`: `collect_metrics=False` (default) starts no
  thread and sends nothing (assert on a mock transport / call count);
  `collect_metrics=True` sends a shape-valid snapshot on the configured
  interval; a POST failure is counted, not raised.
- `services/collector`: rename compiles through — every existing
  `test_a_*_keys_file_*`/`test_a_valid_keys_file_*`/
  `test_get_projects_*` test in `tests/test_server.py` gets renamed
  alongside the code, same assertions; `POST /metrics` — accepted with
  a valid key, 401 without one, `public_ip` in the stored record
  matches the real test client's peer address (this is the one
  genuinely new behavior to prove, not just a renamed existing test);
  malformed body → 400.
- End-to-end (matches this session's own verification pattern for
  `--init-keys-file`): real collector process, real POST, curl/read the
  written file back, don't just trust unit tests in isolation.

## Migration note for this session's own recent commit

`odyssey-collector --init-keys-file` (commit `9085fe3`, this session)
and its runbook (`docs/runbooks/run-services.md`) predate this design
by a few hours and use the old `Project`/`--keys-file` naming
throughout. The implementation plan must rename these too, not leave
them as stragglers — `docs/environment-variables.md` and
`services/collector/README.md`'s "Bootstrapping the file" section also
need the same pass.
