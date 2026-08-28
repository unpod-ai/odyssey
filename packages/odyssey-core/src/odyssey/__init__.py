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
from odyssey.dpo import (
    DpoResult,
    dpo_pairs,
    export_dpo_dir,
    export_dpo_spool,
    save_dpo,
)
from odyssey.export import (
    ExportResult,
    export_dir,
    export_spool,
    fold_shard,
    save,
)
from odyssey.fold import FoldResult, fold
from odyssey.jsonl import ReadResult, read_events, read_header, write_events
from odyssey.pii import redact_pii, scan_pii
from odyssey.primitives import (
    SCHEMA_VERSION,
    WRITER_META_KEY,
    Journey,
    JourneyEvent,
    JourneyHeader,
    Message,
    PiiPolicy,
    RedactionPreview,
    Reward,
    RewardComponent,
    Signal,
    Step,
    Terminal,
    ToolCall,
    ToolDefinition,
    ToolResponse,
)
from odyssey.sft import (
    SftResult,
    export_sft_dir,
    export_sft_spool,
    save_sft,
    sft_examples,
)
from odyssey.sinks import FileSink, HttpSink, HttpSinkError
from odyssey.spool import (
    DrainResult,
    IntervalDrainer,
    Sink,
    Spool,
    SpoolConfig,
    drain,
    gc,
)

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
    "JourneyHeader",
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
    "PiiPolicy",
    "RedactionPreview",
    "scan_pii",
    "redact_pii",
    # Read side
    "fold",
    "FoldResult",
    "save",
    "export_dir",
    "export_spool",
    "fold_shard",
    "ExportResult",
    "read_events",
    "read_header",
    "write_events",
    "ReadResult",
    # Training exporters
    "sft_examples",
    "save_sft",
    "export_sft_dir",
    "export_sft_spool",
    "SftResult",
    "dpo_pairs",
    "save_dpo",
    "export_dpo_dir",
    "export_dpo_spool",
    "DpoResult",
    # Storage and delivery
    "Spool",
    "SpoolConfig",
    "Sink",
    "FileSink",
    "HttpSink",
    "HttpSinkError",
    "IntervalDrainer",
    "DrainResult",
    "drain",
    "gc",
]
