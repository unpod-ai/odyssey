"""FastAPI dependency wiring — the one place `Settings` becomes request-
scoped. Kept separate from `settings.py` itself so `domain/`/`repositories/`
never import fastapi (STRUCTURE.md's "domain: zero fastapi imports" rule)."""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException, status

from odyssey_api.settings import Settings, get_settings

__all__ = ["get_settings_dep", "require_api_key"]


def get_settings_dep() -> Settings:
    return get_settings()


def require_api_key(
    authorization: Optional[str] = Header(None),
    settings: Settings = Depends(get_settings_dep),
) -> None:
    """Bearer-token auth, same shape as services/collector's --api-key
    mode -- one shared key, open when unset. A per-router dependency
    (not middleware) so it composes with this service's existing
    dependency-override test pattern (app.dependency_overrides[
    get_settings_dep] = lambda: settings) the same way every other
    Settings-dependent behavior in this service is already tested.
    """
    if not settings.api_key:
        return
    if authorization != f"Bearer {settings.api_key}":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid Authorization",
        )
