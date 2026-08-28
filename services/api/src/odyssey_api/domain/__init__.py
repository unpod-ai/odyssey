"""Use-cases — zero fastapi imports (`docs/STRUCTURE.md`'s rule). Each
function takes plain paths/ids and a `repositories.filesystem` reference
implicitly (imported directly; there is only one repository today, see
`repositories/__init__.py`) and returns a plain dict/dataclass shape, not
an `odyssey_schemas` model — routers do that translation, so this layer
has no FastAPI/pydantic dependency at all.
"""

from __future__ import annotations

__all__: list[str] = []
