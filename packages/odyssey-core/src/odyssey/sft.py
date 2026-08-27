"""Write SFT (supervised fine-tuning) training data — messages-only, one JSON
line per trainable turn.

Where ``export.py``'s :func:`~odyssey.export.save` produces the Trajectory
artifact (task + steps + reward + metrics — what the platform consumes), this
produces the file a trainer actually reads: ``{"messages": [...]}`` per line,
because that is the entire shape an SFT trainer needs and everything else in
the Trajectory artifact is dead weight to it. Closes ``docs/WORKING.md`` item
5.4b — "nothing converts a ``Journey`` into an SFT file".

Only steps whose ``trainable_status == "trainable"`` are emitted.
``superseded`` (the rejected side of a regeneration/edit — see ``dpo.py``),
``not_trainable`` (context turns, or an explicit ``thumbs_down``), and
``summarization_boundary`` never become an SFT target — ``fold.py``'s own
precedence already decided that; this module trusts it rather than
re-deriving it. A journey must be ``trainable`` (== ``complete``) at the fold
level too: an incomplete journey has a known hole in it, and training on it
silently teaches the model a conversation that never happened — the same gate
``export.py`` and ``fold.py`` already apply everywhere else.

One combined ``.jsonl`` file, not one file per conversation: unlike the
Trajectory artifact (one document a human or a platform opens per call), this
is a training shard, and every SFT trainer that reads JSONL wants one file to
point at, not a directory to glob.
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
from odyssey.primitives import Message


@dataclass(frozen=True)
class SftResult:
    """What one SFT export run did, including what it skipped and why."""

    written: int = 0
    skipped_incomplete: Dict[str, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _message_dict(message: Message) -> Dict[str, Any]:
    """A ``Message`` as a plain dict, minus the fold's own bookkeeping field.

    ``trainable_status`` is odyssey's internal derived label — every message
    reaching this function has already been filtered on it, so repeating it
    on the line is noise no SFT trainer's schema expects.
    """
    d = _strip_none(dataclasses.asdict(message))
    d.pop("trainable_status", None)
    return d


def sft_examples(result: FoldResult) -> List[Dict[str, Any]]:
    """SFT examples for one journey: one per step whose final turn is trainable."""
    cid = result.journey.task.conversation_id or result.journey_id
    out: List[Dict[str, Any]] = []
    for i, step in enumerate(result.journey.steps):
        if step.trainable_status != "trainable":
            continue
        out.append(
            {
                "conversation_id": cid,
                "step_index": i,
                "messages": [_message_dict(m) for m in step.messages],
            }
        )
    return out


def save_sft(results: Iterable[FoldResult], out_path: Path | str) -> SftResult:
    """Write every trainable step across ``results`` as one JSONL file.

    Written whole to a temp file, then moved into place: a reader must never
    pick up a half-written training shard, same discipline as
    :func:`odyssey.export.save`.
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
        for example in sft_examples(result):
            lines.append(json.dumps(example, sort_keys=True))
            written += 1

    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp.replace(out)

    return SftResult(written=written, skipped_incomplete=skipped)


def export_sft_dir(
    events_dir: Path | str,
    out_path: Path | str,
    *,
    journey_id: Optional[str] = None,
) -> SftResult:
    """Fold every drained ``*.jsonl`` in a directory and write an SFT file."""
    results, errors = _gather_from_dir(Path(events_dir), journey_id)
    result = save_sft(results, out_path)
    return dataclasses.replace(result, errors=errors + result.errors)


def export_sft_spool(
    spool_root: Path | str,
    out_path: Path | str,
    *,
    journey_id: Optional[str] = None,
) -> SftResult:
    """Fold straight from the spool and write an SFT file. Does not drain —
    no watermark moves, same as :func:`odyssey.export.export_spool`."""
    results, errors = _gather_from_spool(Path(spool_root), journey_id)
    result = save_sft(results, out_path)
    return dataclasses.replace(result, errors=errors + result.errors)
