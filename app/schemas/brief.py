from __future__ import annotations

from pydantic import BaseModel


class CallBriefResponse(BaseModel):
    title: str      # 알림 제목. "📞 홍길동"
    text: str       # 알림 본문. 최근 주제 3줄 요약
    sms: int        # 요약에 넣은 문자 수
    kakao: int      # 요약에 넣은 카톡 수
    calls: int      # 요약에 넣은 통화 수
    elapsed: float  # 서버에서 걸린 시간(초)
    summarized_at: str | None  # 미리 만들어 둔 요약을 줬으면 만든 시각(UTC). 그때 요약했으면 null
    html: str       # 전화 화면 위에 띄우는 큰 카드 (폰 Dialog web 블록)
