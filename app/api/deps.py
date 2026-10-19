from __future__ import annotations

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings

# Swagger UI 는 Authorization 헤더 파라미터를 보내지 않으므로 보안 스킴으로 받는다 (docs 의 Authorize 버튼).
bearer = HTTPBearer(auto_error=False)


def require_ingest_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    token = settings.ingest_token.get_secret_value() if settings.ingest_token else None
    if token and (credentials is None or credentials.credentials != token):
        raise HTTPException(401, "invalid token")
