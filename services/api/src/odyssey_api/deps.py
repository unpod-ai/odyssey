"""FastAPI dependency wiring — the one place `Settings` becomes request-
scoped. Kept separate from `settings.py` itself so `domain/`/`repositories/`
never import fastapi (STRUCTURE.md's "domain: zero fastapi imports" rule)."""

from __future__ import annotations

import secrets
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from odyssey_api.index.manager import IndexHandle, get_index
from odyssey_api.settings import Settings, get_settings

__all__ = ["get_settings_dep", "get_index_dep", "require_api_key"]

_bearer = HTTPBearer(auto_error=False)


def get_settings_dep() -> Settings:
    return get_settings()


def get_index_dep(settings: Settings = Depends(get_settings_dep)) -> IndexHandle:
    return get_index(settings)


def require_api_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    settings: Settings = Depends(get_settings_dep),
) -> None:
    """Bearer-token auth, same shape as services/collector's --api-key
    mode -- one shared key, open when unset. Uses HTTPBearer rather than
    a raw Header() so the OpenAPI schema gets a proper securitySchemes
    entry instead of a spurious per-operation `authorization` parameter
    and 422 response on every route.
    """
    if not settings.api_key:
        return
    if credentials is None or not secrets.compare_digest(
        credentials.credentials, settings.api_key
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid Authorization",
            headers={"WWW-Authenticate": "Bearer"},
        )
