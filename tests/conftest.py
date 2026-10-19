from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings


@pytest.fixture(autouse=True)
def settings_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MESSAGE_SETTLE_SECONDS", "0")
    monkeypatch.setenv("INGEST_TOKEN", "")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    get_settings.cache_clear()
    yield
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
