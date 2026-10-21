from __future__ import annotations

from pydantic import BaseModel


class IngestResponse(BaseModel):
    event_id: int
    duplicate: bool


class CallIngestResponse(IngestResponse):
    transcription_id: int | None = None  # 새로 받은 통화녹음이면 접수된 전사 작업 id


class KakaoIngestResponse(BaseModel):
    event_id: int | None = None
    duplicate: bool = False
    ignored: bool = False   # 묶음 요약 알림이거나 KAKAO_CHANNELS 밖의 채널(알림 끈 방 등)이라 받지 않았다


class HealthResponse(BaseModel):
    status: str
    events: dict[str, int]
