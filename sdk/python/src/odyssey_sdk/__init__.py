"""odyssey-sdk — the generated Python client for `services/api` (item 8.4).

Not to be confused with `odyssey-core` (distributed as `odyssey`), which
people also colloquially call "the SDK" — that's the capture layer
(`odyssey.init()`, `HttpSink`, ...), a different thing this package has no
dependency on. This package only talks to an already-deployed
`services/api` over HTTP; it never touches the spool or the wire format.
"""

from __future__ import annotations

from odyssey_sdk.client import OdysseySDK
from odyssey_sdk.errors import OdysseyAPIError, OdysseyAPINotFoundError

__all__ = ["OdysseySDK", "OdysseyAPIError", "OdysseyAPINotFoundError"]
