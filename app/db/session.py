from __future__ import annotations

import sqlite3
from contextlib import closing

from app.core.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,            -- call | message | email
    dedup_key    TEXT NOT NULL UNIQUE,
    payload      TEXT NOT NULL,            -- JSON 메타데이터
    raw_path     TEXT,                     -- data_dir 기준 원본 경로
    status       TEXT NOT NULL DEFAULT 'pending',
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
