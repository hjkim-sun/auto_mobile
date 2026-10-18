from __future__ import annotations

from fastapi import APIRouter

from app.repositories import events
from app.schemas.ingest import HealthResponse

router = APIRouter(prefix="/v1", tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", events=events.status_counts())
