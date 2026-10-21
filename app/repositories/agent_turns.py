"""agent_turns 테이블: Buzz 에이전트가 답한 말. 웹 흐름 화면이 최근 질문과 답을 보여 준다."""
from __future__ import annotations

import sqlite3
from contextlib import closing

from app.db.session import connect
from app.repositories.events import now


def record(channel: str, is_owner: bool, question: str, answer: str, ok: bool, started_at: str) -> int:
    with closing(connect()) as conn:
        return conn.execute(
            "INSERT INTO agent_turns (channel, is_owner, question, answer, ok, started_at, finished_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (channel, int(is_owner), question, answer, int(ok), started_at, now()),
        ).fetchone()["id"]


def recent(limit: int = 10) -> list[sqlite3.Row]:
    with closing(connect()) as conn:
        return conn.execute("SELECT * FROM agent_turns ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def count() -> int:
    with closing(connect()) as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM agent_turns").fetchone()["n"]
