from __future__ import annotations

from pydantic import BaseModel


class IngestResponse(BaseModel):
    event_id: int
    duplicate: bool


class CallIngestResponse(IngestResponse):
    transcription_id: int | None = None  # 새로 받은 통화녹음이면 접수된 전사 작업 id


class HealthResponse(BaseModel):
    status: str
    events: dict[str, int]
