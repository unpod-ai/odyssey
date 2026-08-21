"""odyssey — agent traces in, training corpora out.

One integration point. Call :func:`init` once at process start; everything after
that is ambient::

    import odyssey
    odyssey.init()

    with odyssey.journey(id=call_id, user_id="u_42") as j:
        ...                      # journey_id and seq come from context
        j.signal("thumbs_up")    # the raw material for preference training

Automatic provider capture is an import swap and nothing else::

    from odyssey.integrations.anthropic import Anthropic
    client = Anthropic()         # every messages.create() is recorded

Recording is local and synchronous: an event is appended to an on-disk spool and
the call returns. A background drainer ships batches out of band, so the
inference path never waits on a network, and recording keeps working with no
server reachable at all.

What is recorded is a training corpus, not an observability trace. That
distinction drives real behaviour — see :func:`observe`.

Nothing here imports a provider SDK, a web framework, or anything outside the
standard library. ``odyssey-core`` declares ``dependencies = []``.
"""

from __future__ import annotations

from odyssey.capture import JourneyHandle, journey, message, observe, reward, signal
from odyssey.client import (
    Client,
    Stats,
    flush,
    get_client,
    health,
    init,
    shutdown,
)
from odyssey.config import Config
from odyssey.context import JourneyContext, SeqAllocator, bind, current
from odyssey.fold import FoldResult, fold
from odyssey.jsonl import ReadResult, read_events, write_events
from odyssey.primitives import (
    SCHEMA_VERSION,
    WRITER_META_KEY,
    Journey,
    JourneyEvent,
    Message,
    Reward,
    RewardComponent,
    Signal,
    Step,
    Terminal,
    ToolCall,
    ToolDefinition,
    ToolResponse,
)
from odyssey.sinks import FileSink
from odyssey.spool import DrainResult, IntervalDrainer, Sink, Spool, SpoolConfig, drain

__version__ = "0.1.0"

__all__ = [
    # Lifecycle — the one integration point
    "init",
    "shutdown",
    "flush",
    "health",
    "get_client",
    "Client",
    "Config",
    "Stats",
    # Recording
    "journey",
    "observe",
    "signal",
    "reward",
    "message",
    "JourneyHandle",
    # Ambient context
    "current",
    "bind",
    "JourneyContext",
    "SeqAllocator",
    # Schema
    "JourneyEvent",
    "Message",
    "Signal",
    "Terminal",
    "Reward",
    "RewardComponent",
    "ToolCall",
    "ToolResponse",
    "ToolDefinition",
    "Journey",
    "Step",
    "SCHEMA_VERSION",
    "WRITER_META_KEY",
    # Read side
    "fold",
    "FoldResult",
    "read_events",
    "write_events",
    "ReadResult",
    # Storage and delivery
    "Spool",
    "SpoolConfig",
    "Sink",
    "FileSink",
    "IntervalDrainer",
    "DrainResult",
    "drain",
]
