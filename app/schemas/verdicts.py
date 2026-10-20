from __future__ import annotations

from pydantic import BaseModel


class Verdict(BaseModel):
    event_id: int
    status: str                  # pending | processing | kept | discarded | failed
    useful: float | None = None  # Jev 가 매긴 useful 확률. 판별 전이거나 텍스트가 없으면 None
    choice: str | None = None    # useful | noise
    reason: str | None = None    # Jev 없이 정한 경우의 사유 (no_text)
    model: str | None = None
    error: str | None = None
    received_at: str
    updated_at: str


class MessageVerdict(Verdict):
    kind: str | None = None
    sender: str | None = None
    date: str | None = None
    text: str | None = None
    attachments: list[dict] = []


class EmailVerdict(Verdict):
    account: str | None = None
    sender: str | None = None
    subject: str | None = None
    date: str | None = None
    text: str | None = None
    attachments: list[dict] = []


class VerdictPage[T](BaseModel):
    items: list[T]
    next_before_id: int | None   # 다음 페이지는 ?before_id=<값> 으로 받는다. 끝이면 None
