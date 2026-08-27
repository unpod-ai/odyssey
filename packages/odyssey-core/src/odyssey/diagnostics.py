"""Answering "is it actually recording?" without reading the source.

A capture layer that silently does nothing is its worst failure mode: the app
works, the tests pass, and six weeks later the corpus is empty. Everything here
exists to make that state visible.

Two views:

- :func:`report` — the live process: what ``init()`` resolved, what is buffered,
  and every failure that was swallowed.
- :func:`scan` — the spool on disk: per journey, whether it would survive a fold
  and why not if it would not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from odyssey.client import health as _client_health
from odyssey.fold import derive_trainable_status, fold
from odyssey.primitives import WRITER_META_KEY
from odyssey.spool import Spool, SpoolConfig


@dataclass
class JourneyReport:
    """What a fold would make of one journey, without exporting it."""

    journey_id: str
    events: int
    kinds: Dict[str, int] = field(default_factory=dict)
    highest_seq: Optional[int] = None
    missing_seqs: List[int] = field(default_factory=list)
    writers: List[str] = field(default_factory=list)
    duplicates_dropped: int = 0
    terminated: bool = False
    trainable: bool = False
    undrained: int = 0
    watermark: Optional[int] = None
    problem: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "journey_id": self.journey_id,
            "events": self.events,
            "kinds": self.kinds,
            "highest_seq": self.highest_seq,
            "missing_seqs": self.missing_seqs,
            "writers": self.writers,
            "duplicates_dropped": self.duplicates_dropped,
            "terminated": self.terminated,
            "trainable": self.trainable,
            "undrained": self.undrained,
            "watermark": self.watermark,
            "problem": self.problem,
        }


def report() -> Dict[str, Any]:
    """Live process state, including swallowed failures. Never raises."""
    return _client_health()


def scan(
    spool_dir: Path | str, *, journey_id: Optional[str] = None
) -> List[JourneyReport]:
    """Fold every journey in a spool and report what it would produce.

    Read-only: nothing is drained, no watermark moves. Safe to run against a
    spool a live process is writing to.
    """
    spool = Spool(SpoolConfig(root=Path(spool_dir)))
    targets = [journey_id] if journey_id else spool.journey_ids()
    out: List[JourneyReport] = []

    for jid in targets:
        events = spool.read(jid)
        kinds: Dict[str, int] = {}
        for e in events:
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
        writers = sorted(
            {
                w
                for w in ((e.metadata or {}).get(WRITER_META_KEY) for e in events)
                if isinstance(w, str)
            }
        )
        entry = JourneyReport(
            journey_id=jid,
            events=len(events),
            kinds=kinds,
            highest_seq=max((e.seq for e in events), default=None),
            writers=writers,
            undrained=len(spool.undrained(jid)),
            watermark=spool.watermark(jid),
        )
        if not events:
            entry.problem = "no events on disk"
            out.append(entry)
            continue
        try:
            result = fold(events, data_source="diagnostics")
        except ValueError as exc:
            entry.problem = f"unfoldable: {exc}"
            out.append(entry)
            continue
        entry.missing_seqs = result.missing_seqs
        entry.duplicates_dropped = result.duplicates_dropped
        entry.terminated = result.terminated
        entry.trainable = result.trainable
        entry.problem = result.incomplete_reason
        out.append(entry)

    return out


def format_report(live: Dict[str, Any], journeys: List[JourneyReport]) -> str:
    """Human-readable rendering for the CLI. Machine callers use ``--json``."""
    lines: List[str] = []
    if not live.get("initialised"):
        lines.append("process:  odyssey.init() has not run in this process")
    else:
        stats = live.get("stats", {})
        lines.append(
            f"process:  writer={live.get('writer_id')} "
            f"enabled={live.get('enabled')} debug={live.get('debug')}"
        )
        lines.append(
            f"          spool={live.get('spool_dir')} out={live.get('out_dir')}"
        )
        lines.append(
            f"          recorded={stats.get('events_recorded', 0)} "
            f"dropped={stats.get('events_dropped', 0)} "
            f"errors={stats.get('capture_errors', 0)} "
            f"open_shards={live.get('open_shards', 0)}"
        )
        for err in stats.get("recent_errors", []):
            lines.append(f"  error   {err}")

    if not journeys:
        lines.append("spool:    empty")
        return "\n".join(lines)

    lines.append("")
    lines.append(
        f"{'journey':<34}{'events':>7}{'undrained':>10}"
        f"{'writers':>8}{'trainable':>10}  problem"
    )
    for j in journeys:
        lines.append(
            f"{j.journey_id:<34}{j.events:>7}{j.undrained:>10}"
            f"{len(j.writers):>8}{str(j.trainable):>10}  {j.problem or ''}"
        )

    conflicted = [j.journey_id for j in journeys if len(j.writers) > 1]
    if conflicted:
        lines.append("")
        lines.append(
            "WRITER CONFLICT: more than one process wrote "
            f"{', '.join(conflicted)}. seq is allocated per process, so these "
            "journeys interleave two conversations and are not exportable."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Looking at what was collected
#
# `report` and `scan` answer "is it working?". This answers the question a person
# actually has next: "show me what you captured, and tell me which of it a model
# would learn from." Without it there is no way to look at a corpus except
# reading raw JSONL, which is how a capture layer ends up trusted on faith.
# ---------------------------------------------------------------------------

_ROLE_MARK = {
    "system": "·",
    "user": ">",
    "assistant": "<",
    "tool": "=",
}


def _one_line(text: Optional[str], width: int) -> str:
    if not text:
        return ""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def render_journey(
    spool_dir: Path | str,
    journey_id: str,
    *,
    width: int = 74,
) -> str:
    """Render one collected journey as a conversation plus its training view."""
    spool = Spool(SpoolConfig(root=Path(spool_dir)))
    events = spool.read(journey_id)
    if not events:
        return f"journey {journey_id!r}: nothing on disk under {spool_dir}"

    result = fold(events, data_source="show")
    j = result.journey
    lines: List[str] = []

    # trainable_status is *derived*, not recorded: what is on disk is whatever the
    # producer set (usually the default), and the real label depends on signals
    # that arrive later. Recompute it the way fold() does, keyed by seq, or this
    # view would report every turn as not_trainable.
    statuses = derive_trainable_status(
        {e.seq: e.message for e in events if e.kind == "message" and e.message},
        result.signals,
    )

    verdict = "TRAINABLE" if result.trainable else "NOT TRAINABLE"
    model = j.model_id or (", ".join(result.model_ids) if result.model_ids else "—")
    lines.append(f"journey {journey_id}")
    lines.append(
        f"  {len(events)} events · {len(j.steps)} steps · model {model} · {verdict}"
    )
    if result.incomplete_reason:
        lines.append(f"  why not: {result.incomplete_reason}")
    lines.append("")

    # The conversation, in the order it happened.
    for e in events:
        tag = f"  {e.seq:>3} "
        if e.kind == "message" and e.message is not None:
            m = e.message
            mark = _ROLE_MARK.get(m.role, " ")
            body = _one_line(m.content, width)
            if m.tool_calls:
                calls = ", ".join(
                    f"{c.name}({_one_line(str(c.arguments), 40)})" for c in m.tool_calls
                )
                body = f"call {calls}"
            elif m.tool_response is not None:
                body = f"result {_one_line(str(m.tool_response.response), width - 8)}"
                if m.tool_response.error:
                    body = f"ERROR {m.tool_response.error}"
            status = statuses.get(e.seq, "not_trainable")
            star = "  ★ trainable" if status == "trainable" else ""
            if status not in ("trainable", "not_trainable"):
                star = f"  [{status}]"
            lines.append(f"{tag}{mark} {m.role:<10}{body}{star}")
            if m.reasoning:
                lines.append(f"      {'':<12}(reasoning: {_one_line(m.reasoning, 50)})")
        elif e.kind == "signal" and e.signal is not None:
            s = e.signal
            extra = f" order={s.regen_order}" if s.regen_order is not None else ""
            lines.append(f"{tag}! {'signal':<10}{s.signal} → seq {s.target_seq}{extra}")
        elif e.kind == "reward" and e.reward is not None:
            lines.append(
                f"{tag}$ {'reward':<10}{e.reward.aggregated_value} "
                f"({e.reward.aggregation_method})"
            )
        elif e.kind == "terminal" and e.terminal is not None:
            err = f" — {e.terminal.error}" if e.terminal.error else ""
            lines.append(f"{tag}. {'terminal':<10}{e.terminal.termination_reason}{err}")

    # The training view: what a corpus builder would take from this.
    trainable_steps = [s for s in j.steps if s.trainable_status == "trainable"]
    superseded = [seq for seq, st in sorted(statuses.items()) if st == "superseded"]
    rejected_by = [s.signal for s in result.signals if s.signal == "thumbs_down"]

    lines.append("")
    lines.append("training view")
    if not result.trainable:
        lines.append("  nothing exportable — the journey is not complete")
        return "\n".join(lines)

    lines.append(
        f"  SFT candidates : {len(trainable_steps)} "
        f"(a step's messages are the prompt, its last turn is the target)"
    )
    lines.append(
        f"  superseded     : {len(superseded)} turn(s) at seq {superseded} "
        f"— rejected side of a preference pair"
        if superseded
        else "  superseded     : 0 (no regeneration or edit in this journey)"
    )
    lines.append(f"  thumbs_down    : {len(rejected_by)}")
    if j.metrics is not None:
        lines.append(
            f"  reward         : {j.metrics.aggregated_reward} · "
            f"tool calls {j.metrics.num_tool_calls} · "
            f"failures {j.metrics.num_tool_failures}"
        )
    lines.append("  NOTE: 'odyssey sft'/'odyssey dpo' write these to a training file.")
    return "\n".join(lines)
