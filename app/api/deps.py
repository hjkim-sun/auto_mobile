from __future__ import annotations

from fastapi import Depends, Header, HTTPException

from app.core.config import Settings, get_settings


def require_ingest_token(
    authorization: str | None = Header(None), settings: Settings = Depends(get_settings)
) -> None:
    token = settings.ingest_token.get_secret_value() if settings.ingest_token else None
    if token and authorization != f"Bearer {token}":
        raise HTTPException(401, "invalid token")
