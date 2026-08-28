# odyssey-training

The soup-cli adapter (`docs/WORKING.md` items 5.6/5.7): translate an odyssey
corpus into a `soup.yaml` config for [soup-cli](https://trysoup.dev), the
CLI this project pins Python to `<3.13` for. Also writes `experiments/
<exp_id>.yaml` manifests (item 5.8).

## What this is not

This member never trains anything itself, and never imports torch. It
writes a `soup.yaml` — a plain file — and the actual `soup train --config
soup.yaml` run happens separately, on whatever machine has the GPU. `soup-cli`
is installed here as its *light* install only (CLI + config validation, no
`[train]` extra), specifically so this member stays dependency-light the
same way every other odyssey member does.

## Formats

odyssey's own exporters and soup-cli's expected shapes, checked against
soup-cli 0.73.3's real parser (`soup_cli/data/formats.py`), not guessed
from documentation:

- **SFT** — `odyssey sft` already writes exactly soup-cli's `chatml` format
  (`{"messages": [...]}` per line). No translation needed; `write_sft_config`
  points a `soup.yaml` straight at an `odyssey sft` shard.
- **DPO** — `odyssey dpo` writes `{"prompt": [...], "chosen": {...}, "rejected":
  {...}}`, where `chosen`/`rejected` are a single message. soup-cli's `dpo`
  format (which passes straight through to `trl.DPOTrainer`) accepts
  conversational `chosen`/`rejected`, but as message **lists**, not a bare
  message. `translate_dpo_shard` does the one required transform: wrap each
  in a one-element list.

- **GRPO** — odyssey has no GRPO data exporter (a different, unpaired data
  shape than SFT/DPO — the same reason `dpo.py` gives for not implementing
  KTO/ORPO). `write_grpo_config` still writes a real, schema-valid
  `task="grpo"` config against a caller-supplied prompts shard (any
  chatml `*.jsonl`) and a `reward_fn` (one of soup-cli's own built-ins —
  `accuracy`/`format`/`verifiable` — a custom `.py` path, or a
  comma-separated ensemble). It does not fabricate the missing exporter.

Every config this module builds is validated against the real, installed
`soup_cli.config.schema.SoupConfig` before being written — a config that
does not parse never reaches disk.

## Usage

```bash
odyssey train sft-config --base meta-llama/Llama-3.1-8B-Instruct --shard sft.jsonl --out soup.yaml
odyssey train dpo-config --base meta-llama/Llama-3.1-8B-Instruct --shard dpo.jsonl --out soup.yaml
odyssey train grpo-config --base meta-llama/Llama-3.1-8B-Instruct --prompts prompts.jsonl --reward-fn format --out soup.yaml
```

Then, wherever the GPU actually lives: `soup train --config soup.yaml`.

`configs/{base,sft,dpo,grpo}` (item 5.7) holds tracked starter examples,
generated with these same functions so they are guaranteed schema-valid —
copy one and point it at your own shard and base model rather than
hand-writing a `soup.yaml` from scratch.

## Experiment manifests (item 5.8)

`experiments/<exp_id>.yaml` records one run's provenance — config sha +
corpus version + a metrics pointer — small enough to commit even though the
run's own `checkpoints/`/`logs/`/`outputs/` are `.gitignore`'d:

```bash
odyssey train record-experiment \
  --exp-id exp_0001 \
  --config soup.yaml \
  --corpus-version "$(odyssey data corpus-version --recipe recipe.yaml --curated ./curated)" \
  --metrics https://wandb.ai/acme/run/exp_0001
```

Refuses to overwrite an existing `exp_id` unless `--overwrite` is passed —
a silently clobbered manifest would lose the previous run's provenance,
the one thing this file exists to keep.
