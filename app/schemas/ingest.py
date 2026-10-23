from __future__ import annotations

from typing import Literal

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


class KakaoRoom(BaseModel):
    chat_id: int
    kind: Literal["personal", "group"]   # 1:1 방, 단톡방. 오픈채팅·채널·나와의 채팅은 보내지 않는다
    name: str                             # 1:1 은 상대 이름, 단톡은 방 이름(없으면 참여자 이름을 이은 것)
    members: list[str] = []               # 단톡 참여자 이름. 나는 뺀다


class KakaoHistoryMessage(BaseModel):
    chat_id: int
    log_id: int
    sent_at: int    # epoch 초
    author: str
    text: str


class KakaoHistory(BaseModel):
    rooms: list[KakaoRoom] = []
    messages: list[KakaoHistoryMessage] = []


class KakaoHistoryResponse(BaseModel):
    rooms: int
    added: int
    duplicates: int
    unknown_room: int   # rooms 에 없는 방의 메시지라 버린 수


class HealthResponse(BaseModel):
    status: str
    events: dict[str, int]
