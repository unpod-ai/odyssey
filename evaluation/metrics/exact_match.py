"""Deterministic exact-match scoring (item 7.3) — loaded by
`odyssey_eval.harness.load_metric`, not imported as a package (this
directory is tracked metric *implementation* code, not part of the
installable `odyssey_eval` package — see `docs/STRUCTURE.md`).
"""

from __future__ import annotations


def score(response: str, reference: str) -> float:
    """1.0 if ``response`` equals ``reference`` after stripping surrounding
    whitespace, else 0.0."""
    return 1.0 if response.strip() == reference.strip() else 0.0
