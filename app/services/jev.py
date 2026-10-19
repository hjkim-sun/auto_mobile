"""TypeSafe Jev 판별 호출. 질문(questions)은 app/services/prompts.py 가 prompts/*.toml 에서 읽는다."""
from __future__ import annotations

import httpx

from app.core.config import get_settings


def classify(state: str, questions: dict) -> dict:
    settings = get_settings()
    resp = httpx.post(
        f"{settings.typesafe_api_base}/v1/systemone",
        headers={"Authorization": f"Bearer {settings.typesafe_api_key.get_secret_value()}"},
        json={"model": settings.jev_model, "state": state, "questions": questions},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
