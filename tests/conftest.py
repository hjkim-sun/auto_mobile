from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings


@pytest.fixture(autouse=True)
def settings_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MESSAGE_SETTLE_SECONDS", "0")
    monkeypatch.setenv("INGEST_TOKEN", "")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("MAIL_ACCOUNTS", "[]")   # 실제 .env 의 메일 계정에 접속하지 않게
    monkeypatch.setenv("DIARIZATION_ENABLED", "false")   # 실제 화자 분리 모델을 올리지 않게
    get_settings.cache_clear()
    yield
    # 통화 수신이 띄운 전사 스레드가 다음 테스트 사이에 설정을 읽어 캐시하지 않게 끝날 때까지 기다린다.
    # 기다리지 않으면 실제 .env 의 INGEST_TOKEN 이 캐시돼 다음 테스트가 가끔 401 을 받는다.
    # 앱 종료(lifespan)가 executor 를 기다리지 않고 놓아 버리므로 스레드를 직접 기다린다.
    for thread in threading.enumerate():
        if thread.name.startswith("transcribe"):
            thread.join(timeout=30)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_whisper(monkeypatch):
    """통화 수신이 전사를 접수하므로, 테스트에서 실제 모델을 올리지 않게 막는다."""
    from app.services import transcribe

    class NoModel:
        def transcribe(self, *a, **kw):
            raise RuntimeError("whisper disabled in tests")

    monkeypatch.setattr(transcribe, "_model", NoModel())


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c
