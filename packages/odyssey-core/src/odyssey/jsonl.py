"""The versioned JSONL event format — the sole contract between projects.

superdialog writes it, odyssey reads it, and neither imports the other. That
makes this module's on-disk shape the actual interface, so it is explicit rather
than reflective: every nested type has a hand-written decoder that validates, and
an unknown MAJOR version refuses to parse rather than guessing.

File layout — a header line, then one event per line::

    {"odyssey_schema_version": "2.0", "journey_id": "j_1", "data_source": "livekit",
     "trace_id": "t_9", "started_at": "...", "journey_metadata": {"tenant": "acme"}}
    {"journey_id": "j_1", "seq": 0, "kind": "message", ...}
    {"journey_id": "j_1", "seq": 1, "kind": "message", ...}

The header states what the events below it are a recording of, once. Anything
constant for the whole journey belongs there rather than on every event: a v1.0
file repeating its own tags N times still could not say what it was, and paid for
the repetition on every line.

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
    JourneyHeader,
    Message,
    Reward,
    RewardComponent,
    Signal,
    Terminal,
    ToolCall,
    ToolDefinition,
    ToolResponse,
    VoiceEvent,
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
    # The parsed header. On a v1.0 file every field but the version is None —
    # which is exactly the condition a caller needs to detect to know it must
    # supply `data_source` itself.
    header: JourneyHeader = field(default_factory=JourneyHeader)

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


# Message fields the fold derives, paired with the value that means "the writer
# had nothing to say". Only that value is dropped on encode — see `encode_event`.
_DERIVED_MESSAGE_DEFAULTS = {"trainable_status": "not_trainable"}


def encode_event(event: JourneyEvent) -> str:
    """One event as one JSON line (no trailing newline).

    A ``message.trainable_status`` still sitting at the writer default is dropped
    on the way out. ``fold.derive_trainable_status`` assigns the label from role,
    signals and structural flags, so the default carries no information — and
    emitting it on every line makes the file read as a dead corpus to anyone who
    opens it, while the folded journey says the opposite.

    A producer that set the field deliberately keeps it. The decoder defaults the
    absent field back to ``not_trainable``, so dropping the default is
    round-trip-lossless in both directions; dropping an explicit value would not
    be.
    """
    d = _strip_none(dataclasses.asdict(event))
    msg = d.get("message")
    if isinstance(msg, dict):
        for name, default in _DERIVED_MESSAGE_DEFAULTS.items():
            if msg.get(name) == default:
                del msg[name]
    return json.dumps(d, separators=(",", ":"), sort_keys=True)


def header_line(
    version: str = SCHEMA_VERSION,
    header: Optional[JourneyHeader] = None,
) -> str:
    """The first line of a shard.

    ``header`` carries the journey identity; None-valued fields are omitted, so
    a caller with nothing to declare still writes the bare v1.0-shaped line.
    ``version`` always wins over ``header.odyssey_schema_version`` — the writer
    decides what it is writing, not its payload.
    """
    obj: Dict[str, Any] = {HEADER_KEY: version}
    if header is not None:
        for name, value in dataclasses.asdict(header).items():
            if name == "odyssey_schema_version" or value is None:
                continue
            obj[name] = value
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


def decode_header(d: Dict[str, Any]) -> JourneyHeader:
    """Rebuild a :class:`JourneyHeader` from a decoded header object.

    Unknown keys are ignored rather than rejected: a MINOR bump is allowed to
    add them, and refusing here would make every forward-compatible file
    unreadable to the build that predates it.
    """
    meta = d.get("journey_metadata")
    return JourneyHeader(
        odyssey_schema_version=str(d[HEADER_KEY]),
        journey_id=d.get("journey_id"),
        data_source=d.get("data_source"),
        trace_id=d.get("trace_id"),
        started_at=d.get("started_at"),
        journey_metadata=meta if isinstance(meta, dict) else None,
    )


def write_events(
    path: Path | str,
    events: Iterable[JourneyEvent],
    *,
    append: bool = False,
    header: Optional[JourneyHeader] = None,
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
            fh.write(header_line(header=header) + "\n")
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
    voice = d.get("voice")
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
        voice=(
            VoiceEvent(
                voice_kind=voice["voice_kind"],
                text=voice.get("text"),
                confidence=voice.get("confidence"),
                latency_ms=voice.get("latency_ms"),
                metadata=voice.get("metadata"),
            )
            if voice
            else None
        ),
        model_id=d.get("model_id"),
        metadata=d.get("metadata"),
    )


def read_schema_version(path: Path | str) -> str:
    """Read the declared version from the header alone, parsing no events.

    Unlike :func:`read_header` this does not check the version, so a caller can
    ask what a file claims to be before deciding whether it can read it.
    """
    with Path(path).open(encoding="utf-8") as fh:
        first = fh.readline()
    if not first.strip():
        raise MalformedHeaderError(f"{path}: file is empty, no header")
    return str(_header_from(first, Path(path))[HEADER_KEY])


def read_events(path: Path | str) -> ReadResult:
    """Read a JSONL event file, tolerating truncation and bad lines."""
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    if not raw.strip():
        raise MalformedHeaderError(f"{p}: file is empty, no header")

    ends_clean = raw.endswith("\n")
    lines = raw.splitlines()

    header = decode_header(_header_from(lines[0], p))
    version = _check_version(header.odyssey_schema_version)

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
        header=header,
    )


def read_header(path: Path | str) -> JourneyHeader:
    """Parse the header alone, reading no events.

    What a drain needs: the sink writes a new file and has to reproduce the
    identity of the shard the events came from, without paying to parse a
    journey that may be thousands of events long.
    """
    with Path(path).open(encoding="utf-8") as fh:
        first = fh.readline()
    if not first.strip():
        raise MalformedHeaderError(f"{path}: file is empty, no header")
    header = decode_header(_header_from(first, Path(path)))
    _check_version(header.odyssey_schema_version)
    return header


def _header_from(line: str, p: Path) -> Dict[str, Any]:
    """The header line as a dict, validated as an odyssey header.

    Returns the whole object rather than just the version: v1.1 put journey
    identity up here, and a reader that extracts one key and drops the rest
    leaves every consumer to be told out-of-band what the file it is holding
    actually is.
    """
    try:
        obj = json.loads(line)
    except ValueError as exc:
        raise MalformedHeaderError(f"{p}: header is not valid JSON") from exc
    if not isinstance(obj, dict) or HEADER_KEY not in obj:
        raise MalformedHeaderError(f"{p}: first line is not an odyssey header")
    return obj
