from __future__ import annotations

from pydantic import BaseModel


class IngestResponse(BaseModel):
    event_id: int
    duplicate: bool


class HealthResponse(BaseModel):
    status: str
    events: dict[str, int]
