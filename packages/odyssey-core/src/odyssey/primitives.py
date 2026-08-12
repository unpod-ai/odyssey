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
    info: Optional[Dict[str, Any]] = None
    trainable_status: TrainableStatus = "not_trainable"


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
    env_time: Optional[float] = None
    llm_time: Optional[float] = None
    total_time: Optional[float] = None
    termination_reason: Optional[TerminationReason] = None


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
