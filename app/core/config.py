from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MailAccountConfig(BaseModel):
    # events.payload.account 와 mail_cursors 키. 계정마다 달라야 한다.
    name: str = Field(pattern=r"^[a-z0-9_.-]+$")
    user: str
    password: SecretStr
    host: str = "imap.naver.com"


class Settings(BaseSettings):
    """Runtime configuration; secrets are supplied only through environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    data_dir: Path = Path("data")
    # 문자·메일의 date_text, 날짜 폴더, 통화녹음 기본 파일명에 쓰는 시간대. 서버(VPS)는 UTC 다.
    timezone: str = "Asia/Seoul"
    # 설정하면 수신 API 가 "Authorization: Bearer <토큰>" 을 요구한다.
    ingest_token: SecretStr | None = None

    typesafe_api_key: SecretStr = SecretStr("")
    typesafe_api_base: str = "https://api.typesafe.ai"
    jev_model: str = "jev-latest"
    # 판별 프롬프트 디렉토리. 이벤트 종류마다 {source}.toml (app/services/prompts.py)
    prompts_dir: Path = Path("prompts")
    # useful 확률이 이 값 이상이면 남긴다. 놓치는 쪽이 더 손해라 낮게 잡는다. 프롬프트 파일의 threshold 가 우선한다.
    jev_keep_threshold: float = Field(default=0.3, ge=0, le=1)

    # MMS 는 본문 POST 뒤에 첨부 PUT 이 이어지므로, 마지막 수신 후 이만큼 기다렸다 처리한다.
    message_settle_seconds: int = Field(default=30, ge=0)
    worker_poll_seconds: float = Field(default=3.0, gt=0)
    worker_max_attempts: int = Field(default=5, ge=1)

    # 메일 폴러 (app/jobs/mail_poller.py). JSON 목록으로 넣는다:
    # MAIL_ACCOUNTS='[{"name": "naver-main", "user": "아이디", "password": "비밀번호"}, ...]'
    # 네이버는 메일 환경설정에서 IMAP 을 켜고, 2단계 인증을 쓰면 애플리케이션 비밀번호를 넣는다.
    mail_accounts: list[MailAccountConfig] = []
    mail_poll_seconds: float = Field(default=300, gt=0)
    # 처음 폴링하거나 UIDVALIDITY 가 바뀌었을 때 며칠 전 메일부터 가져올지
    mail_backfill_days: int = Field(default=1, ge=0)

    # 음성 전사 (faster-whisper). 모델은 CTranslate2 변환본 디렉토리를 미리 받아 둔다.
    whisper_model_dir: Path = Path.home() / "whisper" / "large-v3-turbo"
    whisper_compute_type: str = "int8"
    whisper_cpu_threads: int = Field(default=2, ge=1)
    whisper_language: str = "ko"

    # 화자 분리 (sherpa-onnx). 모델 파일이 없으면 화자 없이 전사만 한다.
    diarization_enabled: bool = True
    diarization_segmentation_model: Path = (
        Path.home() / "sherpa" / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx")
    diarization_embedding_model: Path = (
        Path.home() / "sherpa" / "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx")
    # 통화는 거의 항상 2명이다. 0 이면 diarization_threshold 로 화자 수를 추정한다.
    diarization_num_speakers: int = Field(default=2, ge=0)
    diarization_threshold: float = Field(default=0.5, gt=0)

    # Buzz 봇 (app/jobs/buzz_bot.py). relay URL 이 비어 있으면 봇을 띄우지 않는다.
    # 봇 신원은 BUZZ_PRIVATE_KEY(hex 또는 nsec). 사용자는 커뮤니티에 이 봇의 npub 을 초대해 둔다.
    buzz_relay_url: str = ""
    buzz_private_key: SecretStr = SecretStr("")
    # 알림을 받고 명령을 내릴 사용자(npub 또는 hex). 봇은 이 사람과의 DM 만 본다.
    buzz_owner_pubkey: str = ""
    # buzz CLI. 데스크톱 .deb 에 들어 있는 바이너리를 꺼내 둔다 (README "Buzz 봇" 참고).
    buzz_cli_path: Path = Path.home() / ".local" / "bin" / "buzz"
    buzz_poll_seconds: float = Field(default=5, gt=0)

    # LLM 위키 (app/jobs/wiki_builder.py). kept 이벤트를 Claude Code(헤드리스)가 data/wiki 의 마크다운으로 정리한다.
    # 위키 관리 규칙은 WIKI_SCHEMA_PATH. claude 는 로그인돼 있어야 한다(automan 계정의 ~/.claude).
    wiki_claude_path: Path = Path.home() / ".local" / "bin" / "claude"
    wiki_model: str = "sonnet"
    wiki_schema_path: Path = Path("prompts/wiki.md")
    # claude 1회 호출에 넣는 자료 수
    wiki_batch_size: int = Field(default=5, ge=1)
    wiki_poll_seconds: float = Field(default=600, gt=0)
    wiki_timeout_seconds: float = Field(default=900, gt=0)

    @field_validator("mail_accounts")
    @classmethod
    def _unique_account_names(cls, v: list[MailAccountConfig]) -> list[MailAccountConfig]:
        names = [a.name for a in v]
        if len(names) != len(set(names)):
            raise ValueError("mail account names must be unique")
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "events.db"

    @property
    def wiki_dir(self) -> Path:
        return self.data_dir / "wiki"


@lru_cache
def get_settings() -> Settings:
    return Settings()
