from __future__ import annotations

from pydantic import BaseModel


class CallBriefResponse(BaseModel):
    title: str      # 알림 제목. "📞 홍길동"
    text: str       # 알림 본문. 최근 주제 3줄 요약
    sms: int        # 요약에 넣은 문자 수
    kakao: int      # 요약에 넣은 카톡 수
    elapsed: float  # 서버에서 걸린 시간(초)
