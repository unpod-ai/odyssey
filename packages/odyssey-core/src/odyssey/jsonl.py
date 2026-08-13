"""The versioned JSONL event format — the sole contract between projects.

superdialog writes it, odyssey reads it, and neither imports the other. That
makes this module's on-disk shape the actual interface, so it is explicit rather
than reflective: every nested type has a hand-written decoder that validates, and
an unknown MAJOR version refuses to parse rather than guessing.

File layout — a header line, then one event per line::

    {"odyssey_schema_version": "1.0"}
    {"journey_id": "j_1", "seq": 0, "kind": "message", ...}
    {"journey_id": "j_1", "seq": 1, "kind": "message", ...}

Two failure modes are first-class, because both happen in production:

- **a truncated final line** — the writer was killed mid-append. Every complete
  event before it is returned; the partial line is reported, not fatal.
- **one malformed line** — returned as a rejection with its line number while
  every other line is still parsed. One bad line never eats the file.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from odyssey.primitives import (
    SCHEMA_VERSION,
    JourneyEvent,
    Message,
    Reward,
    RewardComponent,
    Signal,
    Terminal,
    ToolCall,
    ToolDefinition,
    ToolResponse,
)

HEADER_KEY = "odyssey_schema_version"


class SchemaVersionError(ValueError):
    """The file declares a MAJOR version this build cannot parse."""


class MalformedHeaderError(ValueError):
    """The first line is not a readable odyssey header."""


@dataclass(frozen=True)
class Rejection:
    """One line that could not be turned into an event."""

    line_no: int
    reason: str
    raw: str = ""


@dataclass(frozen=True)
class ReadResult:
    schema_version: str
    events: List[JourneyEvent] = field(default_factory=list)
    rejections: List[Rejection] = field(default_factory=list)
    truncated_last_line: bool = False

    @property
    def rejected_count(self) -> int:
        return len(self.rejections)

    @property
    def clean(self) -> bool:
        return not self.rejections and not self.truncated_last_line


# ---------------------------------------------------------------------------
# Encode
# ---------------------------------------------------------------------------


def _strip_none(obj: Any) -> Any:
    """Drop None-valued keys so a line carries only what was actually set."""
    if isinstance(obj, dict):
        return {k: _strip_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [_strip_none(v) for v in obj]
    return obj


def encode_event(event: JourneyEvent) -> str:
    """One event as one JSON line (no trailing newline)."""
    return json.dumps(
        _strip_none(dataclasses.asdict(event)), separators=(",", ":"), sort_keys=True
    )


def header_line(version: str = SCHEMA_VERSION) -> str:
    return json.dumps({HEADER_KEY: version}, separators=(",", ":"))


def write_events(
    path: Path | str,
    events: Iterable[JourneyEvent],
    *,
    append: bool = False,
) -> int:
    """Write events as JSONL, emitting the header when creating the file.

    Returns the number of events written.
    """
    p = Path(path)
    exists = p.exists() and p.stat().st_size > 0
    mode = "a" if append and exists else "w"
    n = 0
    with p.open(mode, encoding="utf-8") as fh:
        if mode == "w":
            fh.write(header_line() + "\n")
        for e in events:
            fh.write(encode_event(e) + "\n")
            n += 1
    return n


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------


def _major(version: str) -> int:
    try:
        return int(str(version).split(".", 1)[0])
    except (ValueError, AttributeError) as exc:
        raise SchemaVersionError(f"unparseable schema version {version!r}") from exc


def _check_version(version: str) -> str:
    if _major(version) != _major(SCHEMA_VERSION):
        raise SchemaVersionError(
            f"file declares schema version {version!r}; this build reads "
            f"major {_major(SCHEMA_VERSION)} (v{SCHEMA_VERSION}). Refusing to "
            f"parse rather than risk misreading events."
        )
    return str(version)


def _tool_call(d: Dict[str, Any]) -> ToolCall:
    return ToolCall(name=d["name"], arguments=d.get("arguments") or {}, id=d.get("id"))


def _tool_response(d: Dict[str, Any]) -> ToolResponse:
    return ToolResponse(
        id=d["id"],
        name=d["name"],
        arguments=d.get("arguments") or {},
        response=d.get("response"),
        error=d.get("error"),
        metadata=d.get("metadata"),
    )


def _tool_definition(d: Dict[str, Any]) -> ToolDefinition:
    return ToolDefinition(
        name=d["name"],
        description=d.get("description", ""),
        parameters=d.get("parameters") or {},
    )


def _reward_component(d: Dict[str, Any]) -> RewardComponent:
    rng: Optional[Tuple[float, float]] = None
    raw = d.get("range")
    if raw is not None:
        # JSON has no tuple; the dataclass is typed Tuple, so restore it.
        rng = (float(raw[0]), float(raw[1]))
    return RewardComponent(
        name=d["name"],
        value=float(d["value"]),
        scaled_value=d.get("scaled_value"),
        explanation=d.get("explanation"),
        weight=float(d.get("weight", 1.0)),
        range=rng,
        metadata=d.get("metadata"),
    )


def _reward(d: Dict[str, Any]) -> Reward:
    comps = d.get("components")
    return Reward(
        aggregated_value=d.get("aggregated_value"),
        aggregation_method=d.get("aggregation_method"),
        components=[_reward_component(c) for c in comps] if comps else None,
    )


def _message(d: Dict[str, Any]) -> Message:
    calls = d.get("tool_calls")
    defs = d.get("tool_definitions")
    resp = d.get("tool_response")
    return Message(
        role=d["role"],
        content=d.get("content"),
        tool_calls=[_tool_call(c) for c in calls] if calls else None,
        tool_response=_tool_response(resp) if resp else None,
        tool_definitions=[_tool_definition(t) for t in defs] if defs else None,
        usage=d.get("usage"),
        finish_reason=d.get("finish_reason"),
        metadata=d.get("metadata"),
        reasoning=d.get("reasoning"),
        trainable_status=d.get("trainable_status", "not_trainable"),
    )


def decode_event(d: Dict[str, Any]) -> JourneyEvent:
    """Rebuild a JourneyEvent from a decoded JSON object.

    Raises ``KeyError``/``ValueError`` on anything malformed — the caller turns
    that into a :class:`Rejection` rather than letting it abort the read.
    """
    sig = d.get("signal")
    term = d.get("terminal")
    rew = d.get("reward")
    msg = d.get("message")
    return JourneyEvent(
        journey_id=d["journey_id"],
        seq=int(d["seq"]),
        kind=d["kind"],
        ts=d.get("ts") or "",
        event_id=d["event_id"],
        message=_message(msg) if msg else None,
        signal=(
            Signal(
                signal=sig["signal"],
                target_seq=int(sig["target_seq"]),
                regen_order=sig.get("regen_order"),
                edited_output=sig.get("edited_output"),
            )
            if sig
            else None
        ),
        reward=_reward(rew) if rew else None,
        terminal=(
            Terminal(
                termination_reason=term.get("termination_reason", "NONE"),
                error=term.get("error"),
            )
            if term
            else None
        ),
        model_id=d.get("model_id"),
        metadata=d.get("metadata"),
    )


def read_schema_version(path: Path | str) -> str:
    """Read the declared version from the header alone, parsing no events."""
    with Path(path).open(encoding="utf-8") as fh:
        first = fh.readline()
    if not first.strip():
        raise MalformedHeaderError(f"{path}: file is empty, no header")
    try:
        obj = json.loads(first)
    except ValueError as exc:
        raise MalformedHeaderError(f"{path}: header is not valid JSON") from exc
    if not isinstance(obj, dict) or HEADER_KEY not in obj:
        raise MalformedHeaderError(f"{path}: first line is not an odyssey header")
    return str(obj[HEADER_KEY])


def read_events(path: Path | str) -> ReadResult:
    """Read a JSONL event file, tolerating truncation and bad lines."""
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    if not raw.strip():
        raise MalformedHeaderError(f"{p}: file is empty, no header")

    ends_clean = raw.endswith("\n")
    lines = raw.splitlines()

    version = _check_version(_header_from(lines[0], p))

    events: List[JourneyEvent] = []
    rejections: List[Rejection] = []
    truncated = False
    last_index = len(lines) - 1

    for i, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        is_last = (i - 1) == last_index
        try:
            obj = json.loads(line)
        except ValueError:
            # A partial final line with no trailing newline is a killed writer,
            # not corruption: report it and keep everything before it.
            if is_last and not ends_clean:
                truncated = True
                continue
            rejections.append(Rejection(i, "line is not valid JSON", line[:200]))
            continue
        if not isinstance(obj, dict):
            rejections.append(Rejection(i, "line is not a JSON object", line[:200]))
            continue
        try:
            events.append(decode_event(obj))
        except (KeyError, ValueError, TypeError) as exc:
            rejections.append(Rejection(i, f"{type(exc).__name__}: {exc}", line[:200]))

    return ReadResult(
        schema_version=version,
        events=events,
        rejections=rejections,
        truncated_last_line=truncated,
    )


def _header_from(line: str, p: Path) -> str:
    try:
        obj = json.loads(line)
    except ValueError as exc:
        raise MalformedHeaderError(f"{p}: header is not valid JSON") from exc
    if not isinstance(obj, dict) or HEADER_KEY not in obj:
        raise MalformedHeaderError(f"{p}: first line is not an odyssey header")
    return str(obj[HEADER_KEY])
