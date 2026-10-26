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
    # 웹 화면(/web/wiki, /web/flow)과 그 API(/wiki/*, /flow/*)의 토큰. 위키에 개인 정보가 있으므로 비우면 화면을 닫는다(404).
    # 브라우저에서 /web/flow?token=<토큰> 으로 한 번 열면 쿠키에 남는다.
    web_token: SecretStr | None = None

    typesafe_api_key: SecretStr = SecretStr("")
    typesafe_api_base: str = "https://api.typesafe.ai"
    jev_model: str = "jev-latest"
    # 판별 프롬프트 디렉토리. 이벤트 종류마다 {source}.toml (app/services/prompts.py)
    prompts_dir: Path = Path("prompts")
    # useful 확률이 이 값 이상이면 남긴다. 놓치는 쪽이 더 손해라 낮게 잡는다. 프롬프트 파일의 threshold 가 우선한다.
    jev_keep_threshold: float = Field(default=0.3, ge=0, le=1)

    # MMS 는 본문 POST 뒤에 첨부 PUT 이 이어지므로, 마지막 수신 후 이만큼 기다렸다 처리한다.
    message_settle_seconds: int = Field(default=30, ge=0)
    # 받을 카카오톡 알림 채널 (JSON 목록). 알림에 방 이름도 오픈채팅 여부도 없어서 채널로 거른다.
    # new_message_v6 는 알림을 켠 방, quiet_new_message 는 알림을 끈 방과 카톡 방해금지 시간대의 메시지다.
    # 오픈채팅방은 카톡에서 알림을 꺼 두면 quiet 로 빠진다.
    kakao_channels: list[str] = ["new_message_v6"]
    # 카톡 방 단위 판단 (app/services/kakao_judge.py). 새 메시지가 들어온 방의 최근 대화를 묶어 Jev → Claude 로 본다.
    kakao_judge_enabled: bool = True
    # Jev 와 Claude 에 보여 줄 최근 대화 수
    kakao_judge_window: int = Field(default=20, ge=1)
    # 대화가 이만큼 멈추면 판단한다. 맥 동기화가 1분마다라 그보다 조금 길게 잡는다.
    kakao_judge_settle_seconds: float = Field(default=70, ge=0)
    # 폰 알림이 들어온 방은 메시지마다 바로 오므로 짧게 기다린다. 이어지는 답장("응", "그때 봐")을 묶을 만큼만
    kakao_judge_phone_settle_seconds: float = Field(default=20, ge=0)
    # 대화가 계속 이어져도 첫 새 메시지 뒤 이만큼 지나면 판단한다
    kakao_judge_max_wait_seconds: float = Field(default=300, ge=0)
    # 이보다 오래된 메시지(맥을 오래 꺼 뒀다 채운 것)는 맥락으로만 쓰고 판단하지 않는다
    kakao_judge_max_age_hours: float = Field(default=12, gt=0)
    kakao_judge_poll_seconds: float = Field(default=5, gt=0)
    # Jev 나 Claude 가 실패하면 이만큼 뒤에 다시 판단한다
    kakao_judge_retry_seconds: float = Field(default=300, gt=0)
    kakao_judge_prompt_path: Path = Path("prompts/kakao_judge.md")
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
    # 봇 프로필 이름. 앱의 DM 목록에 이 이름이 보인다. 봇이 시작할 때 설정한다.
    buzz_bot_name: str = "auto_mobile"
    # 멘션 없이도 주인의 말에 답하는 채널 UUID 목록 (JSON). 다른 사람을 콕 집어 부른 말은 빼고 답한다.
    buzz_listen_channels: list[str] = []
    # 알림(kept 이벤트, 캘린더)을 보낼 채널 UUID. 비우면 주인과의 DM 으로 보낸다.
    buzz_notify_channel: str = ""

    # LLM 위키 (app/jobs/wiki_builder.py). kept 이벤트를 Claude Code(헤드리스)가 data/wiki 의 마크다운으로 정리한다.
    # 위키 관리 규칙은 WIKI_SCHEMA_PATH. claude 는 로그인돼 있어야 한다(automan 계정의 ~/.claude).
    wiki_claude_path: Path = Path.home() / ".local" / "bin" / "claude"
    wiki_model: str = "sonnet"
    wiki_schema_path: Path = Path("prompts/wiki.md")
    # claude 1회 호출에 넣는 자료 수
    wiki_batch_size: int = Field(default=5, ge=1)
    wiki_poll_seconds: float = Field(default=600, gt=0)
    wiki_timeout_seconds: float = Field(default=900, gt=0)

    # Buzz 에이전트 (app/services/agent.py). 명령이 아닌 말은 Claude Code 가 위키와 MCP 도구로 답한다.
    agent_enabled: bool = True
    agent_model: str = "sonnet"
    agent_prompt_path: Path = Path("prompts/agent.md")
    agent_timeout_seconds: float = Field(default=300, gt=0)
    # 채널에서 이 시간 동안 말이 없으면 다음 말부터 새 대화(claude 세션)로 시작한다.
    agent_session_hours: float = Field(default=6, gt=0)
    # 다른 에이전트끼리 서로 멘션하며 끝없이 주고받지 않게, 주인이 다시 말할 때까지 에이전트에게 답하는 횟수
    agent_peer_replies: int = Field(default=3, ge=0)
    # kept 알림을 원문 그대로 보내지 않고, 에이전트가 위키에서 맥락을 찾아 비서처럼 브리핑한다.
    agent_briefing: bool = True

    # 전화 수신 브리핑 (app/services/call_brief.py). 사람별 최근 카톡·문자·통화를 claude 로 3줄 요약해 두고 전화가 오면 바로 준다.
    # 워커가 이 주기마다 새 대화가 들어온 사람(call_briefs.is_updated)만 다시 요약한다
    brief_refresh_seconds: float = Field(default=1800, gt=0)
    # haiku 는 effort low 에 생각을 꺼도 생각을 길게 하는 때가 많아(출력 1천여 토큰) 7~10초 걸린다. sonnet low 는 4초 안팎
    brief_model: str = "sonnet"
    brief_effort: str = "low"
    brief_prompt_path: Path = Path("prompts/call_brief.md")
    brief_timeout_seconds: float = Field(default=15, gt=0)
    # 이 기간 안의 대화만, 카톡·문자 각각 최근 이만큼까지 넣는다
    brief_days: int = Field(default=30, ge=1)
    brief_max_messages: int = Field(default=40, ge=1)
    brief_calls: int = Field(default=3, ge=0)   # 통화 전사문은 최근 이만큼

    # 네이버 캘린더 (CalDAV). 위키 빌더가 고른 일정을 넣는다. 계정은 MAIL_ACCOUNTS 의 name 으로 고르고,
    # 비우면 첫 번째 네이버 계정을 쓴다. 메일과 같은 애플리케이션 비밀번호로 로그인된다.
    calendar_enabled: bool = True
    calendar_account: str = ""
    calendar_name: str = "내 캘린더"
    calendar_caldav_url: str = "https://caldav.calendar.naver.com"

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
    def attachments_dir(self) -> Path:
        # kept 메일의 첨부를 풀어 두는 곳. Buzz 에이전트가 --add-dir 로 읽는다
        return self.data_dir / "attachments"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "events.db"

    @property
    def wiki_dir(self) -> Path:
        return self.data_dir / "wiki"


@lru_cache
def get_settings() -> Settings:
    return Settings()
