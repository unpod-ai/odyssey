"""Storage adapters. `filesystem.py` is the only one implemented today —
every registry/journey store this service reads is a real file on disk
right now (`docs/adr/0002-artifacts-out-of-git.md`'s "git/registry holds
the pointer, object store holds the bytes" — no object store integration
exists here yet either, same as `odyssey_dataprep.datasets`).

`mongo.py`/`postgres.py`/`objectstore.py` (named in `docs/STRUCTURE.md`'s
tree) are deliberately **not built** — same explicit-deferral treatment
items 0.11/3.5/`judges.py` got: a real dependency with no concrete,
named deployment to justify it yet. `domain/` calls this module through
one narrow interface, so adding a real DB-backed repository later is a
swap, not a rewrite of every router.
"""

from __future__ import annotations

__all__: list[str] = []
