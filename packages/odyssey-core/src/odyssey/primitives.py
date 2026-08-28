"""Journey schema types and provider-agnostic intermediates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

# ---------------------
# Type aliases
# ---------------------

TrainableStatus = Literal[
    "trainable", "not_trainable", "superseded", "summarization_boundary"
]
Role = Literal["system", "user", "assistant", "tool"]
TerminationReason = Literal[
    "TIMEOUT", "ENV_DONE", "MAX_STEPS", "TRUNCATION", "STALE", "ERROR", "NONE"
]
PiiRule = Literal["EMAIL", "PHONE", "CREDIT_CARD", "SSN"]


# ---------------------
# Journey schema
# ---------------------


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Dict[str, Any]
    id: Optional[str] = None


@dataclass(frozen=True)
class ToolResponse:
    id: str
    name: str
    arguments: Dict[str, Any]
    response: Any = None
    error: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: Dict[str, Any]


@dataclass(frozen=True)
class Message:
    role: Role
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_response: Optional[ToolResponse] = None
    tool_definitions: Optional[List[ToolDefinition]] = None
    usage: Optional[Dict[str, int]] = None
    finish_reason: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    reasoning: Optional[str] = None
    trainable_status: TrainableStatus = "not_trainable"

    def is_empty(self) -> bool:
        return (
            not self.content and self.tool_calls is None and self.tool_response is None
        )


@dataclass(frozen=True)
class RewardComponent:
    name: str
    value: float
    scaled_value: Optional[float] = None
    explanation: Optional[str] = None
    weight: float = 1.0
    range: Optional[Tuple[float, float]] = None
    metadata: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class Reward:
    aggregated_value: Optional[float] = None
    aggregation_method: Optional[str] = None
    components: Optional[List[RewardComponent]] = None


@dataclass(frozen=True)
class Step:
    messages: List[Message]  # CUMULATIVE
    reward: Optional[Reward] = None
    trainable_status: TrainableStatus = "not_trainable"
    # `info` dropped on port: upstream declared it and never assigned it, and
    # nothing read it. See design.md Decision 4 (dead fields).


@dataclass(frozen=True)
class JourneyMetrics:
    steps: Optional[int] = None
    tokens_generated: Optional[int] = None
    aggregated_reward: Optional[float] = None
    num_tool_calls: Optional[int] = None
    num_tool_failures: Optional[int] = None
    num_tool_response_none: Optional[int] = None
    tool_error_rate: Optional[float] = None


@dataclass(frozen=True)
class ExecutionMetrics:
    total_time: Optional[float] = None
    termination_reason: Optional[TerminationReason] = None
    # `env_time` / `llm_time` dropped on port: never assigned upstream, and
    # odyssey has neither an environment nor per-call timing to source them
    # from. Re-add them when something can actually populate them.


@dataclass(frozen=True)
class Telemetry:
    source: str
    data: Dict[str, Any]


@dataclass(frozen=True)
class TelemetryEvent:
    """A single product telemetry event for the push_events() pipeline.

    Mirrors the backend's TelemetryEventCreateModel. The event_id UUID serves
    as the idempotency key -- partners can set it deterministically (e.g. hash
    their own primary key) so re-pushes never produce duplicates.
    """

    event_type: str
    session_id: str
    properties: Dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: __import__("uuid").uuid4().hex)
    timestamp: str = field(
        default_factory=lambda: (
            __import__("datetime")
            .datetime.now(__import__("datetime").timezone.utc)
            .isoformat()
        )
    )
    user_id: Optional[str] = None
    # `trajectory_id`, not `journey_id`: this is the backend's own id for the
    # persisted row, returned by upload and stamped back onto buffered events.
    # The field name is the platform's, so it stays the platform's spelling even
    # though everything odyssey owns says "journey".
    trajectory_id: Optional[str] = None
    source: str = "sdk"
    metadata: Optional[Dict[str, Any]] = None
    trace_id: Optional[str] = None

    def to_api_dict(self) -> Dict[str, Any]:
        """Serialize to the shape expected by POST /api/v1/telemetry/events."""
        d: Dict[str, Any] = {
            "event_type": self.event_type,
            "session_id": self.session_id,
            "properties": self.properties,
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "source": self.source,
        }
        if self.trace_id is not None:
            d["trace_id"] = self.trace_id
        if self.user_id is not None:
            d["user_id"] = self.user_id
        if self.trajectory_id is not None:
            d["trajectory_id"] = self.trajectory_id
        if self.metadata is not None:
            d["metadata"] = self.metadata
        return d


@dataclass(frozen=True)
class Task:
    id: Optional[str] = None
    data_source: Optional[str] = None
    conversation_id: Optional[str] = None
    num_turns: Optional[int] = None
    num_steps: Optional[int] = None
    total_tokens: Optional[int] = None
    total_cost: Optional[float] = None


@dataclass(frozen=True)
class Journey:
    task: Task
    steps: List[Step]
    reward: Optional[Reward] = None
    metrics: Optional[JourneyMetrics] = None
    execution_metrics: Optional[ExecutionMetrics] = None
    reference_journey: Optional[Dict[str, Any]] = None
    telemetry: Optional[Telemetry] = None
    idx: Optional[int] = None
    error: Optional[str] = None
    trace_id: Optional[str] = None
    model_id: Optional[str] = None


# ---------------------
# Provider intermediates
# ---------------------


@dataclass
class ParsedTurn:
    """One turn's worth of structured data, extracted from a provider trace."""

    messages: List[Message]
    source_run_id: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    error: Optional[str] = None
    token_counts: Dict[str, int] = field(default_factory=dict)
    total_cost: Optional[float] = None


@dataclass
class ParsedConversation:
    """A full conversation parsed from provider data. Fed into the builder."""

    conversation_id: str
    data_source: str
    num_turns: int
    turns: List[ParsedTurn]
    trace_id: Optional[str] = None


# ---------------------
# Public result types
# ---------------------


@dataclass(frozen=True)
class ConversationSummary:
    conversation_id: str
    num_turns: int
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    root_run_names: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class RedactionPreview:
    """Dry-run result from a PII redactor."""

    total_rule_counts: Dict[str, int]
    samples: List[Dict[str, Any]]


# ---------------------
# PII policy
# ---------------------


@dataclass(frozen=True)
class PiiPolicy:
    name: str
    rules: Sequence[PiiRule]


# =============================================================================
# Event-sourced layer.
#
# Everything BELOW this line is odyssey original work, not derived from
# trajectory-sdk. The boundary is load-bearing: the MIT attribution in NOTICE
# names this file as derived, and the unresolved copyright holder blocks public
# distribution of the derived half only.
# =============================================================================

# Bumped only for a breaking change to the on-the-wire event shape. The reader
# rejects an unknown MAJOR outright rather than mis-parsing (see jsonl.py).
#
# 1.1 — additive: the header line gained journey identity (`JourneyHeader`), and
# a `message.trainable_status` still at the writer default is no longer encoded.
# A 1.0 reader still parses a 1.1 file: the extra header keys are ignored and the
# absent label decodes back to its default. Hence MINOR, not MAJOR.
#
# 2.0 — breaking (item 0'.4): a new "voice" `EventKind` with its own payload
# field. A 1.x reader's `_PAYLOAD_FIELD`/kind-dispatch has no branch for
# "voice" at all, so it cannot merely ignore an unrecognized event the way a
# 1.0 reader ignored 1.1's new header keys — it would either drop real turns
# or raise. That is what makes this MAJOR rather than another additive MINOR,
# and why the reader in jsonl.py refuses to parse a 1.x shard's major version
# against a 2.x reader (or vice versa) instead of guessing. No migration tool
# ships with this bump; a 1.x shard on disk simply stops parsing.
SCHEMA_VERSION = "2.0"

EventKind = Literal["message", "signal", "reward", "terminal", "voice"]
SignalKind = Literal["thumbs_up", "thumbs_down", "regenerated", "user_edit"]
VoiceKind = Literal["stt_transcript", "tts_output", "barge_in", "latency"]

# Writer identity lives in ``JourneyEvent.metadata`` under this key rather than
# in a field of its own. That is what keeps ``SCHEMA_VERSION`` at 1.0 while still
# making a two-writer collision provable: ``seq`` is allocated per process, so
# two processes recording one journey would issue the same numbers, and the fold
# has to be able to tell that apart from a legitimate single-writer stream.
#
# The leading underscore marks the key as SDK-owned inside a user-facing dict.
WRITER_META_KEY = "_odyssey_writer"


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _new_event_id() -> str:
    from uuid import uuid4

    return uuid4().hex


@dataclass(frozen=True)
class JourneyHeader:
    """The first line of a shard: what the events below it are a recording of.

    v1.0 carried only the schema version, which left a file unable to say what
    it recorded. Everything ``fold`` needs to build a ``Task`` — ``data_source``
    for provenance, ``trace_id`` to correlate with telemetry — had to be supplied
    by whoever happened to call the reader, so two callers could fold one file
    into two differently-labelled journeys and neither was wrong. Identity
    belongs in the file that has it.

    Only fields that cannot change once recording starts live here. Mutable
    caller tags stay on the events, because a shard header is written before the
    second event exists and a later tag would have nowhere to land.
    """

    odyssey_schema_version: str = SCHEMA_VERSION
    journey_id: Optional[str] = None
    data_source: Optional[str] = None
    trace_id: Optional[str] = None
    started_at: Optional[str] = None
    # Snapshot of the journey-level tags as of the first recorded event. Held as
    # one nested object rather than spread across the header so a reader can tell
    # caller-supplied keys from schema-defined ones.
    journey_metadata: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class Signal:
    """Explicit human or system feedback about an earlier event.

    This is what makes preference training possible. ``Reward`` is a scalar
    judgement; DPO/KTO/ORPO need to know which of two outputs won, which is an
    *ordering* — hence ``regen_order`` and ``edited_output``.
    """

    signal: SignalKind
    target_seq: int
    regen_order: Optional[int] = None
    edited_output: Optional[str] = None


@dataclass(frozen=True)
class Terminal:
    """Closes a journey. No event with a higher ``seq`` is accepted after it."""

    termination_reason: TerminationReason = "NONE"
    error: Optional[str] = None


@dataclass(frozen=True)
class VoiceEvent:
    """A voice-modality signal alongside a turn's `Message` (item 0'.4).

    Deliberately narrow: this records what a voice integration (e.g.
    `integrations/livekit.py`) already observes about its own STT/TTS
    pipeline -- transcript confidence, barge-in, latency -- not a general
    audio/telephony schema. It carries no `trainable` notion of its own and
    is folded separately from `Journey.messages`/`Step[]` (see fold.py's
    `FoldResult.voice_events`), since an SFT/DPO export has nothing to do
    with a barge-in flag.
    """

    voice_kind: VoiceKind
    text: Optional[str] = None
    confidence: Optional[float] = None
    latency_ms: Optional[float] = None
    metadata: Optional[Dict[str, Any]] = None


_PAYLOAD_FIELD: Dict[str, str] = {
    "message": "message",
    "signal": "signal",
    "reward": "reward",
    "terminal": "terminal",
    "voice": "voice",
}


@dataclass(frozen=True)
class JourneyEvent:
    """The only unit odyssey writes to disk or sends over a network.

    Append-only, ordered by a client-assigned ``seq`` within ``journey_id``, and
    idempotent by ``event_id``. Cumulative state is never transmitted: folding N
    events costs O(N), where shipping N cumulative steps would cost O(N**2).

    ``model_id`` is per-event on purpose. One journey spans model switches,
    retries and routing fallbacks, so journey-level attribution would silently
    mix models under a single label.
    """

    journey_id: str
    seq: int
    kind: EventKind
    ts: str = field(default_factory=_utc_now_iso)
    event_id: str = field(default_factory=_new_event_id)
    message: Optional[Message] = None
    signal: Optional[Signal] = None
    reward: Optional[Reward] = None
    terminal: Optional[Terminal] = None
    voice: Optional[VoiceEvent] = None
    model_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.seq < 0:
            raise ValueError(f"seq must be non-negative, got {self.seq}")
        expected = _PAYLOAD_FIELD.get(self.kind)
        if expected is None:
            raise ValueError(
                f"unknown kind {self.kind!r}; "
                f"expected one of {sorted(_PAYLOAD_FIELD)}"
            )
        if getattr(self, expected) is None:
            raise ValueError(f"kind={self.kind!r} requires a {expected!r} payload")
        extra = [
            name
            for kind, name in _PAYLOAD_FIELD.items()
            if kind != self.kind and getattr(self, name) is not None
        ]
        if extra:
            raise ValueError(
                f"kind={self.kind!r} must not carry {sorted(extra)} payload(s)"
            )

    @property
    def payload(self) -> Any:
        """The one payload this event carries, whichever kind it is."""
        return getattr(self, _PAYLOAD_FIELD[self.kind])
