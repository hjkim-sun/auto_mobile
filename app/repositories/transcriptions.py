"""transcriptions 테이블: 전사 요청 1건당 1행.

status 흐름
  queued → running → done
                   → failed
"""
from __future__ import annotations

import sqlite3
from contextlib import closing

from app.db.session import connect
from app.repositories.events import now


def insert(input_path: str, output_dir: str, language: str | None) -> sqlite3.Row:
    ts = now()
    with closing(connect()) as conn:
        return conn.execute(
            "INSERT INTO transcriptions (input_path, output_dir, language, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?) RETURNING *",
            (input_path, output_dir, language, ts, ts),
        ).fetchone()


def get(job_id: int) -> sqlite3.Row | None:
    with closing(connect()) as conn:
        return conn.execute("SELECT * FROM transcriptions WHERE id = ?", (job_id,)).fetchone()


def start(job_id: int) -> None:
    with closing(connect()) as conn:
        conn.execute("UPDATE transcriptions SET status = 'running', updated_at = ? WHERE id = ?",
                     (now(), job_id))


def finish(job_id: int, text_path: str, duration: float) -> None:
    with closing(connect()) as conn:
        conn.execute(
            "UPDATE transcriptions SET status = 'done', text_path = ?, duration = ?, error = NULL,"
            " updated_at = ? WHERE id = ?",
            (text_path, duration, now(), job_id),
        )


def fail(job_id: int, error: str) -> None:
    with closing(connect()) as conn:
        conn.execute("UPDATE transcriptions SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                     (error, now(), job_id))


def unfinished_ids() -> list[int]:
    """API 가 처리 도중 재시작됐을 때 다시 돌릴 작업. 오래된 것부터."""
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT id FROM transcriptions WHERE status IN ('queued', 'running') ORDER BY id").fetchall()
        return [r["id"] for r in rows]


def latest_for(input_path: str) -> sqlite3.Row | None:
    """이 파일의 가장 최근 전사 작업."""
    with closing(connect()) as conn:
        return conn.execute("SELECT * FROM transcriptions WHERE input_path = ? ORDER BY id DESC LIMIT 1",
                            (input_path,)).fetchone()
