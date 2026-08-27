"""Extract DPO preference pairs — ``(prompt, chosen, rejected)`` — from folded
journeys. Closes ``docs/WORKING.md`` item 5.5.

The schema was built for this from day one: :class:`~odyssey.primitives.Signal`
carries an *ordering* (``regen_order``) and the corrected text
(``edited_output``) precisely so a ``regenerated``/``user_edit`` signal can
mark one candidate ``superseded`` rather than merely ``not_trainable`` (see
``fold.derive_trainable_status``). A pair exists wherever a run of
``superseded`` steps is immediately followed by a ``trainable`` one — one or
more rejected candidates for a turn, resolved by whichever answer won.

A bare ``thumbs_down`` with no recorded alternative is *not* a pair: there is
nothing to prefer it over. KTO/ORPO want unpaired single-response labels
(``prompt``, ``response``, ``label``) rather than a chosen/rejected pair —
a different data shape, not produced here.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from odyssey.export import _gather_from_dir, _gather_from_spool
from odyssey.fold import FoldResult
from odyssey.jsonl import _strip_none
from odyssey.primitives import Message, Step


@dataclass(frozen=True)
class DpoResult:
    """What one DPO extraction run did, including what it skipped and why."""

    written: int = 0
    skipped_incomplete: Dict[str, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _message_dict(message: Message) -> Dict[str, Any]:
    d = _strip_none(dataclasses.asdict(message))
    d.pop("trainable_status", None)
    return d


def dpo_pairs(result: FoldResult) -> List[Dict[str, Any]]:
    """``(prompt, chosen, rejected)`` triples for one journey.

    Walks ``journey.steps`` in order rather than grouping by prompt prefix:
    a step's ``messages`` is cumulative, so a candidate downstream of an
    earlier rejection carries that rejection in its *own* history — the
    step ending on "candidate C" does not share a literal prefix with the
    step ending on "candidate A" two regenerations earlier, even though both
    answer the same turn. Only the *first* rejected candidate in a chain has
    a prompt uncontaminated by the rejections before it, so that one supplies
    ``prompt`` for every pair the chain produces.

    A run of consecutive ``superseded`` steps followed by one ``trainable``
    step is one chain: every member of the run pairs against that trainable
    step. Anything else (a ``trainable`` step with nothing pending, a bare
    ``thumbs_down`` with no regeneration) resets the pending run — there is
    nothing to pair it against.
    """
    cid = result.journey.task.conversation_id or result.journey_id
    out: List[Dict[str, Any]] = []
    pending: List[Step] = []

    for step in result.journey.steps:
        status = step.trainable_status
        if status == "superseded":
            pending.append(step)
            continue
        if status == "trainable" and pending:
            prompt = [_message_dict(m) for m in pending[0].messages[:-1]]
            chosen = _message_dict(step.messages[-1])
            for rejected_step in pending:
                out.append(
                    {
                        "conversation_id": cid,
                        "prompt": prompt,
                        "chosen": chosen,
                        "rejected": _message_dict(rejected_step.messages[-1]),
                    }
                )
        pending = []

    return out


def save_dpo(results: Iterable[FoldResult], out_path: Path | str) -> DpoResult:
    """Write every DPO pair across ``results`` as one JSONL file.

    Written whole to a temp file, then moved into place — same discipline as
    :func:`odyssey.export.save` and :func:`odyssey.sft.save_sft`.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    lines: List[str] = []
    written = 0
    skipped: Dict[str, str] = {}
    for result in results:
        if not result.trainable:
            skipped[result.journey_id] = result.incomplete_reason or "not trainable"
            continue
        for pair in dpo_pairs(result):
            lines.append(json.dumps(pair, sort_keys=True))
            written += 1

    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp.replace(out)

    return DpoResult(written=written, skipped_incomplete=skipped)


def export_dpo_dir(
    events_dir: Path | str,
    out_path: Path | str,
    *,
    journey_id: Optional[str] = None,
) -> DpoResult:
    """Fold every drained ``*.jsonl`` in a directory and write a DPO file."""
    results, errors = _gather_from_dir(Path(events_dir), journey_id)
    result = save_dpo(results, out_path)
    return dataclasses.replace(result, errors=errors + result.errors)


def export_dpo_spool(
    spool_root: Path | str,
    out_path: Path | str,
    *,
    journey_id: Optional[str] = None,
) -> DpoResult:
    """Fold straight from the spool and write a DPO file. Does not drain —
    no watermark moves, same as :func:`odyssey.export.export_spool`."""
    results, errors = _gather_from_spool(Path(spool_root), journey_id)
    result = save_dpo(results, out_path)
    return dataclasses.replace(result, errors=errors + result.errors)
