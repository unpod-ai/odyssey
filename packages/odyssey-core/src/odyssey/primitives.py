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
    journey_id: Optional[str] = None
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
        if self.journey_id is not None:
            d["journey_id"] = self.journey_id
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
SCHEMA_VERSION = "1.0"

EventKind = Literal["message", "signal", "reward", "terminal"]
SignalKind = Literal["thumbs_up", "thumbs_down", "regenerated", "user_edit"]


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _new_event_id() -> str:
    from uuid import uuid4

    return uuid4().hex


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


_PAYLOAD_FIELD: Dict[str, str] = {
    "message": "message",
    "signal": "signal",
    "reward": "reward",
    "terminal": "terminal",
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
