"""Normalization — the first ``data_preparation`` stage: raw traces to
canonical ``Journey`` artifacts, via odyssey-core's own ``fold()`` and BYOD
builders. Closes ``docs/WORKING.md`` item 3.3.

Two raw shapes come in, one canonical shape goes out:

- **odyssey-shaped** — ``*.jsonl`` already in the wire format, drained from a
  spool or received by ``services/collector``. Coerced via
  ``odyssey.export.export_dir``, the read side of the event-sourced core.
- **BYOD-shaped** — a directory of ``*.json`` files holding a provider's own
  message format (OpenAI chat, Anthropic Messages, or Vercel AI SDK — the
  three ``odyssey.builders.messages`` adapters shaped as "one raw array in,
  one message list out", which is what makes them uniformly dispatchable by
  name here; ``messages_from_prompt_response``/``messages_from_role_content_pairs``
  take different arguments and are for direct use, not this stage). Parsed
  via the matching adapter, then assembled with ``build_journey_from_messages``.

Either way the job is exactly what ``docs/STRUCTURE.md`` names this stage
for: "schema coercion via odyssey-core fold; role/message canon form" — no
new parsing logic lives here, only the wiring that turns the existing,
already-tested engine into a batch stage over a directory of files.

Output is the same Trajectory JSON shape ``odyssey.export.save()`` already
produces (via :func:`normalize_odyssey_dir`) or the equivalent for BYOD input
with no fold to diagnose (via :func:`normalize_byod_dir`) — the artifact
every later stage (cleaning, annotation, validation, splitting) will read.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from odyssey.builders.journey import build_journey_from_messages
from odyssey.builders.messages import (
    messages_from_anthropic_messages,
    messages_from_openai_chat,
    messages_from_vercel_ai_sdk,
)
from odyssey.export import _filename, journey_to_dict
from odyssey.fold import derive_trainable_status
from odyssey.primitives import Journey, Message

# Every format odyssey-core already ships a parser for. Each maps a raw
# messages array to `list[Message]`; adding a new BYOD format here is adding
# one entry, not new parsing logic — see the module docstring.
_PARSERS: Dict[str, Callable[[List[Any]], List[Message]]] = {
    "openai_chat": messages_from_openai_chat,
    "anthropic_messages": messages_from_anthropic_messages,
    "vercel_ai_sdk": messages_from_vercel_ai_sdk,
}


@dataclass(frozen=True)
class NormalizeResult:
    """What one normalization run did, including what it refused to write."""

    written: List[Path] = field(default_factory=list)
    incomplete: Dict[str, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def count(self) -> int:
        return len(self.written)


def normalize_odyssey_dir(
    events_dir: Path | str,
    out_dir: Path | str,
    *,
    journey_id: Optional[str] = None,
    indent: Optional[int] = 2,
) -> NormalizeResult:
    """Coerce already-drained odyssey ``*.jsonl`` into canonical artifacts.

    A thin rename of :func:`odyssey.export.export_dir` at this stage's own
    entry point — completeness diagnostics (``incomplete``, gaps, writer
    conflicts) still apply, because folding is still folding.
    """
    from odyssey.export import export_dir

    result = export_dir(events_dir, out_dir, journey_id=journey_id, indent=indent)
    return NormalizeResult(
        written=result.written, incomplete=result.incomplete, errors=result.errors
    )


def normalize_byod_dir(
    raw_dir: Path | str,
    out_dir: Path | str,
    *,
    format: str,
    data_source: str,
    indent: Optional[int] = 2,
) -> NormalizeResult:
    """Parse a directory of raw provider-format exports into canonical
    artifacts.

    Each ``*.json`` file in ``raw_dir`` is one conversation, either a bare
    array of raw provider messages or ``{"messages": [...], ...}`` with
    optional ``conversation_id``/``trace_id``/``task_metadata`` alongside it.
    The file's stem is the conversation id when none is given.

    Unlike :func:`normalize_odyssey_dir`, there is no fold here — a BYOD
    export carries no ``seq``/terminal-event concept to be incomplete about.
    A file either parses into a ``Journey`` or it is reported as an error;
    every parseable file is written.
    """
    parser = _PARSERS.get(format)
    if parser is None:
        raise ValueError(
            f"unknown format {format!r}; expected one of {sorted(_PARSERS)}"
        )

    src = Path(raw_dir)
    journeys: List[Journey] = []
    errors: List[str] = []

    for path in sorted(src.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                messages_raw = raw.get("messages")
                conversation_id = raw.get("conversation_id") or path.stem
                trace_id = raw.get("trace_id")
                task_metadata = raw.get("task_metadata")
            else:
                messages_raw = raw
                conversation_id = path.stem
                trace_id = None
                task_metadata = None
            if not isinstance(messages_raw, list):
                raise ValueError("expected a JSON array or {'messages': [...]}")

            messages = _label_trainable(parser(messages_raw))
            journeys.append(
                build_journey_from_messages(
                    messages,
                    conversation_id=str(conversation_id),
                    data_source=data_source,
                    trace_id=trace_id,
                    task_metadata=(
                        task_metadata if isinstance(task_metadata, dict) else None
                    ),
                )
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue

    written = _save_journeys(journeys, out_dir, indent=indent)
    return NormalizeResult(written=written, errors=errors)


def _label_trainable(messages: List[Message]) -> List[Message]:
    """Apply ``fold.py``'s own role-default labelling to BYOD messages.

    ``build_journey_from_messages`` does not run ``fold()`` — there is no
    event stream, so there is nothing to fold — which means every message
    would otherwise keep the dataclass default, ``not_trainable``, including
    the assistant's own replies. That defeats the field: it is what every
    later stage (annotation, splitting, an SFT/DPO exporter) filters
    training targets on. Reusing ``derive_trainable_status`` with an empty
    signal list is exactly "schema coercion via fold()" — no new labelling
    logic, the same rule an odyssey-recorded journey with no signals gets.
    """
    by_index = dict(enumerate(messages))
    statuses = derive_trainable_status(by_index, signals=[])
    return [
        dataclasses.replace(m, trainable_status=statuses[i])
        for i, m in enumerate(messages)
    ]


def _save_journeys(
    journeys: List[Journey], out_dir: Path | str, *, indent: Optional[int]
) -> List[Path]:
    """Write each ``Journey`` as ``{out_dir}/{conversation_id}.json``.

    Same atomic-write and filename-sanitisation discipline as
    :func:`odyssey.export.save`, reused directly (:func:`journey_to_dict`,
    :func:`odyssey.export._filename`) rather than re-derived — there is
    nothing here a fold produced, so :func:`odyssey.export.save` itself
    (which expects ``FoldResult``, not a bare ``Journey``) does not fit.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    written: List[Path] = []
    for journey in journeys:
        cid = journey.task.conversation_id or journey.task.id or "journey"
        path = out / _filename(cid)
        payload = journey_to_dict(journey)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, indent=indent, sort_keys=True) + "\n", encoding="utf-8"
        )
        tmp.replace(path)
        written.append(path)
    return written
