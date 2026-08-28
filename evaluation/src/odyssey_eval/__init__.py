"""odyssey-eval — the offline evaluation harness (item 7).

Scores caller-produced completions against a frozen benchmark suite; never
calls a model itself. See `harness.py`'s module docstring for the
offline-vs-live design decision this member commits to.
"""

from __future__ import annotations

__all__: list[str] = []
