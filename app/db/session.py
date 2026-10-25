from __future__ import annotations

import sqlite3
from contextlib import closing

from app.core.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,            -- call | message | kakao | email
    dedup_key    TEXT NOT NULL UNIQUE,
    payload      TEXT NOT NULL,            -- JSON 메타데이터
    raw_path     TEXT,                     -- data_dir 기준 원본 경로
    status       TEXT NOT NULL DEFAULT 'pending',  -- transcribing(통화 전사 중) | pending | processing | kept | discarded | filed(카톡, 방 단위 판단) | failed
    verdict      TEXT,                     -- JSON 판별 결과
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    available_at TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_queue ON events (status, available_at);

CREATE TABLE IF NOT EXISTS mail_cursors (    -- 메일 폴러가 어디까지 가져왔는지
    account      TEXT NOT NULL,
    mailbox      TEXT NOT NULL,
    uidvalidity  INTEGER NOT NULL,
    last_uid     INTEGER NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (account, mailbox)
);

CREATE TABLE IF NOT EXISTS transcriptions (
    id           INTEGER PRIMARY KEY,
    input_path   TEXT NOT NULL,            -- data_dir 기준 음성 파일 경로
    output_dir   TEXT NOT NULL,            -- data_dir 기준 결과 디렉토리
    language     TEXT,                     -- NULL 이면 자동 감지
    status       TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
    text_path    TEXT,                     -- data_dir 기준 결과 .txt
    duration     REAL,                     -- 음성 길이(초)
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS buzz_state (      -- Buzz 봇이 기억하는 값 (DM 채널, 읽은 위치 등)
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (   -- Buzz 로 알린 이벤트. 한 이벤트는 한 번만 알린다
    event_id     INTEGER PRIMARY KEY,
    sent_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wiki_ingests (    -- 위키에 넣은 이벤트. 한 이벤트는 한 번만 넣는다
    event_id     INTEGER PRIMARY KEY,
    ingested_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar_items (  -- 위키 빌더가 고른 캘린더 일정 후보
    id           INTEGER PRIMARY KEY,
    event_id     INTEGER NOT NULL,         -- 근거 자료 (events.id)
    title        TEXT NOT NULL,
    start        TEXT NOT NULL,            -- 2026-10-22T20:00 또는 2026-10-30 (하루 종일), TIMEZONE 기준
    end          TEXT,
    location     TEXT,
    note         TEXT,
    decision     TEXT NOT NULL,            -- add (바로 등록) | ask (사용자에게 묻기)
    reason       TEXT,
    status       TEXT NOT NULL,            -- pending (등록 대기) | asking (답 대기) | added | declined
    caldav_uid   TEXT,
    error        TEXT,
    notified     INTEGER NOT NULL DEFAULT 0,  -- Buzz 로 알렸거나 물었는지
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS call_briefs (     -- 전화 수신 브리핑. 사람별로 미리 만들어 둔 최근 대화 3줄 요약
    person        TEXT PRIMARY KEY,        -- 연락처·카톡 이름. 이름을 모르면 번호(숫자만)
    number        TEXT,                    -- 숫자만. 모르면 NULL
    is_updated    INTEGER NOT NULL DEFAULT 1,  -- 마지막 요약 뒤에 카톡·문자·통화가 들어왔으면 1. 요약 배치가 1 인 사람만 다시 요약한다
    summary       TEXT,
    sms           INTEGER,                 -- 요약에 넣은 문자 수
    kakao         INTEGER,                 -- 요약에 넣은 카톡 수
    calls         INTEGER,                 -- 요약에 넣은 통화 수
    updated_at    TEXT NOT NULL,           -- 마지막으로 is_updated 를 켠 시각
    summarized_at TEXT
);
CREATE INDEX IF NOT EXISTS call_briefs_number ON call_briefs (number);

CREATE TABLE IF NOT EXISTS agent_turns (     -- Buzz 에이전트가 답한 말. 웹 흐름 화면(/web/flow)에 보여 준다
    id           INTEGER PRIMARY KEY,
    channel      TEXT NOT NULL,
    is_owner     INTEGER NOT NULL,
    question     TEXT NOT NULL,
    answer       TEXT,
    ok           INTEGER NOT NULL,         -- 에이전트 오류면 0
    started_at   TEXT NOT NULL,
    finished_at  TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_settings().db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    get_settings().data_dir.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
