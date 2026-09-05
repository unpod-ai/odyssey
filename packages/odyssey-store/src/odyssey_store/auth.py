"""One hash function, shared by services/collector (who stores it, who
authenticates against it) and any future migration/audit tooling (who
needs to reproduce it from an already-issued key) -- a single
implementation so "how is a key hashed" is never a question with two
different answers.
"""

from __future__ import annotations

import hashlib


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()
