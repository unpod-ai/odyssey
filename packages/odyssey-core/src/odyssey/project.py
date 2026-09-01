"""Auto-detect a "project" tag -- which repo/codebase a process is
capturing from. Purely descriptive: it lands in
``JourneyHeader.journey_metadata["project"]`` (see ``odyssey.capture``).
Never an auth boundary -- that is ``services/collector``'s unrelated
``Product`` concept (a unique api_key per top-level tenant).

Chain: ``ODYSSEY_PROJECT`` env var, then ``.git/config``'s
``[remote "origin"]`` URL, then the cwd's directory name. The caller
(``odyssey.config.resolve()``) handles the "explicit argument" step above
this chain via its own sentinel -- this function only ever runs the
auto-detect part, and never raises: every failure degrades to the next
step, ending at the directory name, which always succeeds.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import Optional

ENV_PROJECT = "ODYSSEY_PROJECT"


def _from_git_remote(start: Path) -> Optional[str]:
    """``start``'s ``.git/config``, walking up through parents the same way
    ``git`` itself resolves a repo from a subdirectory. Returns the last
    path segment of ``[remote "origin"]``'s ``url``, minus a trailing
    ``.git``. ``None`` on any failure -- no ``.git``, no ``origin``
    remote, unreadable or malformed config -- never raises.
    """
    try:
        current = start.resolve()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        git_dir = candidate / ".git"
        if not git_dir.is_dir():
            continue
        config_path = git_dir / "config"
        if not config_path.exists():
            return None
        parser = configparser.ConfigParser()
        try:
            parser.read(config_path)
        except configparser.Error:
            return None
        for section in parser.sections():
            if section.strip() == 'remote "origin"':
                url = parser.get(section, "url", fallback=None)
                if not url:
                    return None
                name = url.rstrip("/").rsplit("/", 1)[-1]
                if name.endswith(".git"):
                    name = name[: -len(".git")]
                return name or None
        return None
    return None


def resolve_project(*, cwd: Optional[Path] = None) -> Optional[str]:
    """``ODYSSEY_PROJECT`` env var, then git remote ``origin``, then the
    cwd's directory name. Always returns a usable value in practice --
    the directory name step only fails to produce one if the working
    directory itself no longer exists, in which case this returns
    ``None`` rather than raising.
    """
    env = os.environ.get(ENV_PROJECT)
    if env:
        return env
    try:
        start = cwd if cwd is not None else Path.cwd()
    except OSError:
        return None
    detected = _from_git_remote(start)
    if detected:
        return detected
    try:
        name = start.resolve().name
    except OSError:
        return None
    return name or None
