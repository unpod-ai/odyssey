"""Write folded journeys as Trajectory JSON — the artifact, not the transport.

odyssey has two on-disk shapes and they are not competing:

- ``jsonl.py`` — the **wire**. Append-only ``JourneyEvent`` lines, one per turn.
  ``Step[]`` is deliberately absent: a step holds the whole conversation up to
  its point, so shipping N cumulative steps costs O(N**2) bytes where shipping N
  events costs O(N). ``test_no_step_record_is_ever_encoded`` enforces that.
- this module — the **product**. One ``{conversation_id}.json`` per conversation,
  carrying ``task`` + ``steps`` + ``reward`` + metrics. That is the shape the
  Trajectory platform and a trainer consume, and it is what ``tj.save()``
  produces.

The events are how a conversation travels; this is what it becomes. Handing a
consumer the event stream and calling it the deliverable is the gap this closes
(``docs/WORKING.md`` item 5.4 — "Nothing converts a ``Journey`` into an SFT
file").

Field naming follows the **platform**, not odyssey, wherever the two disagree:
``reference_journey`` is written as ``reference_trajectory`` — a name the
platform owns keeps the platform's spelling, because the whole value of this
file is that something else can read it without a translation table.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from odyssey.fold import FoldResult, fold
from odyssey.jsonl import _strip_none, read_events
from odyssey.primitives import (
    SCHEMA_VERSION,
    Journey,
    JourneyEvent,
    JourneyHeader,
)

# odyssey's own name for a field the platform spells differently. Exported under
# the platform's spelling; see the module docstring.
_RENAMED = {"reference_journey": "reference_trajectory"}

# Diagnostics that describe the *recording*, not the conversation. Namespaced
# under one reserved key so a consumer reading the platform's schema sees only
# fields it declares, and everything odyssey adds is obviously additive.
#
# The leading underscore marks SDK-owned keys inside a user-facing object — the
# same convention as `_odyssey_writer` and `_odyssey_unparsed_arguments`.
DIAGNOSTICS_KEY = "_odyssey"


class ExportError(ValueError):
    """A journey could not be written."""


@dataclass(frozen=True)
class ExportResult:
    """What one export run did, including what it refused to write."""

    written: List[Path] = field(default_factory=list)
    incomplete: Dict[str, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def count(self) -> int:
        return len(self.written)


def journey_to_dict(
    journey: Journey,
    *,
    diagnostics: Optional[Dict[str, Any]] = None,
    last_step_only: bool = False,
) -> Dict[str, Any]:
    """One ``Journey`` as the platform's Trajectory object.

    ``None``-valued keys are dropped, so a file carries only what was actually
    recorded rather than a wall of nulls a reader has to skip past. ``False`` and
    ``0`` survive — only absence is absence.

    ``diagnostics`` lands under :data:`DIAGNOSTICS_KEY`. It is how an incomplete
    journey stays exportable while still being obviously incomplete: the caller
    chose to write it, and the file has to say so rather than passing for whole.

    ``last_step_only`` writes the final step alone. Every step is the whole
    conversation up to its own turn, so the last one already contains every
    message — including the tool calls and their results — and the preceding
    N-1 are prefixes of it. Keeping them all is what makes the file quadratic in
    turns: a 12-turn call is 54 KB of which 50 KB is the same messages written
    again. ``task.num_turns`` still reports the real turn count, and the
    diagnostics say the steps were trimmed, so nothing about the conversation is
    silently lost — only the redundant copies of it.
    """
    d = _strip_none(dataclasses.asdict(journey))
    for ours, theirs in _RENAMED.items():
        if ours in d:
            d[theirs] = d.pop(ours)
    trimmed = False
    if last_step_only and len(d.get("steps") or ()) > 1:
        d["steps"] = d["steps"][-1:]
        trimmed = True
    if diagnostics or trimmed:
        diag = dict(diagnostics or {})
        if trimmed:
            # A consumer counting `steps` must be able to tell a one-turn call
            # from a trimmed twelve-turn one.
            diag["steps_written"] = "last"
        d[DIAGNOSTICS_KEY] = diag
    return d


def _diagnostics(result: FoldResult, schema_version: str) -> Dict[str, Any]:
    """Everything the fold learned that the Trajectory schema has no field for.

    ``complete`` is always written, including when it is ``True``. A flag that
    appears only on failure is one a consumer forgets to check — the absent key
    and the healthy key look identical to code that never saw a bad file.
    """
    diag: Dict[str, Any] = {
        "schema_version": schema_version,
        "journey_id": result.journey_id,
        "complete": result.complete,
        "terminated": result.terminated,
    }
    if result.incomplete_reason:
        diag["incomplete_reason"] = result.incomplete_reason
    if result.missing_seqs:
        diag["missing_seqs"] = result.missing_seqs
    if result.duplicates_dropped:
        diag["duplicates_dropped"] = result.duplicates_dropped
    if result.rejected_after_terminal:
        diag["rejected_after_terminal"] = result.rejected_after_terminal
    if len(result.writers) > 1:
        # The one diagnostic that means "do not train on this". `seq` is
        # allocated per process, so two writers issued the same numbers and the
        # journey is a silent interleaving of two conversations.
        diag["writers"] = result.writers
    if result.model_ids:
        diag["model_ids"] = result.model_ids
    return diag


def voice_provenance(result: FoldResult) -> Optional[Dict[str, Any]]:
    """Per-stage provider name and latency stats from one journey's voice events.

    ``FoldResult.voice_events`` is deliberately kept out of :func:`journey_to_dict`
    — an SFT/DPO example has nothing to do with a latency reading (see fold.py).
    A deployment that still wants "which STT/TTS/LLM vendor served this call and
    how slow was each" reads it from here instead, opted into the artifact's
    diagnostics rather than forced onto every export.

    Grouped by ``metadata["stage"]`` (``llm``/``tts``/``stt``/``eou`` — see
    ``integrations/livekit.py``'s ``_METRIC_STAGES``, the source every producer
    of these events currently shares). A stage a producer never reported is
    simply absent from the result, not zeroed — ``None`` means "no voice events
    on this journey at all", an empty dict never comes back.
    """
    if not result.voice_events:
        return None
    by_stage: Dict[str, Dict[str, Any]] = {}
    for ev in result.voice_events:
        meta = ev.metadata or {}
        stage = meta.get("stage")
        if not stage:
            continue
        bucket = by_stage.setdefault(stage, {"label": meta.get("label"), "latencies_ms": []})
        if ev.latency_ms is not None:
            bucket["latencies_ms"].append(ev.latency_ms)

    provenance: Dict[str, Any] = {}
    for stage, bucket in by_stage.items():
        latencies = bucket["latencies_ms"]
        entry: Dict[str, Any] = {}
        if bucket["label"]:
            entry["provider"] = bucket["label"]
        if latencies:
            entry["calls"] = len(latencies)
            entry["avg_latency_ms"] = round(sum(latencies) / len(latencies), 1)
            entry["min_latency_ms"] = round(min(latencies), 1)
            entry["max_latency_ms"] = round(max(latencies), 1)
        provenance[stage] = entry
    return provenance or None


def _enrich_llm_provenance(
    provenance: Dict[str, Any], llm_result: Optional[FoldResult]
) -> None:
    """Fold a linked ``<journey_id>.llm`` sub-journey's model id and token usage
    into ``provenance["llm"]``, in place.

    Opened by ``attach(record_provider_calls=True)``, that sub-journey is where
    the request/response detail of each provider call actually lives — the
    voice-side ``llm`` stage :func:`voice_provenance` already found only carries
    time-to-first-token, never which model answered or how many tokens it cost.
    ``gateway`` (the linked journey's ``Message.provider``) is kept separate
    from the voice stage's ``provider`` (the plugin's own class name) rather
    than overwriting it — a deployment behind an inference gateway wants both:
    which plugin measured the call and which backend actually served it.

    Best-effort and additive only: a call with no linked journey — provider
    recording turned off, or none of this call's turns went through one —
    leaves ``provenance`` exactly as :func:`voice_provenance` built it.
    """
    if llm_result is None:
        return
    entry = provenance.setdefault("llm", {})
    if llm_result.model_ids:
        entry["model_ids"] = sorted(set(llm_result.model_ids))

    steps = llm_result.journey.steps
    # Cumulative steps: the last one already holds every turn — see
    # `journey_to_dict`'s `last_step_only` docstring for why.
    messages = steps[-1].messages if steps else []
    usage_totals: Dict[str, int] = {}
    providers: set[str] = set()
    for m in messages:
        if m.role != "assistant":
            continue
        if m.provider:
            providers.add(m.provider)
        for key, val in (m.usage or {}).items():
            if isinstance(val, (int, float)):
                usage_totals[key] = usage_totals.get(key, 0) + val
    if providers:
        entry["gateway"] = sorted(providers)[0] if len(providers) == 1 else sorted(providers)
    if usage_totals:
        entry["token_usage_total"] = usage_totals


# Leave room for the ``.json`` suffix and the ``.tmp`` the atomic write adds,
# inside the 255-byte limit every common filesystem enforces on one name.
_MAX_STEM = 240


def _filename(conversation_id: str) -> str:
    """A conversation id as a flat filename.

    Journey ids are caller-chosen — a room name, a call id, whatever the platform
    handed the app — and nothing stops one holding a separator. Written naively,
    ``a/b`` silently creates a subdirectory and ``../../etc/passwd`` escapes the
    output directory entirely.

    Traversal segments are dropped rather than escaped: ``..`` mangled into
    ``_.._`` is contained but unreadable, and the name is the only thing
    identifying the file to whoever opens the directory.
    """
    segments = [
        seg
        for seg in conversation_id.strip().replace("\\", "/").split("/")
        if seg and seg not in (".", "..")
    ]
    # A leading dot would hide the artifact from a plain `ls` and from most
    # glob patterns — including this module's own `*.jsonl` scan on the way in.
    stem = "_".join(segments).lstrip(".")[:_MAX_STEM]
    return f"{stem or 'journey'}.json"


def save(
    results: Iterable[FoldResult],
    out_dir: Path | str,
    *,
    schema_version: str = SCHEMA_VERSION,
    indent: Optional[int] = 2,
    last_step_only: bool = False,
    include_voice_provenance: bool = False,
    llm_results: Optional[Dict[str, FoldResult]] = None,
) -> ExportResult:
    """Write each folded journey as ``{out_dir}/{conversation_id}.json``.

    Incomplete journeys are written too, carrying their reason under
    :data:`DIAGNOSTICS_KEY`, and are also reported in
    :attr:`ExportResult.incomplete` so a caller that wants the stricter policy
    can act without re-reading the files.

    Indented by default: this is the artifact a human opens to check the work,
    and the compactness that matters on the wire buys nothing here. Pass
    ``indent=None`` for a machine-only export.

    ``last_step_only`` writes only the final, complete step of each journey —
    see :func:`journey_to_dict`.

    ``include_voice_provenance`` adds a ``voice_provenance`` block under
    :data:`DIAGNOSTICS_KEY` — per-stage STT/TTS/LLM/EOU provider name and
    latency, built by :func:`voice_provenance` from each result's own
    ``voice_events``. Off by default, matching every other diagnostic that
    grows the file: a caller opts in rather than every export paying for it.

    ``llm_results`` enriches that block further, keyed by the *main* journey's
    id: when a caller already has the linked ``<journey_id>.llm`` sub-journey's
    :class:`FoldResult` folded (:func:`export_dir`/:func:`export_spool` do this
    for you), :func:`_enrich_llm_provenance` adds its model id and token usage.
    Ignored when ``include_voice_provenance`` is ``False``.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    written: List[Path] = []
    incomplete: Dict[str, str] = {}
    errors: List[str] = []

    for result in results:
        cid = result.journey.task.conversation_id or result.journey_id
        path = out / _filename(cid)
        try:
            diag = _diagnostics(result, schema_version)
            if include_voice_provenance:
                provenance = voice_provenance(result)
                if provenance is not None:
                    _enrich_llm_provenance(
                        provenance, (llm_results or {}).get(result.journey_id)
                    )
                    diag["voice_provenance"] = provenance
            payload = journey_to_dict(
                result.journey,
                diagnostics=diag,
                last_step_only=last_step_only,
            )
            # Written whole, then moved into place: a reader watching this
            # directory must never pick up half a file, and an export
            # interrupted partway must not leave one behind.
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, indent=indent, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)
        except (OSError, TypeError, ValueError) as exc:
            errors.append(f"{cid}: {type(exc).__name__}: {exc}")
            continue
        written.append(path)
        if result.incomplete_reason:
            incomplete[cid] = result.incomplete_reason

    return ExportResult(written=written, incomplete=incomplete, errors=errors)


def fold_shard(path: Path | str, **fold_kwargs: Any) -> FoldResult:
    """Read one JSONL file and fold it, taking identity from its own header.

    The payoff of the v1.1 header. ``fold()`` needs ``data_source`` and used to
    make the caller supply it, so two callers could fold one file into two
    differently-labelled journeys and neither was wrong. Explicit kwargs still
    win, for a v1.0 file whose header has nothing to say.
    """
    result = read_events(path)
    if not result.events:
        raise ExportError(f"{path}: no events to fold")
    return _fold_with_header(result.events, result.header, fold_kwargs)


def _fold_with_header(
    events: List[JourneyEvent],
    header: JourneyHeader,
    overrides: Dict[str, Any],
) -> FoldResult:
    """Fold events, taking every label the header can supply.

    The payoff of the v1.1 header. ``fold()`` needs ``data_source`` and used to
    make the caller supply it, so two callers could fold one file into two
    differently-labelled journeys and neither was wrong. Explicit overrides still
    win, for a v1.0 file whose header has nothing to say.
    """
    kwargs: Dict[str, Any] = {
        "data_source": header.data_source or "unknown",
        "conversation_id": header.journey_id,
        "trace_id": header.trace_id,
        "start_time": header.started_at,
    }
    if header.journey_metadata:
        meta = dict(header.journey_metadata)
        # Two homes, on purpose. The builder pulls `num_turns`/`total_tokens`/
        # `total_cost` out of `task_metadata` and ignores everything else, so the
        # same dict also goes to `extra_telemetry` — otherwise every deployment
        # tag the header carried would be dropped on the way into the artifact.
        kwargs["task_metadata"] = meta
        kwargs["extra_telemetry"] = meta
    kwargs.update({k: v for k, v in overrides.items() if v is not None})
    return fold(events, **kwargs)


def _gather_from_dir(
    events_dir: Path, journey_id: Optional[str]
) -> Tuple[List[FoldResult], List[str]]:
    """Fold every drained ``*.jsonl`` in a directory. Shared by every exporter
    that reads drained output — Trajectory JSON, SFT, DPO — so there is one
    "one bad shard must not abort the run" policy, not one per exporter.
    """
    shards = (
        [events_dir / f"{journey_id}.jsonl"]
        if journey_id
        else sorted(events_dir.glob("*.jsonl"))
    )
    results: List[FoldResult] = []
    errors: List[str] = []
    for shard in shards:
        try:
            results.append(fold_shard(shard))
        except (OSError, ValueError) as exc:
            # One unreadable shard must not abort the run: the other journeys in
            # this directory are fine and a partial export beats none.
            errors.append(f"{shard.name}: {type(exc).__name__}: {exc}")
    return results, errors


def _gather_from_spool(
    spool_root: Path, journey_id: Optional[str]
) -> Tuple[List[FoldResult], List[str]]:
    """Fold straight from the spool. The read side of :func:`_gather_from_dir`
    for "show me the artifact for the call I just recorded" — the spool is not
    a flat directory, it nests one directory per journey and rotates shards
    inside it, so only ``Spool.read`` knows how to reassemble one.
    """
    from odyssey.spool import Spool, SpoolConfig

    spool = Spool(SpoolConfig(root=spool_root))
    targets = [journey_id] if journey_id else spool.journey_ids()

    results: List[FoldResult] = []
    errors: List[str] = []
    for jid in targets:
        try:
            events = spool.read(jid)
            if not events:
                raise ExportError(f"{jid}: no events to fold")
            header = spool.header(jid) or JourneyHeader(journey_id=jid)
            results.append(_fold_with_header(events, header, {}))
        except (OSError, ValueError) as exc:
            # One unfoldable journey must not abort the run — the rest of the
            # spool is fine, and a partial export beats none.
            errors.append(f"{jid}: {type(exc).__name__}: {exc}")
    return results, errors


def _gather_linked_llm_dir(
    events_dir: Path, results: List[FoldResult]
) -> Dict[str, FoldResult]:
    """Best-effort fold of each result's ``<journey_id>.llm`` sibling shard, if
    one was drained into the same directory.

    Enrichment, not a required part of the export — a missing or unfoldable
    sibling is silently skipped rather than turned into an error the caller
    only wanted a latency summary from.
    """
    linked: Dict[str, FoldResult] = {}
    for result in results:
        sibling = events_dir / f"{result.journey_id}.llm.jsonl"
        if not sibling.exists():
            continue
        try:
            linked[result.journey_id] = fold_shard(sibling)
        except (OSError, ValueError):
            continue
    return linked


def export_dir(
    events_dir: Path | str,
    out_dir: Path | str,
    *,
    journey_id: Optional[str] = None,
    indent: Optional[int] = 2,
    last_step_only: bool = False,
    include_voice_provenance: bool = False,
) -> ExportResult:
    """Fold every drained ``*.jsonl`` in a directory and write Trajectory JSON.

    The second half of the pipeline the CLI exposes: ``odyssey push`` drains the
    spool into events, and this turns those events into the artifact. Kept
    separate because they fail differently — a drain that cannot reach its sink
    is retried, an export that cannot fold is a data problem.

    ``include_voice_provenance`` also looks for each journey's ``.llm`` sibling
    shard in ``events_dir`` and folds it in — see :func:`save`.
    """
    results, errors = _gather_from_dir(Path(events_dir), journey_id)
    llm_results = (
        _gather_linked_llm_dir(Path(events_dir), results)
        if include_voice_provenance
        else None
    )
    result = save(
        results,
        out_dir,
        indent=indent,
        last_step_only=last_step_only,
        include_voice_provenance=include_voice_provenance,
        llm_results=llm_results,
    )
    return dataclasses.replace(result, errors=errors + result.errors)


def _gather_linked_llm_spool(
    spool_root: Path, results: List[FoldResult]
) -> Dict[str, FoldResult]:
    """Best-effort fold of each result's ``<journey_id>.llm`` sibling journey, if
    ``attach(record_provider_calls=True)`` opened one for this call.

    Enrichment, not a required part of the export — a missing or unfoldable
    sibling is silently skipped rather than turned into an error the caller
    only wanted a latency summary from.
    """
    from odyssey.spool import Spool, SpoolConfig

    spool = Spool(SpoolConfig(root=spool_root))
    linked: Dict[str, FoldResult] = {}
    for result in results:
        jid = f"{result.journey_id}.llm"
        try:
            events = spool.read(jid)
            if not events:
                continue
            header = spool.header(jid) or JourneyHeader(journey_id=jid)
            linked[result.journey_id] = _fold_with_header(events, header, {})
        except (OSError, ValueError):
            continue
    return linked


def export_spool(
    spool_root: Path | str,
    out_dir: Path | str,
    *,
    journey_id: Optional[str] = None,
    indent: Optional[int] = 2,
    last_step_only: bool = False,
    include_voice_provenance: bool = False,
) -> ExportResult:
    """Fold straight from the spool and write Trajectory JSON.

    The shortest question a developer asks — "show me the artifact for the call I
    just recorded" — and the one :func:`export_dir` cannot answer, because the
    spool is not a flat directory of files. It nests one directory per journey
    and rotates shards inside it, so a journey long enough to rotate lives in
    several files that only ``Spool.read`` knows how to reassemble.

    Reading the spool does **not** drain it: no watermark moves, so a later
    ``push`` still ships every event. Exporting is a view, not a consumption.

    ``include_voice_provenance`` also reads each journey's ``<journey_id>.llm``
    sub-journey straight from the spool and folds it in — see :func:`save`.
    """
    results, errors = _gather_from_spool(Path(spool_root), journey_id)
    llm_results = (
        _gather_linked_llm_spool(Path(spool_root), results)
        if include_voice_provenance
        else None
    )
    result = save(
        results,
        out_dir,
        indent=indent,
        last_step_only=last_step_only,
        include_voice_provenance=include_voice_provenance,
        llm_results=llm_results,
    )
    return dataclasses.replace(result, errors=errors + result.errors)
