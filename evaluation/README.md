# odyssey-eval

The offline evaluation harness (`docs/WORKING.md` Step 7): score
caller-produced completions against a frozen benchmark suite.

## What this is not

This member never calls a model. `services/api` (Step 8) is a read-only
API over already-produced results, not a model-serving path — there is
still no live inference endpoint anywhere in this repo — so the harness
takes a benchmark (`benchmarks/*.yaml`: task prompts + references) and a
completions file the caller produced however they like (a `soup-cli`
-trained model run through any inference tool, a raw API call, whatever),
and scores the pairing. See `src/odyssey_eval/harness.py`'s module
docstring for the full design decision, including why `judges.py`
(LLM-as-judge scoring, named in `docs/STRUCTURE.md`) is deliberately not
built yet.

## Layout

- `src/odyssey_eval/` — the installable package: `runner.py` (load +
  score), `harness.py` (report writing), `eval_datasets.py` (frozen eval
  set manifests/registry/cards, item 7.2), `overlap.py` (no-overlap gate,
  item 7.4), `cli.py`.
- `benchmarks/` — TRACKED suite defs (yaml: task prompts + references).
- `metrics/` — TRACKED metric implementation code, loaded dynamically by
  `runner.load_metric` rather than imported as part of the package — a new
  metric can be added without a release. Ships with `exact_match` and
  `tool_call_accuracy` (the latter reuses `odyssey.primitives.JourneyMetrics`'
  already-computed `tool_error_rate`).
- `datasets/` — TRACKED manifests + registry + cards for frozen eval sets;
  never trained on (enforced by `overlap.py`, not by write-protection here).
- `reports/` — gitignored, generated `*.json`/`*.md` per run;
  `reports/templates/` is tracked.

## CLI

Run these from the repo root so the default `evaluation/metrics` and
`evaluation/reports` paths resolve correctly. Completions JSONL rows use the
key `response` for the model output.

```bash
odyssey eval run --benchmark evaluation/benchmarks/example-arithmetic.yaml --completions completions.jsonl
odyssey eval compare --a evaluation/reports/a.json --b evaluation/reports/b.json
odyssey eval build-set --name my-eval --shard journeys/
odyssey eval card --name my-eval --license ... --intended-use ... --provenance ...
odyssey eval check-overlap --eval-journeys evaluation/datasets/.../journeys --train-journeys data_preparation/.../train
```
