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
