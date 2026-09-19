"""Which provider served an OpenAI-compatible call, and per-provider logic.

Most hosted models are reached through the ``openai`` SDK pointed at another
host — Groq, xAI, Cerebras, OpenRouter, DeepInfra, Sarvam, Azure, a local
Ollama, a Modal deployment. The SDK and the wire format are OpenAI's, so the
SDK's name says nothing about who answered. The client's ``base_url`` does.

Resolution, first match wins:

1. providers added with :func:`register_provider` (host suffix or a callable)
2. ``ODYSSEY_PROVIDER_HOSTS`` — ``host=name`` pairs, comma separated, so a
   deployment can name a new gateway without a code change
3. the built-in host table below
4. the bare hostname, so an unknown gateway is still told apart from OpenAI
5. ``"openai"`` when the client has no ``base_url`` at all

A registered provider may carry an ``adapt`` hook. It receives the assembled
assistant message (a Chat Completions-shaped dict) and the raw response —
``None`` for a stream — and returns the dict to record. That is where a
provider's quirks go (a non-standard field, reasoning inlined as tags) without
touching the shared capture path. Every provider first gets
:func:`normalize_reasoning`, which covers the common spellings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from odyssey.client import require_client

Adapter = Callable[[Dict[str, Any], Any], Dict[str, Any]]

ENV_PROVIDER_HOSTS = "ODYSSEY_PROVIDER_HOSTS"

# Host suffix -> provider. A suffix, so per-deployment subdomains match
# (`<resource>.openai.azure.com`, `<app>.modal.run`).
_BUILTIN_HOSTS: Tuple[Tuple[str, str], ...] = (
    ("api.openai.com", "openai"),
    ("openai.azure.com", "azure"),
    ("services.ai.azure.com", "azure"),
    ("api.groq.com", "groq"),
    ("api.x.ai", "xai"),
    ("api.cerebras.ai", "cerebras"),
    ("openrouter.ai", "openrouter"),
    ("api.sarvam.ai", "sarvam"),
    ("api.deepinfra.com", "deepinfra"),
    ("api.deepseek.com", "deepseek"),
    ("api.together.xyz", "together"),
    ("api.fireworks.ai", "fireworks"),
    ("api.perplexity.ai", "perplexity"),
    ("api.mistral.ai", "mistral"),
    ("api.sambanova.ai", "sambanova"),
    ("api.cometapi.com", "cometapi"),
    ("api.letta.com", "letta"),
    ("generativelanguage.googleapis.com", "gemini"),
    ("api.anthropic.com", "anthropic"),
    ("modal.run", "modal"),
)

# Ollama's default port. `LLM.with_ollama` points at localhost, where the
# hostname alone would say nothing.
_LOCAL_PORTS: Dict[int, str] = {11434: "ollama"}


@dataclass(frozen=True)
class Provider:
    name: str
    adapt: Optional[Adapter] = None


@dataclass(frozen=True)
class _Registration:
    provider: Provider
    hosts: Tuple[str, ...]
    match: Optional[Callable[[str], bool]]


_REGISTRY: List[_Registration] = []
_env_cache: Tuple[Optional[str], Tuple[Tuple[str, str], ...]] = (None, ())


def register_provider(
    name: str,
    *,
    hosts: Sequence[str] = (),
    match: Optional[Callable[[str], bool]] = None,
    adapt: Optional[Adapter] = None,
) -> None:
    """Name a provider, and optionally give it custom capture logic.

    ::

        register_provider("unpod", hosts=["unpod-llm.modal.run"])
        register_provider("sarvam", hosts=["api.sarvam.ai"], adapt=split_think)

    ``hosts`` are host suffixes; ``match`` receives the full ``base_url``.
    Registrations win over the environment and the built-in table, and a later
    one wins over an earlier one, so a deployment can rename a built-in host.
    An ``adapt`` that raises is counted in ``odyssey.health()`` and the
    unadapted message is recorded — a hook bug must not cost the turn.
    """
    if not hosts and match is None:
        raise ValueError("register_provider needs hosts= or match=")
    _REGISTRY.insert(
        0,
        _Registration(
            Provider(name, adapt), tuple(h.lower().strip() for h in hosts), match
        ),
    )


def unregister_provider(name: str) -> None:
    """Remove every registration under ``name``. Safe when there is none."""
    _REGISTRY[:] = [r for r in _REGISTRY if r.provider.name != name]


def resolve(base_url: Any) -> Provider:
    """The provider behind ``base_url``. Never raises."""
    url = str(base_url) if base_url is not None else ""
    if not url:
        return Provider("openai")
    try:
        parts = urlsplit(url if "://" in url else f"//{url}")
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return Provider("openai")

    for reg in _REGISTRY:
        if _host_matches(host, reg.hosts) or _safe_match(reg.match, url):
            return reg.provider
    for suffix, name in _env_hosts():
        if _host_matches(host, (suffix,)):
            return Provider(name)
    for suffix, name in _BUILTIN_HOSTS:
        if _host_matches(host, (suffix,)):
            return Provider(name)
    if port in _LOCAL_PORTS:
        return Provider(_LOCAL_PORTS[port])
    return Provider(host or "openai")


def normalize_reasoning(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Put a reasoning trace where the parser expects it.

    DeepSeek, Groq, Qwen and Sarvam return ``reasoning_content``; OpenRouter and
    some vLLM builds return ``reasoning``, sometimes as an object. The parser
    accepts one string field and rejects anything else — which would cost the
    whole turn — so this runs for every provider, before any ``adapt`` hook.
    """
    out = dict(entry)
    alt = out.pop("reasoning_content", None)
    current = out.get("reasoning")
    if not isinstance(current, str):
        out.pop("reasoning", None)
        current = None
    if current is None and isinstance(alt, str) and alt:
        out["reasoning"] = alt
    return out


def apply_adapter(
    adapt: Adapter, entry: Dict[str, Any], raw: Any, *, label: str
) -> Dict[str, Any]:
    """Run a provider's ``adapt`` hook, falling back to ``entry`` on failure."""
    try:
        adapted = adapt(dict(entry), raw)
    except Exception as exc:  # noqa: BLE001 - a hook bug must not cost the turn
        client = require_client()
        if client is not None:
            client.note_error(label, exc)
        return entry
    return adapted if isinstance(adapted, dict) else entry


def _host_matches(host: str, suffixes: Sequence[str]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes if s)


def _safe_match(match: Optional[Callable[[str], bool]], url: str) -> bool:
    if match is None:
        return False
    try:
        return bool(match(url))
    except Exception:  # noqa: BLE001 - a matcher bug means "not this provider"
        return False


def _env_hosts() -> Tuple[Tuple[str, str], ...]:
    global _env_cache
    raw = os.environ.get(ENV_PROVIDER_HOSTS)
    if raw == _env_cache[0]:
        return _env_cache[1]
    pairs: List[Tuple[str, str]] = []
    for item in (raw or "").split(","):
        host, sep, name = item.partition("=")
        if sep and host.strip() and name.strip():
            pairs.append((host.strip().lower(), name.strip()))
    _env_cache = (raw, tuple(pairs))
    return _env_cache[1]
