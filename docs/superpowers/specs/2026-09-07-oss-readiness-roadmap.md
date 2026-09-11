# OSS readiness — gap analysis, competitive framing, and roadmap

Status: analysis complete, nothing implemented. No approach below has been
approved for build.

Target assumed throughout: **Odyssey as a public OSS project** — strangers
clone it, self-host it, and adopt it. Every priority call in this document
follows from that. If the target changes to internal-tool or hosted-SaaS,
re-read §6, because the ordering changes.

## 0. Method and evidence standard

Findings came from four parallel code surveys (capture, serve, train/eval,
ops) plus three passes over `nemo_docs.db` — a local corpus of 1,835 pages
including all 147 pages of `docs.maximem.ai`, 43 maxiMem blog posts, and
146 glossary pages, at `/home/anuj/Desktop/Work/nvidia_speech/nemo_docs.db`
(table `pages`). Note there is no `sqlite3` CLI on this box and
`python3 -m sqlite3` does not work either — that entry point arrived in
Python 3.12 and this box runs 3.10. Query it with `python3 -c` and the
`sqlite3` module. Be aware that `url LIKE '%maximem%'` matches 835 rows
because it also catches cloned `maximem_synap_sdk` source files; filter on
`docs.maximem.ai` or `www.maximem.ai` to isolate their published pages.

Two evidence tiers are marked explicitly below, because they carry
different weight:

- **[V]** — verified directly against the code during this analysis.
- **[R]** — reported by a survey agent, cited with file:line but not
  independently re-read. Treat as a strong lead, not settled fact. Confirm
  before acting.

A pre-existing review claimed `services/api` was tested 12/31. That number
was wrong; the real ratio is 26 source files to 11 test files plus a
513-line integration suite [R]. Cited as a reminder that inherited numbers
in this repo's review history are not all trustworthy.

## 1. Strategic framing — Odyssey and maxiMem are complementary, not rival

This is the most important conclusion here and it is empirically settled,
not a judgement call.

maxiMem Synap is a **read path**: conversation in → extract facts,
preferences, episodes, emotions → entity-resolve against a registry →
store across vector/graph/file → retrieve in <15ms to inject into the
agent's context *now*.

Odyssey is a **write path**: journey capture → seven dataprep stages →
SFT/DPO shards → a fine-tuned model that is better *next time*.

Both consume the identical input stream. They terminate in different
places. Evidence that the training axis is genuinely uncontested [V]:

> Across all 147 pages of `docs.maximem.ai` — their actual product
> documentation — there are **zero** occurrences of `fine-tuning`,
> `training data`, `RLHF`, `distillation`, `SFT`, or `DPO`.

All 28 `fine-tuning` hits sitewide land on `www.maximem.ai` — the
marketing site's glossary, blog, and research pages — and none in the
product docs or the SDK source. Same shape for the 7 `RLHF` and 5 `LoRA`
hits. That is SEO and educational content, not product capability.

Caution for anyone re-running this check: a naive
`LIKE '%DPO%'` returns 61 false-positive pages because **"en*dpo*int"**
contains the substring. Use word boundaries.

**Implications.**

1. Odyssey's competitor is Langfuse — which `docs/WORKING.md` already
   names as its reference — not Mem0/Zep/Letta/maxiMem.
2. maxiMem ships 14 framework integrations, has NVIDIA/ByteDance/Razorpay
   as users, and maintains comparison pages against six competitors. They
   have no path from captured conversations to a trained model. That is
   Odyssey's entire thesis.
3. Their content cluster arguing against "skills replace memory"
   (`/memory-vs-agent-skills`, `/research/agent-skills`) shows the
   retrieval axis is crowded and contested. The training axis is empty.

The strategic move is therefore **not** to add memory/retrieval features.
It is to be the best training-data capture layer, and to be adoptable.

## 2. Gap inventory

### Tier 1 — the product hole

**No conversation content is reachable anywhere in the product.** [V]

`StepOut` (`packages/odyssey-schemas/src/odyssey_schemas/__init__.py:47-50`)
carries only `index`, `trainable_status`, `message_count`. At
`services/api/src/odyssey_api/routers/journeys.py:93-100` the code computes
`len(step.messages)` and discards the messages themselves. The web
drill-down therefore renders a table of step numbers and counts.

A user cannot read a single conversation turn. For a journey-observability
and training-data tool this is the core missing view, and no other item on
this list matters as much.

### Tier 2 — correctness, data loss, and denial of service

**2.1 Poison-batch infinite retry with unbounded disk growth.** [V]
`packages/odyssey-core/src/odyssey/spool.py:657` catches bare `Exception`
with the comment `# noqa: BLE001 - any sink failure is retryable`, and
`sinks.py:268` raises `HttpSinkError` for any status ≥ 300. So HTTP 400
(schema-major skew), 401, and 403 retry forever. The watermark never
advances, and `gc()` only deletes fully-drained journeys — so disk grows
without bound on a permanently-failing batch. There is no dead-letter, no
max-attempts, no quarantine.

**2.2 Unbounded request body.** [V]
`services/collector/src/odyssey_collector/server.py:523-524` reads
`Content-Length` into memory with no cap, then gzip-decompresses it. Two
independent surveys flagged this. Memory exhaustion and zip-bomb are both
trivial. Rate limiting is a documented deliberate cut (`server.py:64-66`),
which compounds it.

**2.3 OpenAI streaming silently unrecorded.** [V]
`packages/odyssey-core/src/odyssey/integrations/openai.py:89,165,206` —
`if kwargs.get("stream"): return` passthrough. It is disclosed in the
module docstring (lines 31-32, 79-82), so this is a known scope cut, not a
hidden bug. But streaming is the production default: a user wires up
capture, streams, and gets an empty spool with no warning and no counter.

**2.4 No schema migration path.** [R]
`packages/odyssey-store/.../schema.py` is entirely
`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` with no
`PRAGMA user_version` and no migration runner. Adding a column ships as a
silent no-op against every existing database, and `IF NOT EXISTS` masks
it. Both collector and api open the same file via `ODYSSEY_DB_URI`, so a
version-skewed deploy is undetectable.

**2.5 Read-side auth is one global key with no product scoping.** [R]
`services/api/.../deps.py:33-53` compares a single `settings.api_key` —
correct crypto (`compare_digest`), wrong model. The collector has
per-product hashed keys; the read side does not. `get_journey`
(`domain/journeys.py:90-94`) takes only `journey_id` with no product
filter, so journey IDs are cross-tenant readable.

**2.6 Drain telemetry is computed and thrown away.** [V]
`DrainResult` (`spool.py:214-224`) carries `pushed`, `skipped`, `failed`,
`errors`, `gaps`, `journeys` — genuinely useful. `IntervalDrainer`
overwrites `self.last_result` every tick (`spool.py:848,864`). Nothing
accumulates. There is no consecutive-failure count and no oldest-undrained
age, so an operator whose capture broke six hours ago has no signal.

### Tier 3 — OSS adoption blockers

**3.1 Capture is Python-only.** [R] Both SDKs (`sdk/python`,
`sdk/javascript`) are generated read-side OpenAPI clients — no `record`,
no spool, no `JourneyEvent`. LiveKit and LangChain, the two flagship
integrations, have very large TS install bases. This is the single biggest
reach limitation, and it is the same confusion the repo already flagged
internally as the "`odyssey-sdk` naming collision."

**3.2 `infra/` is four empty `.gitkeep` files.** [R] No Docker, no
compose. Deployment is `git pull` + `uv sync` + `pnpm build` on the
production host (`scripts/deploy-restart.sh:15-24`), and two of the three
systemd units it restarts are not in the repo at all (`:30,41`). A
stranger will not bootstrap a uv workspace to evaluate a project.

**3.3 The headline status doc is stale.** [V]
`docs/WORKING.md:44-62` still reports "**6 of 10 layers built**" with L7
HTTP transport `❌ 0% (only FileSink)`, L8 collector `❌ 0%`, and L9
dashboard `❌ 0%`. All three shipped weeks ago. The phase checklist at
`:231-245` leaves Steps 3–6 unticked despite `docs/NEXT.md` recording them
closed. This is the first table a prospective adopter reads.

Worth stating plainly, because a prior review concluded the opposite: this
repo's documentation-to-code fidelity is excellent in its ADRs and module
docstrings and **poor in its top-of-file status tables**. Those are
different artifacts with different decay rates.

**3.4 `NOTICE` copyright holder (item 9.4)** still blocks distribution and
still needs a human, not engineering.

### Tier 4 — the loop does not close

**4.1** Training is an out-of-repo handoff by design
(`training/README.md:9-16` — never trains, never imports torch) and
evaluation never generates completions (`harness.py:3-16` — no live model
path) [R]. Both are documented decisions, but together they mean a human
carries artifacts between stages. The flywheel does not spin unattended.

**4.2** Only two evaluation metrics exist, 39 lines total, and
`tool_call_accuracy` just re-reports `1 - tool_error_rate` from the fold —
it scores nothing new [R].

**4.3** `datasets/`, `models/`, and `training/experiments/` contain only
`.gitkeep` [V]. Lineage is well-designed and has never been exercised.

**4.4** The `dataset-audit` CI gate passes over zero registries
(`evaluation/src/odyssey_eval/audit.py:65-75`) [V]. Mitigating: its
success string honestly reads `"ok: no manifest integrity breaches (or no
registries yet)"`, so it discloses the vacuity rather than hiding it. A
green check that proves nothing is still a green check that proves
nothing.

**4.5** GRPO has a config writer but no data exporter; `adapters/` and
`callbacks/` are empty scaffolding [R].

## 3. What to port from maxiMem — with verdicts

### PORT — stable PII aliasing (highest value in the corpus)

maxiMem detects at ingest and replaces with a **deterministic alias**:
`[[PERSON_AADHAAR_h2n7v5cx8m0d]]`. The same input value always produces
the same alias, so "deduplication, corrections, and search keep working on
a consistent string."

Odyssey uses one constant, `REDACTED = "[REDACTED]"`
(`spool.py:61`) [V]. Two *different* card numbers collapse to the same
token, destroying coreference — a model cannot distinguish "the user
repeated the same value" from "the user gave a different one."

This directly answers the objection Odyssey's own code states (that
blanket redaction would destroy the training corpus, `spool.py:65-71`).
Aliasing is not blanket redaction: it removes the secret while preserving
exactly the structure that makes a turn trainable. For a training-data
tool specifically, this is a better fit than it is for maxiMem.

Sub-items worth taking with it:

- **"Watching" mode** — run detection, count findings, change nothing,
  "so you find out what is actually in your traffic before you make a
  decision." `odyssey.pii` already has `scan_pii`; it is simply never run
  at capture time. Cheapest win in this document.
- **Floor vs. policy layer** — seven types (card number, CVV, PIN,
  passwords, API keys, private keys, raw biometrics) are never stored and
  no setting overrides that; everything else is policy. `DEFAULT_REDACT_KEYS`
  is a floor with no policy layer above it.
- **Published detector limits** — they state outright that "names and
  street addresses are not detected" and that health, origin, and belief
  data are out of scope. `odyssey.pii` has comparable real limits with no
  user-facing caveat. For an OSS release this is a half-hour docs change
  that prevents a trust-destroying surprise.
- Caveat: their shipped detectors are India-first (Aadhaar, PAN, GSTIN,
  IFSC, UPI). Do not assume the category list transfers.

### PORT — crypto-shredding, which solves a problem Odyssey currently cannot

maxiMem encrypts protected values under a per-account key; erasing a
person destroys the encrypted form "which makes those values unreadable
everywhere at the same moment, including in backups already taken. There
is no waiting period and nothing to scan for."

Odyssey's lineage chain is content-addressed and immutable:
`content_hash` → `curated_watermark` → `corpus_version`. Honoring a GDPR
erasure request today would require rewriting shards and invalidating
every downstream manifest, model card, and corpus version. It is
architecturally impossible, not merely unimplemented.

If PII were encrypted at capture under a per-subject key, deleting that
key leaves shard bytes **byte-identical** — the entire hash chain survives
while the values become unrecoverable. This reconciles immutable
provenance with right-to-erasure. For a public OSS project handling
conversation data, "we have no deletion story" is a serious adoption
objection, and this removes it without compromising the reproducibility
that is arguably the repo's best design work.

### PORT — the error taxonomy as the retry decision

maxiMem's exception hierarchy *is* the retry policy:

```
SynapTransientError   (auto-retried)
  NetworkTimeoutError, RateLimitError, ServiceUnavailableError, ...
SynapPermanentError   (never retried; needs a code or config fix)
  InvalidInputError, AuthenticationError, ContextNotFoundError,
  SessionExpiredError, ...
```

with `RetryPolicy(max_attempts=5, backoff_base=1.0, backoff_max=10.0,
backoff_jitter=True, retryable_errors=[...])` — an allowlist, not a
denylist. Stream reconnect is separate: 10 attempts, **counter resets on
each success**. Every error carries a `correlation_id`.

This is a better fix for §2.1 than the max-attempts counter it first
appears to need. Auth failure and malformed-batch are *permanent* and
should quarantine on the first response rather than burn five attempts.
Odyssey already has the hardest piece — `_note_retry_after` honors
`Retry-After`. What is missing is the class split, max attempts, jitter,
and a delay ceiling. The reset-on-success detail is worth copying: it
stops a long-lived drainer exhausting its budget on unrelated blips.

Note: maxiMem documents **no** client queue cap and no circuit breaker.
Odyssey's unbounded-spool growth is not solved by them either; that part
must be designed here.

### PORT — scope resolved from the credential, never from the client

> "You never pass the `instance_id` directly on SDK calls: the Instance is
> resolved from the API key you authenticate with."

That is exactly the fix pattern for §2.5. Odyssey's collector already does
this with hashed product keys; `services/api` simply does not. Related:
they reject a mismatched tenancy parameter with **HTTP 400** rather than
silently ignoring it.

The four-level ladder (World → Client → Customer → User) itself is
**SURFACE-LEVEL** for Odyssey — training corpora do not need those tiers.

### PORT — capture-time depth dial (not a drop dial)

maxiMem's `mode="fast"|"long-range"` tunes **extraction depth per item and
never drops the item**: fast skips deep entity resolution, relationship
mapping, topic categorization, and sentiment analysis.

`ODYSSEY_SAMPLE_RATE` is one coin-flip per journey that discards the
journey entirely. A depth tag set at capture and honored by `run_recipe`
would preserve every journey's header, metrics, and status while running
the expensive stages (PII scan, LLM augmentation, annotation queueing) on
a subset only. Odyssey's stages already share a uniform dir-in/dir-out
contract, so this fits the existing design rather than fighting it.

### PORT — a quarantine tier between "valid" and "halt"

maxiMem routes uncertain cases to human review on three named triggers:
multiple entries at similar confidence; semantic match in the "ambiguity
zone (confidence between 0.5 and 0.8)"; and an auto-registered entity that
closely resembles an existing entry.

That third trigger maps onto a real Odyssey corpus-quality problem:
`dedupe_journeys` catches only exact `content_hash` collisions and
silently keeps near-duplicates, which causes memorization and skewed
weighting. The transferable principle is a *bounded uncertainty band
routed to humans* versus Odyssey's binary `validate` → exit 3 → halt.

Odyssey already has the machinery idle: `annotation`'s `build_queue` and
`apply_reviews`. maxiMem concedes their queue is dashboard-only with SDK
access merely "on the roadmap" — a CLI-driven queue would put Odyssey
ahead here.

### PORT — operational alerting shape

Webhooks on `ingestion.failed`, `credential.expiring`, `config.applied`;
alert thresholds of "ingestion failures (any occurrence)" and — the good
one — *"retrieval returning zero results when memories are expected"*, a
dead-man's-switch. The Odyssey analogue is "zero journeys ingested today,"
which §2.6's discarded counters already almost support. They also publish
timeout defaults as a table (connect 5s, read 30s, write 10s); Odyssey
documents no timeout surface at all.

### SKIP — the 4-tier entity registry

Canonical name, aliases, embeddings, and `last_seen` exist so retrieval
can resolve "my team lead from engineering" → John Smith. **Training data
never retrieves.** The narrow exception is consistent pseudonymization
across journeys, but building an embedding registry for that is wildly
disproportionate to `pii.py`'s regex approach — and aliasing (above)
already delivers the consistency.

### SKIP — context compaction (and note the naming trap)

It compresses conversation history to fit a prompt window. For SFT/DPO
this is **actively harmful**: it destroys the verbatim cumulative-prefix
invariant that `cleaning`'s dead-turn splice works specifically to
preserve. Recorded here so nobody ports it later by name-association.

### SKIP — MACA (config generated from a use-case description)

It fights `recipe_hash`, which exists precisely so configs are
reproducible. A `recipe init` scaffolder is a reasonable ergonomic idea on
its own merits; generating policy from prose is not.

### SKIP — multi-format ingestion, hosted-service concerns

Email/PDF/image/audio ingestion is low value for training-grade capture.
Cost tracking, BYOK/VPC/SSO tiering, and cert pinning are hosted-backend
concerns with no self-hosted analogue.

## 4. Onboarding — Odyssey's unearned advantage

maxiMem's quickstart states **~10 minutes** across 8 numbered steps.
Steps 1–3 are Create Client → Create Instance → Generate API Key.

**Odyssey is self-hostable, so its try-it path needs no signup at all.**
`docker compose up`, a short snippet, open localhost, see the journey.
That is a strictly better first-run story than a well-funded competitor's,
and the only thing standing in the way is §3.2's empty `infra/`. This
should be the headline of the README.

Friction removers worth copying, in leverage order:

1. **A coding-agent skill.** They ship one so users tell Claude Code,
   Cursor, or Codex "add Synap to my app" and it knows the SDK and every
   integration. Highest leverage-to-effort item found in this analysis;
   Odyssey could ship one in a day.
2. **An intent router above the quickstart** — six branches including "I
   just want to try without installing anything" and "I'm moving from
   Mem0/Zep/Letta." Nobody lands on the wrong page.
3. **A TL;DR Hello World above the numbered steps** — one complete
   runnable script, using a real completion wait rather than a fixed
   sleep.
4. **Footguns documented inline** rather than left in an issue tracker
   (they call out a camelCase/snake_case trap that fails at the call site,
   and a Windows Store `python` alias shadowing).
5. Install tabs across Python/JS × pip/poetry/uv/npm/pnpm × shell
   variants.

## 5. Things this analysis validated rather than improved

Worth recording so they are not "fixed" by a later pass:

- **Redaction that is key-name-based and deliberately not prose-scanning**
  (`spool.py:65-71`) is the correct call for a training corpus. Aliasing
  extends it; it does not overturn it.
- **`fsync=False` by default** (`spool.py:191`) is a named tradeoff with
  an escape hatch, not an oversight.
- **The `SCHEMA_VERSION` 2.0 no-migration decision** matches how a funded
  competitor handled an analogous cache-path break — they also declined to
  migrate, and stated why, rather than carrying contamination forward.
- **The DPO pairing algorithm** (`dpo.py:73-92`) handles cumulative-step
  regeneration chains correctly, which is a subtlety most implementations
  get wrong [R].
- **The lineage design** (`corpus_version` over `recipe_hash` +
  `curated_watermark`, order-independent and per-journey) is the strongest
  design work in the repo. It is only untested because nothing has been
  registered yet.

## 6. Proposed sequence

Ordered for the OSS target. Each band is independently shippable.

**Band A — visibility.** Message content through `StepOut`, the API, and a
real conversation view in `apps/web`; Docker Compose and a no-signup
quickstart; correct the stale `WORKING.md` scorecard. *Rationale: for OSS
adoption nothing else matters if a stranger can neither run it nor see
anything once it runs.*

**Band B — hardening.** Error taxonomy (transient vs. permanent) replacing
the blanket-retry in `spool.py:657`; request size cap returning 413;
product-scoped read auth derived from the key; accumulate drain counters
and expose queue depth, oldest-undrained age, and consecutive failures;
`PRAGMA user_version` migrations. *Rationale: all verified, all contained,
and this is what a security-minded evaluator greps for first.*

**Band C — differentiation.** PII aliasing plus watching mode at capture
time; crypto-shredding for erasure with lineage intact; capture-time depth
dial; quarantine tier with near-duplicate detection. *Rationale: this is
what makes Odyssey more than a Langfuse clone, but it deepens a pipeline
nobody can see into until Band A lands.*

**Band D — reach.** The TypeScript capture SDK. *Large enough to need its
own brainstorm and spec; do not fold it into another band.*

Cheap and unsequenced: the coding-agent skill, and publishing
`odyssey.pii`'s real detector limits.

## 7. Open questions

1. Does crypto-shredding belong at the spool (capture) or at the dataprep
   boundary? Capture is stronger for erasure guarantees; dataprep is far
   less invasive to the hot path. This needs its own design pass.
2. Should the depth dial replace `ODYSSEY_SAMPLE_RATE` or compose with it?
3. Is the near-duplicate detector worth a real similarity measure, or is
   normalized-content hashing enough to catch what matters?
4. Does the TS SDK wrap capture natively, or post to the collector over
   HTTP from a thin client? The latter is far less code and loses the
   local spool's crash-durability.
