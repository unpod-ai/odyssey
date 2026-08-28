"""FastAPI dependency wiring — the one place `Settings` becomes request-
scoped. Kept separate from `settings.py` itself so `domain/`/`repositories/`
never import fastapi (STRUCTURE.md's "domain: zero fastapi imports" rule)."""

from __future__ import annotations

from odyssey_api.settings import Settings, get_settings

__all__ = ["get_settings_dep"]


def get_settings_dep() -> Settings:
    return get_settings()
