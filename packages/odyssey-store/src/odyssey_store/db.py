"""Connection helper for the one shared ODYSSEY_DB_URI file. Deliberately
opens a fresh connection per call rather than holding one shared
long-lived connection across threads -- sqlite3 connections are not
safe to share across threads without care, and opening a local-file
connection is cheap (tens of microseconds), so "one connection per
caller" sidesteps the whole class of cross-thread sharing bugs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from odyssey_store.schema import SCHEMA_STATEMENTS

_PREFIX = "sqlite:///"


class CorruptDatabaseError(RuntimeError):
    """Raised instead of letting a raw sqlite3.DatabaseError propagate --
    this file may hold real, unrecoverable tenant credentials (see the
    design spec's corruption-recovery decision), so both services must
    fail loudly and specifically here, never auto-delete-and-rebuild."""


def parse_sqlite_uri(uri: str) -> Path:
    """``sqlite:///relative/path`` -> ``Path("relative/path")``;
    ``sqlite:////absolute/path`` -> ``Path("/absolute/path")`` -- the
    same three-slash-relative/four-slash-absolute convention SQLAlchemy's
    sqlite URIs use, so it reads familiarly to anyone who has seen a
    ``DATABASE_URL``.
    """
    if not uri.startswith(_PREFIX):
        raise ValueError(f"{uri!r}: expected a sqlite:/// URI")
    rest = uri[len(_PREFIX) :]
    if rest.startswith("/"):
        return Path("/" + rest.lstrip("/"))
    return Path(rest)


def connect(uri: str) -> sqlite3.Connection:
    """Opens (creating the file/parent dirs if needed), applies the
    shared schema (idempotent), and returns a ready-to-query connection
    in WAL mode with a 5s busy timeout."""
    path = parse_sqlite_uri(uri)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        conn.commit()
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise CorruptDatabaseError(
            f"{path} is not a valid SQLite database (or is corrupt): {exc}. "
            f"This file may hold real tenant credentials — restore from "
            f"backup rather than deleting it."
        ) from exc
    return conn
