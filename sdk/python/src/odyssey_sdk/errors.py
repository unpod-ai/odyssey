"""Errors this client raises. Kept separate from `client.py` so
`resources/*.py` (generated) can import just this, not the transport."""

from __future__ import annotations

__all__ = ["OdysseyAPIError", "OdysseyAPINotFoundError"]


class OdysseyAPIError(RuntimeError):
    """Any non-2xx response from `services/api`."""

    def __init__(self, status_code: int, body: str, path: str) -> None:
        super().__init__(f"{status_code} from {path}: {body}")
        self.status_code = status_code
        self.body = body
        self.path = path


class OdysseyAPINotFoundError(OdysseyAPIError):
    """A 404 — split out from the base error since callers routinely want
    to catch "not found" without catching every other failure mode."""


def raise_for_status(status_code: int, body: str, path: str) -> None:
    if 200 <= status_code < 300:
        return
    if status_code == 404:
        raise OdysseyAPINotFoundError(status_code, body, path)
    raise OdysseyAPIError(status_code, body, path)
