"""Configuration for :func:`odyssey.init` — environment first, arguments win.

Twelve-factor shaped: every knob has an ``ODYSSEY_*`` variable so a deployment
can be configured without touching the app, and an explicit argument to
:func:`odyssey.init` so a test can be configured without touching the
environment. Explicit always beats environment.

Nothing here reads the filesystem or opens anything. It resolves values only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from odyssey.spool import DEFAULT_REDACT_KEYS

ENV_SPOOL = "ODYSSEY_SPOOL"
ENV_OUT = "ODYSSEY_OUT"
ENV_ENABLED = "ODYSSEY_ENABLED"
ENV_DRAIN_INTERVAL = "ODYSSEY_DRAIN_INTERVAL"
ENV_DEBUG = "ODYSSEY_DEBUG"
ENV_MAX_OPEN_SHARDS = "ODYSSEY_MAX_OPEN_SHARDS"
ENV_SAMPLE_RATE = "ODYSSEY_SAMPLE_RATE"

DEFAULT_SPOOL_DIR = ".odyssey"
DEFAULT_OUT_DIR = "odyssey-out"
DEFAULT_DRAIN_INTERVAL = 30.0
DEFAULT_MAX_OPEN_SHARDS = 256
DEFAULT_SAMPLE_RATE = 1.0

_FALSEY = {"", "0", "false", "no", "off", "none"}


def _flag(raw: Optional[str], default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() not in _FALSEY


def _number(raw: Optional[str], default: float) -> float:
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        # A typo in an env var must not take recording down with it.
        return default


def _count(raw: Optional[str], default: int) -> int:
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class Config:
    """Resolved settings for one :class:`~odyssey.client.Client`."""

    spool_dir: Path
    out_dir: Path
    drain_interval: Optional[float]
    enabled: bool
    flush_on_exit: bool
    handle_sigterm: bool
    debug: bool
    max_open_shards: int
    redact_keys: frozenset
    fsync: bool
    # None here means "let SpoolConfig resolve ODYSSEY_TIMEZONE itself" — see
    # spool._make_date_fn. Threaded through mainly so init(timezone=...) works
    # without touching the environment, same as every other knob here.
    timezone: Optional[str]
    # Fraction of journeys to actually record, decided once per journey (see
    # capture.journey()) so a sampled-out journey is never partially written.
    sample_rate: float


def resolve(
    *,
    spool_dir: Optional[Path | str] = None,
    out_dir: Optional[Path | str] = None,
    drain_interval: Optional[float] = DEFAULT_DRAIN_INTERVAL,
    drain_interval_set: bool = True,
    enabled: Optional[bool] = None,
    flush_on_exit: bool = True,
    handle_sigterm: bool = False,
    debug: Optional[bool] = None,
    max_open_shards: Optional[int] = None,
    redact_keys: Optional[frozenset] = None,
    fsync: bool = False,
    timezone: Optional[str] = None,
    sample_rate: Optional[float] = None,
) -> Config:
    """Merge explicit arguments over environment over defaults.

    ``drain_interval_set`` distinguishes "the caller passed ``None`` to disable
    the background drain" from "the caller passed nothing" — both look like
    ``None`` otherwise, and they mean opposite things.
    """
    interval: Optional[float]
    if drain_interval_set:
        interval = drain_interval
    else:
        interval = _number(os.environ.get(ENV_DRAIN_INTERVAL), DEFAULT_DRAIN_INTERVAL)

    return Config(
        spool_dir=Path(
            spool_dir
            if spool_dir is not None
            else os.environ.get(ENV_SPOOL, DEFAULT_SPOOL_DIR)
        ),
        out_dir=Path(
            out_dir if out_dir is not None else os.environ.get(ENV_OUT, DEFAULT_OUT_DIR)
        ),
        drain_interval=interval,
        enabled=(
            enabled if enabled is not None else _flag(os.environ.get(ENV_ENABLED), True)
        ),
        flush_on_exit=flush_on_exit,
        handle_sigterm=handle_sigterm,
        debug=(debug if debug is not None else _flag(os.environ.get(ENV_DEBUG), False)),
        max_open_shards=_count(
            str(max_open_shards) if max_open_shards is not None else None,
            _count(os.environ.get(ENV_MAX_OPEN_SHARDS), DEFAULT_MAX_OPEN_SHARDS),
        ),
        redact_keys=redact_keys if redact_keys is not None else DEFAULT_REDACT_KEYS,
        fsync=fsync,
        timezone=timezone,
        sample_rate=min(
            1.0,
            max(
                0.0,
                (
                    sample_rate
                    if sample_rate is not None
                    else _number(os.environ.get(ENV_SAMPLE_RATE), DEFAULT_SAMPLE_RATE)
                ),
            ),
        ),
    )
