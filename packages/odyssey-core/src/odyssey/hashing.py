"""Canonical hashing for journey dicts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from typing import Any

_VOLATILE_KEYS = {"idx"}


def canonicalize(obj: Any) -> Any:
    """Canonicalize for stable hashing: remove None values, sort dict keys, recurse lists."""
    if obj is None:
        return None
    if is_dataclass(obj) and not isinstance(obj, type):
        obj = asdict(obj)
    if isinstance(obj, dict):
        out = {}
        for k in sorted(obj.keys()):
            if k in _VOLATILE_KEYS:
                continue
            v = canonicalize(obj[k])
            if v is None:
                continue
            out[k] = v
        return out
    if isinstance(obj, (list, tuple)):
        return [canonicalize(x) for x in obj]
    return obj


def content_hash(traj: Any) -> str:
    """SHA-256 of the canonical JSON representation of a journey."""
    canon = canonicalize(traj)
    b = json.dumps(canon, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(b).hexdigest()


def idempotency_key(source: str, source_run_id: str, content_hash_val: str) -> str:
    """Deterministic idempotency key for deduplication."""
    return f"{source}:{source_run_id}:{content_hash_val}"
