"""call_briefs 테이블: 사람별로 미리 만들어 둔 전화 수신 브리핑 (app/services/call_brief.py).

카톡·문자·통화가 들어오면 mark() 로 그 사람의 is_updated 를 켜고, 요약 배치(worker 의 brief 스레드)가
is_updated=1 인 사람만 claim() 해서 다시 요약한다. 전화가 오면 저장된 요약을 바로 돌려준다.
사람은 이름으로 묶는다. 이름을 모르는 번호(문자만 온 번호 등)는 번호로 두었다가, 통화로 이름을 알게 되면 이름 쪽에 합친다.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing

from app.db.session import connect
from app.repositories.events import now

NAME_SUFFIX_RE = re.compile(r",\s*[+\d][\d\s()-]*$")   # 폰 Content query 는 "이름, 010-1234-5678" 로 보낸다


def normalize_number(number: str | None) -> str:
    digits = re.sub(r"\D", "", number or "")
    return "0" + digits[2:] if digits.startswith("82") and len(digits) > 9 else digits


def clean_name(name: str | None) -> str:
    return NAME_SUFFIX_RE.sub("", (name or "").strip())


def name_for_number(number: str) -> str:
    """번호의 이름. 이미 이름으로 묶인 사람이 있으면 그 이름, 없으면 같은 번호의 지난 통화녹음에 붙은 연락처 이름."""
    target = normalize_number(number)
    if not target:
        return ""
    with closing(connect()) as conn:
        row = conn.execute("SELECT person FROM call_briefs WHERE number = ? AND person != number", (target,)).fetchone()
        if row:
            return row["person"]
        rows = conn.execute("SELECT json_extract(payload, '$.number') AS number, json_extract(payload, '$.name') AS name"
                            " FROM events WHERE source = 'call' AND json_extract(payload, '$.name') IS NOT NULL"
                            " ORDER BY id DESC LIMIT 2000").fetchall()
    return next((clean_name(r["name"]) for r in rows if normalize_number(r["number"]) == target), "")


def mark(name: str | None = None, number: str | None = None) -> str | None:
    """그 사람에게 새 대화가 들어왔다고 표시한다. 묶인 사람(person)을 돌려준다."""
    number = normalize_number(number) or None
    name = clean_name(name) or (name_for_number(number) if number else "")
    person = name or number
    if not person:
        return None
    ts = now()
    with closing(connect()) as conn:
        conn.execute("INSERT INTO call_briefs (person, number, updated_at) VALUES (?, ?, ?)"
                     " ON CONFLICT(person) DO UPDATE SET number = COALESCE(excluded.number, number),"
                     " is_updated = 1, updated_at = excluded.updated_at", (person, number, ts))
        if name and number:   # 번호로만 두었던 사람을 이름 쪽에 합친다
            conn.execute("DELETE FROM call_briefs WHERE person = ? AND person != ?", (number, name))
    return person


def mark_existing(names: set[str]) -> None:
    """이미 브리핑이 있는 사람만 표시한다. 단톡방 말은 1:1 대화가 없는 사람까지 요약을 만들지 않는다."""
    if not names:
        return
    with closing(connect()) as conn:
        conn.execute(f"UPDATE call_briefs SET is_updated = 1, updated_at = ?"
                     f" WHERE person IN ({', '.join('?' * len(names))})", (now(), *names))


def any_brief() -> bool:
    with closing(connect()) as conn:
        return conn.execute("SELECT 1 FROM call_briefs LIMIT 1").fetchone() is not None


def mark_call(raw_path: str) -> None:
    """전사가 끝난(또는 실패한) 통화녹음의 상대를 표시한다."""
    with closing(connect()) as conn:
        row = conn.execute("SELECT json_extract(payload, '$.name') AS name, json_extract(payload, '$.number') AS number"
                           " FROM events WHERE raw_path = ? AND source = 'call'", (raw_path,)).fetchone()
    if row is not None:
        mark(row["name"], row["number"])


def find(name: str | None, number: str | None) -> sqlite3.Row | None:
    name, number = clean_name(name), normalize_number(number)
    with closing(connect()) as conn:
        if name and (row := conn.execute("SELECT * FROM call_briefs WHERE person = ?", (name,)).fetchone()):
            return row
        if number:
            return conn.execute("SELECT * FROM call_briefs WHERE number = ? OR person = ?"
                                " ORDER BY person = number", (number, number)).fetchone()
    return None


def claim_updated() -> list[sqlite3.Row]:
    """is_updated=1 인 사람을 꺼내며 0 으로 돌린다. 요약하는 사이에 새 대화가 오면 다시 1 이 된다."""
    with closing(connect()) as conn:
        return conn.execute("UPDATE call_briefs SET is_updated = 0 WHERE is_updated = 1 RETURNING *").fetchall()


def save(person: str, summary: str, sms: int, kakao: int, calls: int) -> None:
    with closing(connect()) as conn:
        conn.execute("UPDATE call_briefs SET summary = ?, sms = ?, kakao = ?, calls = ?, summarized_at = ?"
                     " WHERE person = ?", (summary, sms, kakao, calls, now(), person))


def unclaim(person: str) -> None:
    """요약에 실패하면 다음 배치에서 다시 하게 둔다."""
    with closing(connect()) as conn:
        conn.execute("UPDATE call_briefs SET is_updated = 1 WHERE person = ?", (person,))
