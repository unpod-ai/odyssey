# odyssey-dataprep

`data_preparation` stages. Only `normalization` exists so far — the rest of
the planned pipeline (`collection`, `cleaning`, `annotation`, `augmentation`,
`validation`, `splitting`, `flows`, `recipes`; see `docs/STRUCTURE.md`) is
still `.gitkeep` scaffolding.

## normalization

Raw traces → canonical `Journey` artifacts (Trajectory JSON), via
odyssey-core's own `fold()` and BYOD builders. No new parsing logic lives
here — this is the stage wrapper `docs/WORKING.md` item 3.3 asked for over
an engine (`odyssey.export.export_dir`, `odyssey.builders.messages`,
`odyssey.builders.journey.build_journey_from_messages`) that already existed
and was already tested.

Two raw shapes in, one canonical shape out:

```python
from odyssey_dataprep.normalization import normalize_odyssey_dir, normalize_byod_dir

# Already-drained odyssey *.jsonl (from a spool or services/collector).
result = normalize_odyssey_dir("./events", "./normalized")

# A directory of *.json files in a provider's own message format — one file
# per conversation, either a bare array or {"messages": [...], ...}.
result = normalize_byod_dir(
    "./raw_exports", "./normalized",
    format="openai_chat",       # or "anthropic_messages" / "vercel_ai_sdk"
    data_source="customer_export",
)

print(result.count, result.errors)
```

`normalize_odyssey_dir` keeps the completeness diagnostics folding already
produces (`incomplete`, gaps, writer conflicts) — folding is still folding.
`normalize_byod_dir` has no fold to diagnose: a BYOD export carries no
`seq`/terminal-event concept, so a file either parses into a `Journey` or is
reported as an error, and every parseable file is written.

## Run it

```bash
cd data_preparation
uv sync --extra dev
uv run pytest tests
```
