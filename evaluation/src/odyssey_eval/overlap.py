"""No-overlap gate — item 7.4: an eval-set journey must never also appear in
a training split.

Reuses `odyssey_dataprep.validation.check_leakage` directly — its generic
``{split_name: [ids]}`` shape ("any id in more than one split is a leak")
already covers "eval vs train" the same way it covers "train vs val vs
test", so no new leakage logic is needed here. Journey id == filename stem,
the same convention every `data_preparation` stage (`validate_dir`,
`split_dir`, ...) already uses for a directory of ``*.json`` journeys.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from odyssey_dataprep.validation import check_leakage

__all__ = ["check_no_overlap"]


def _journey_ids(journeys_dir: Path | str) -> List[str]:
    return sorted(p.stem for p in Path(journeys_dir).glob("*.json"))


def check_no_overlap(
    eval_journeys_dir: Path | str, train_journeys_dir: Path | str
) -> List[str]:
    """Errors, one per id that appears in both dirs — empty when clean.

    ``eval_journeys_dir`` is a frozen eval set's journeys (e.g. what
    `eval_datasets` built a manifest over); ``train_journeys_dir`` is a
    training split's journeys (e.g. `odyssey_dataprep.splitting.split_dir`'s
    ``train/`` output)."""
    splits: Dict[str, List[str]] = {
        "eval": _journey_ids(eval_journeys_dir),
        "train": _journey_ids(train_journeys_dir),
    }
    return check_leakage(splits)
