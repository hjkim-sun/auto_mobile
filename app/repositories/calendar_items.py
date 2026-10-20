"""calendar_items 테이블: 위키 빌더가 고른 캘린더 일정 후보.

status 흐름
  decision=add: pending → added            (등록 실패면 pending 으로 남아 다음 주기에 다시)
  decision=ask: asking  → pending → added  (사용자가 "등록 N")
                        → declined         (사용자가 "안함 N")
"""
from __future__ import annotations

import sqlite3
from contextlib import closing

from app.db.session import connect
from app.repositories.events import now

FIELDS = ("event_id", "title", "start", "end", "location", "note", "decision", "reason")


def insert(item: dict) -> int:
    ts = now()
    status = "pending" if item["decision"] == "add" else "asking"
    with closing(connect()) as conn:
        return conn.execute(
            f"INSERT INTO calendar_items ({', '.join(FIELDS)}, status, created_at, updated_at)"
            f" VALUES ({', '.join('?' * len(FIELDS))}, ?, ?, ?) RETURNING id",
            (*(item.get(f) for f in FIELDS), status, ts, ts),
        ).fetchone()["id"]


def get(item_id: int) -> sqlite3.Row | None:
    with closing(connect()) as conn:
        return conn.execute("SELECT * FROM calendar_items WHERE id = ?", (item_id,)).fetchone()


def with_status(*statuses: str) -> list[sqlite3.Row]:
    with closing(connect()) as conn:
        return conn.execute(
            f"SELECT * FROM calendar_items WHERE status IN ({', '.join('?' * len(statuses))}) ORDER BY id",
            statuses).fetchall()


def upcoming(since: str) -> list[sqlite3.Row]:
    """since(YYYY-MM-DD) 이후 일정 후보. 같은 일정을 다시 고르지 않게 위키 빌더에 보여 준다."""
    with closing(connect()) as conn:
        return conn.execute("SELECT * FROM calendar_items WHERE start >= ? ORDER BY start",
                            (since,)).fetchall()


def unnotified() -> list[sqlite3.Row]:
    """Buzz 로 알리거나 물을 것: 자동 등록된 것과 답을 기다리는 것."""
    with closing(connect()) as conn:
        return conn.execute(
            "SELECT * FROM calendar_items WHERE notified = 0"
            " AND ((decision = 'add' AND status = 'added') OR status = 'asking') ORDER BY id").fetchall()


def set_status(item_id: int, status: str, caldav_uid: str | None = None, error: str | None = None) -> None:
    with closing(connect()) as conn:
        conn.execute("UPDATE calendar_items SET status = ?, caldav_uid = COALESCE(?, caldav_uid), error = ?,"
                     " updated_at = ? WHERE id = ?", (status, caldav_uid, error, now(), item_id))


def set_error(item_id: int, error: str) -> None:
    with closing(connect()) as conn:
        conn.execute("UPDATE calendar_items SET error = ?, updated_at = ? WHERE id = ?", (error, now(), item_id))


def mark_notified(item_id: int) -> None:
    with closing(connect()) as conn:
        conn.execute("UPDATE calendar_items SET notified = 1 WHERE id = ?", (item_id,))
