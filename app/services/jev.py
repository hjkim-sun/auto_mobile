"""TypeSafe Jev 판별 호출."""
from __future__ import annotations

import httpx

from app.core.config import get_settings

QUESTIONS = {
    "relevance": {
        "type": "choice",
        "instructions": "개인비서가 이 내용을 기록하거나 사용자에게 알릴 가치가 있는가?",
        "criteria": {
            "useful": "약속·일정·모임, 가족이나 지인과의 개인적 대화, 업무 연락, 결제·송금·예약 확인, "
                      "배송·병원·관공서 등 사용자가 기억하거나 대응해야 할 내용",
            "noise": "광고·홍보·스팸, 인증번호, 뉴스레터, 단순 시스템 알림처럼 버려도 되는 내용",
        },
    },
}


def classify(state: str) -> dict:
    settings = get_settings()
    resp = httpx.post(
        f"{settings.typesafe_api_base}/v1/systemone",
        headers={"Authorization": f"Bearer {settings.typesafe_api_key.get_secret_value()}"},
        json={"model": settings.jev_model, "state": state, "questions": QUESTIONS},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
