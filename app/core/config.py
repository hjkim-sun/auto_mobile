from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration; secrets are supplied only through environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    data_dir: Path = Path("data")
    # 설정하면 수신 API 가 "Authorization: Bearer <토큰>" 을 요구한다.
    ingest_token: SecretStr | None = None

    typesafe_api_key: SecretStr = SecretStr("")
    typesafe_api_base: str = "https://api.typesafe.ai"
    jev_model: str = "jev-latest"
    # useful 확률이 이 값 이상이면 남긴다. 놓치는 쪽이 더 손해라 낮게 잡는다.
    jev_keep_threshold: float = Field(default=0.3, ge=0, le=1)

    # MMS 는 본문 POST 뒤에 첨부 PUT 이 이어지므로, 마지막 수신 후 이만큼 기다렸다 처리한다.
    message_settle_seconds: int = Field(default=30, ge=0)
    worker_poll_seconds: float = Field(default=3.0, gt=0)
    worker_max_attempts: int = Field(default=5, ge=1)

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "events.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
