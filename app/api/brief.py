"""전화 수신 브리핑. 폰 Automate 가 전화가 오면 부르고, 응답의 text 를 알림으로 띄운다.

POST /v1/brief/call?number=<발신 번호>&name=<연락처 이름>   둘 다 선택. 응답 {title, text, sms, kakao, calls, elapsed, summarized_at, html}
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from app.api.deps import require_ingest_token
from app.schemas.brief import CallBriefResponse
from app.services import call_brief

router = APIRouter(prefix="/v1/brief", tags=["brief"], dependencies=[Depends(require_ingest_token)])


@router.post("/call", response_model=CallBriefResponse)
async def brief_call(number: str | None = None, name: str | None = None) -> CallBriefResponse:
    return CallBriefResponse(**await run_in_threadpool(call_brief.brief, number, name))
