"""The soup-cli adapter — item 5.6: an odyssey corpus (`odyssey sft`/`odyssey
dpo` output) into a `soup.yaml` config for `soup train --config soup.yaml`.

Checked against soup-cli 0.73.3's real parser and schema
(`soup_cli/data/formats.py`, `soup_cli/config/schema.py`), not guessed from
documentation:

- **SFT** — `odyssey sft` already writes exactly soup-cli's ``chatml``
  format (``{"messages": [...]}`` per line; `soup_cli.data.formats.
  _convert_chatml` is a literal passthrough of that same shape). No
  translation needed.
- **DPO** — `odyssey dpo` writes ``{"prompt": [...], "chosen": {...},
  "rejected": {...}}``, where ``chosen``/``rejected`` are a single message.
  soup-cli's ``dpo`` format (`_convert_dpo`, which passes straight through
  to `trl.DPOTrainer`) accepts conversational ``chosen``/``rejected``, but
  as message **lists**, matching TRL's own conversational DPO contract —
  not a bare message. :func:`translate_dpo_shard` does that one required
  wrap; nothing else needs to change.

Every config this module builds is validated against the real, installed
``soup_cli.config.schema.SoupConfig`` before being written — a config that
does not parse never reaches disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from soup_cli.config.schema import SoupConfig

__all__ = ["translate_dpo_shard", "write_sft_config", "write_dpo_config"]


def translate_dpo_shard(src_path: Path | str, out_path: Path | str) -> int:
    """`odyssey dpo`'s own shape into soup-cli's `dpo` format: wrap
    `chosen`/`rejected` in a one-element message list. Returns the row
    count written."""
    count = 0
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with (
        open(src_path, encoding="utf-8") as fin,
        open(tmp, "w", encoding="utf-8") as fout,
    ):
        for line in fin:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            fout.write(
                json.dumps(
                    {
                        "prompt": row["prompt"],
                        "chosen": [row["chosen"]],
                        "rejected": [row["rejected"]],
                    }
                )
                + "\n"
            )
            count += 1
    tmp.replace(out)
    return count


def _build_config(
    *,
    task: str,
    data_format: str,
    base: str,
    train_shard: Path | str,
    output: str,
    backend: str,
    training: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    config: Dict[str, Any] = {
        "base": base,
        "task": task,
        "backend": backend,
        "data": {"train": str(train_shard), "format": data_format},
        "output": output,
    }
    if training:
        config["training"] = training
    # Fails before writing, not after `soup train` chokes on a bad key --
    # validated against soup-cli's own real pydantic schema, not a
    # hand-guessed one.
    SoupConfig(**config)
    return config


def _write_yaml(config: Dict[str, Any], out_path: Path | str) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    tmp.replace(out)
    return out


def write_sft_config(
    *,
    base: str,
    train_shard: Path | str,
    out_path: Path | str,
    output: str = "./output",
    backend: str = "transformers",
    training: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write a `soup.yaml` for an `odyssey sft` shard directly — no
    translation step, see the module docstring."""
    config = _build_config(
        task="sft",
        data_format="chatml",
        base=base,
        train_shard=train_shard,
        output=output,
        backend=backend,
        training=training,
    )
    return _write_yaml(config, out_path)


def write_dpo_config(
    *,
    base: str,
    train_shard: Path | str,
    out_path: Path | str,
    output: str = "./output",
    backend: str = "transformers",
    training: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write a `soup.yaml` pointing at an already-translated DPO shard
    (:func:`translate_dpo_shard`'s output — `odyssey dpo`'s own file
    directly will not validate as soup-cli's `dpo` format)."""
    config = _build_config(
        task="dpo",
        data_format="dpo",
        base=base,
        train_shard=train_shard,
        output=output,
        backend=backend,
        training=training,
    )
    return _write_yaml(config, out_path)
