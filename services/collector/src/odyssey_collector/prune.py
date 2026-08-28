"""Retention for the collector's date-partitioned storage (items 1.12/2.14).

``<data_dir>/<YYYY-MM-DD>/`` directories are simple date buckets — this
deletes whole directories older than a cutoff, using the directory's own
name (it already says which day it holds) rather than statting individual
files inside it. Not run on a timer: an explicit, operator-invoked sweep
(cron/ops), the same discipline ``odyssey.spool.gc()`` applies on the SDK
side — this server's own module docstring already called wholesale
deletion of old dates "trivial to archive or delete"; this makes it real.

No server endpoint for this — the collector's ``http.server`` has no admin
surface, and exposing destructive deletion over HTTP would need its own
auth story this module has no opinion on. Run it directly::

    python -m odyssey_collector.prune --data-dir ./data --older-than-days 30
"""

from __future__ import annotations

import argparse
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional, Sequence

__all__ = ["prune_dir", "main"]


def prune_dir(
    data_dir: Path | str, older_than_days: float, *, dry_run: bool = False
) -> List[Path]:
    """Delete every ``<data_dir>/<YYYY-MM-DD>/`` directory older than the
    cutoff. A directory whose name isn't a parseable date is left alone
    entirely — this only ever touches partitions it recognises as its own.
    """
    cutoff = date.today() - timedelta(days=older_than_days)
    root = Path(data_dir)
    deleted: List[Path] = []
    if not root.is_dir():
        return deleted

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        try:
            entry_date = date.fromisoformat(entry.name)
        except ValueError:
            continue
        if entry_date >= cutoff:
            continue
        deleted.append(entry)
        if not dry_run:
            shutil.rmtree(entry)
    return deleted


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m odyssey_collector.prune")
    p.add_argument("--data-dir", required=True, help="the collector's data_dir")
    p.add_argument(
        "--older-than-days", type=float, required=True, help="minimum age, in days"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="list what would be deleted only"
    )
    args = p.parse_args(argv)

    deleted = prune_dir(args.data_dir, args.older_than_days, dry_run=args.dry_run)
    verb = "would delete" if args.dry_run else "deleted"
    if not deleted:
        print(f"{verb} nothing")
        return 0
    for path in deleted:
        print(f"{verb} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
