# odyssey-training

The soup-cli adapter (`docs/WORKING.md` item 5.6): translate an odyssey
corpus into a `soup.yaml` config for [soup-cli](https://trysoup.dev), the
CLI this project pins Python to `<3.13` for.

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

Every config this module builds is validated against the real, installed
`soup_cli.config.schema.SoupConfig` before being written — a config that
does not parse never reaches disk.

## Usage

```bash
odyssey train sft-config --base meta-llama/Llama-3.1-8B-Instruct --shard sft.jsonl --out soup.yaml
odyssey train dpo-config --base meta-llama/Llama-3.1-8B-Instruct --shard dpo.jsonl --out soup.yaml
```

Then, wherever the GPU actually lives: `soup train --config soup.yaml`.
