"""wiki_ingests 테이블: 위키에 넣은 kept 이벤트 기록."""
from __future__ import annotations

import sqlite3
from contextlib import closing

from app.db.session import connect
from app.repositories.events import now


def not_ingested(limit: int = 100) -> list[sqlite3.Row]:
    """아직 위키에 넣지 않은 kept 이벤트. 오래된 것부터."""
    with closing(connect()) as conn:
        return conn.execute(
            "SELECT * FROM events WHERE status = 'kept'"
            " AND id NOT IN (SELECT event_id FROM wiki_ingests) ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()


def mark(event_ids: list[int]) -> None:
    ts = now()
    with closing(connect()) as conn:
        conn.executemany("INSERT OR IGNORE INTO wiki_ingests (event_id, ingested_at) VALUES (?, ?)",
                         [(i, ts) for i in event_ids])


def count() -> int:
    with closing(connect()) as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM wiki_ingests").fetchone()["n"]
