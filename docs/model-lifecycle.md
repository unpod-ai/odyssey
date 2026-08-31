# Model lifecycle

Corpus in, a registered, exportable model out — the chain every command
below writes provenance into so a later "which recipe made this?" always
has an answer. Component-level detail lives in
[`COMPONENTS.md`](COMPONENTS.md) (`data_preparation`, `training`,
`models`, `evaluation`); this page is the *sequence*.

## The chain

```
1. corpus            data_preparation: collection → cleaning → normalization
                      → annotation → augmentation → validation → splitting
                      version = sha(recipe_hash + curated_watermark)
                      odyssey data build-corpus / card

2. training config     odyssey train sft-config / dpo-config / grpo-config
                      corpus shard → validated soup.yaml (soup-cli's own
                      SoupConfig schema — a config that doesn't parse
                      never reaches disk)

3. the actual run      soup train --config soup.yaml
                      NOT run by this repo — a separate step, wherever the
                      GPU lives. training/ never imports torch.

4. experiment record   odyssey train record-experiment --exp-id ... \
                          --config soup.yaml --corpus-version ... --metrics ...
                      writes experiments/<exp_id>.yaml: config sha +
                      corpus version + a metrics pointer (e.g. a W&B URL).
                      Refuses to overwrite an existing exp_id without
                      --overwrite.

5. checkpoint upload   odyssey train upload-checkpoint
                      pushes the trained checkpoint's bytes to an object
                      store, returns a sha256 + uri.

6. model registration  odyssey model register --name ... --sha256 ... \
                          --uri ... --base-model ... --corpus-version ...
                      writes models/registry.yaml: name → version →
                      sha256 → uri → base model → corpus version.

7. model card           odyssey model card --name ... --version ... \
                          --license ... --intended-use ... --limitations ...
                      writes models/cards/<name>-v<version>.md.

8. promote               odyssey model promote --name ... --version ... \
                          --alias production
                      points a human-readable alias (default
                      "production") at an already-registered version —
                      the indirection a caller/`export` reads instead of
                      a raw version number.

9. export                odyssey model export --name ... --out ./dir \
                          [--version N | --alias production]
                      downloads the registered checkpoint's bytes,
                      verified against its registered sha256. Does not
                      convert format (gguf/onnx/safetensors conversion is
                      a separate, not-yet-built concern).

10. evaluation           odyssey eval run --benchmark ... --completions ...
                      scores a completions file the caller produced
                      however they liked (soup-cli run through any
                      inference tool, a raw API call) against a frozen
                      benchmark. Never calls a model itself — there is no
                      live model-serving path in this repo.
                      odyssey eval check-overlap proves the benchmark was
                      never trained on (the frozen eval set's own
                      no-overlap gate).
```

## Why weights never touch git

`training/{checkpoints,logs,outputs}` and `models/{pretrained,finetuned,
exported}` are `.gitkeep`-only. Git holds the recipe and the hash; an
object store (or HF hub) holds the bytes — see
[`adr/0002-artifacts-out-of-git.md`](adr/0002-artifacts-out-of-git.md).
`models/registry.yaml` + `cards/` are the only things about a model that
are ever committed.

## The provenance guarantee

Every step above that mutates a registry refuses to proceed without its
lineage resolved — a model registration names its corpus version, an
experiment manifest names its config sha, a promote/export only moves an
alias, never mutates the underlying registration. The chain this
guarantees, end to end:

> which recipe → which corpus version → which config → which base model → which checkpoint → which report

No answer at any link = not publishable, by construction rather than by
convention (`odyssey data validate` and `odyssey eval check-overlap` both
exit `3` — the contract-violation code, ADR 0003 — when a link would be
missing or a frozen eval set would leak into training).
