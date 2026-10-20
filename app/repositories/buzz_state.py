"""buzz_state, notifications 테이블: Buzz 봇의 상태와 알림 기록."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from app.db.session import connect
from app.repositories.events import now


def get(key: str, default=None):
    with closing(connect()) as conn:
        row = conn.execute("SELECT value FROM buzz_state WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default


def put(key: str, value) -> None:
    with closing(connect()) as conn:
        conn.execute("INSERT INTO buzz_state (key, value) VALUES (?, ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, json.dumps(value, ensure_ascii=False)))


def max_event_id() -> int:
    with closing(connect()) as conn:
        return conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM events").fetchone()["n"]


def unnotified_kept(after_id: int, limit: int = 20) -> list[sqlite3.Row]:
    """after_id 보다 뒤에 들어온 kept 이벤트 중 아직 알리지 않은 것. 오래된 것부터."""
    with closing(connect()) as conn:
        return conn.execute(
            "SELECT * FROM events WHERE status = 'kept' AND id > ?"
            " AND id NOT IN (SELECT event_id FROM notifications) ORDER BY id LIMIT ?",
            (after_id, limit),
        ).fetchall()


def mark_notified(event_id: int) -> None:
    with closing(connect()) as conn:
        conn.execute("INSERT OR IGNORE INTO notifications (event_id, sent_at) VALUES (?, ?)", (event_id, now()))
