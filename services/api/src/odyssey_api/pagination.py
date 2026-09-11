"""Cursor pagination for the list endpoints whose backing data grows
unboundedly (journeys, metrics, runs, exports) -- as opposed to the
name-keyed registries (datasets, models, products) which stay small and
are returned in full.

Every domain listing here already scans its whole directory into memory
(there's no real database to `LIMIT`/`OFFSET` against), so the cursor is
an opaque, base64-encoded offset into that in-memory list rather than a
real keyset cursor. Callers must treat it as opaque and replay it
verbatim as the next request's ``?cursor=`` -- see `JourneyPageOut` and
friends in `odyssey_schemas`.
"""

from __future__ import annotations

import base64
import binascii
from typing import List, Optional, Tuple, TypeVar

T = TypeVar("T")

DEFAULT_LIMIT = 25
MAX_LIMIT = 100


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def decode_cursor(cursor: Optional[str]) -> int:
    """An invalid/tampered cursor restarts from the beginning rather than
    erroring -- this is a read-only dashboard listing, not something
    where silently re-paginating from offset 0 causes harm."""
    if not cursor:
        return 0
    try:
        offset = int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return 0
    return offset if offset > 0 else 0


def paginate(
    items: List[T], cursor: Optional[str], limit: Optional[int]
) -> Tuple[List[T], Optional[str], bool, int]:
    """Returns ``(page_items, next_cursor, has_more, total)``."""
    offset = decode_cursor(cursor)
    lim = limit if limit and limit > 0 else DEFAULT_LIMIT
    lim = min(lim, MAX_LIMIT)
    page_items = items[offset : offset + lim]
    next_offset = offset + len(page_items)
    total = len(items)
    has_more = next_offset < total
    next_cursor = encode_cursor(next_offset) if has_more else None
    return page_items, next_cursor, has_more, total
