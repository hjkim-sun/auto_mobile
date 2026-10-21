"""웹 흐름 화면(/web/flow)에 보여 줄 숫자와 최근 기록.

시나리오 1: 수신(문자·메일·통화 등) → Jev 판별 → 위키 정리·캘린더 일정 → Buzz 알림
시나리오 2: Buzz 에서 주인이 질문 → 에이전트가 위키로 답 → 일정 등록/안함
단계마다 걸린 시간은 events.created_at(수신), events.updated_at(판별을 마친 시각), wiki_ingests.ingested_at,
notifications.sent_at 으로 잰다. 판별 뒤에 상태가 바뀌지 않으므로 updated_at 은 판별 시각으로 본다.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime

from app.db.session import connect
from app.repositories import agent_turns
from app.services import wiki_graph

RECENT_EVENTS = 15
RECENT_TURNS = 6
LATENCY_EVENTS = 30
TEXT_CHARS = 280


def _seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())


def _median(values: list[float]) -> float | None:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def _label(row: sqlite3.Row) -> str:
    payload = json.loads(row["payload"])
    if row["source"] == "call":
        return payload.get("name") or payload.get("number") or (payload.get("filename") or "통화").rsplit(".", 1)[0]
    if row["source"] == "kakao":
        return payload.get("room") or payload.get("sender") or "카톡"
    if row["source"] == "email":
        return payload.get("subject") or payload.get("account") or "메일"
    return payload.get("from") or payload.get("sender") or row["source"]


def _useful(row: sqlite3.Row) -> float | None:
    verdict = json.loads(row["verdict"]) if row["verdict"] else {}
    useful = verdict.get("useful")
    return useful if isinstance(useful, (int, float)) else None


def _counts(conn: sqlite3.Connection) -> dict:
    by_source: dict = {}
    for r in conn.execute("SELECT source, status, COUNT(*) AS n FROM events GROUP BY source, status"):
        by_source.setdefault(r["source"], {})[r["status"]] = r["n"]
    calendar: dict = {}
    for r in conn.execute("SELECT status, COUNT(*) AS n FROM calendar_items GROUP BY status"):
        calendar[r["status"]] = r["n"]
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "sources": by_source,
        "kept": sum(s.get("kept", 0) for s in by_source.values()),
        "discarded": sum(s.get("discarded", 0) for s in by_source.values()),
        "waiting": sum(s.get(k, 0) for s in by_source.values() for k in ("transcribing", "pending", "processing")),
        "failed": sum(s.get("failed", 0) for s in by_source.values()),
        "wiki_ingested": one("SELECT COUNT(*) FROM wiki_ingests"),
        "notified": one("SELECT COUNT(*) FROM notifications"),
        "calendar": calendar,
        "calendar_asked": one("SELECT COUNT(*) FROM calendar_items WHERE decision = 'ask'"),
    }


def _recent(conn: sqlite3.Connection, titles: dict[int, str]) -> list[dict]:
    rows = conn.execute(
        "SELECT e.*, w.ingested_at, n.sent_at FROM events e"
        " LEFT JOIN wiki_ingests w ON w.event_id = e.id LEFT JOIN notifications n ON n.event_id = e.id"
        " ORDER BY e.id DESC LIMIT ?", (RECENT_EVENTS,)).fetchall()
    ids = [r["id"] for r in rows]
    calendar: dict = {}
    if ids:
        for c in conn.execute(f"SELECT event_id, title, start, decision, status FROM calendar_items"
                              f" WHERE event_id IN ({', '.join('?' * len(ids))}) ORDER BY id", ids):
            calendar.setdefault(c["event_id"], []).append(dict(c))
    judged = lambda r: r["updated_at"] if r["status"] in ("kept", "discarded") else None  # noqa: E731
    return [{"id": r["id"], "source": r["source"], "status": r["status"], "useful": _useful(r),
             "title": titles.get(r["id"]) or _label(r), "received_at": r["created_at"],
             "judged_at": judged(r), "wiki_at": r["ingested_at"], "notified_at": r["sent_at"],
             "calendar": calendar.get(r["id"], [])} for r in rows]


def _latency(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT e.source, e.created_at, e.updated_at, w.ingested_at, n.sent_at FROM events e"
        " LEFT JOIN wiki_ingests w ON w.event_id = e.id LEFT JOIN notifications n ON n.event_id = e.id"
        " WHERE e.status = 'kept' ORDER BY e.id DESC LIMIT ?", (LATENCY_EVENTS,)).fetchall()
    # 처음 띄울 때 밀려 있던 자료가 섞이지 않게 최근 것만 본다. 통화는 전사 시간이 판별에 들어가므로 따로 본다
    return {
        "judge": _median([_seconds(r["created_at"], r["updated_at"]) for r in rows if r["source"] != "call"]),
        "judge_call": _median([_seconds(r["created_at"], r["updated_at"]) for r in rows if r["source"] == "call"]),
        "wiki": _median([_seconds(r["updated_at"], r["ingested_at"]) for r in rows]),
        "notify": _median([_seconds(r["updated_at"], r["sent_at"]) for r in rows]),
    }


def _turns() -> list[dict]:
    return [{"id": t["id"], "owner": bool(t["is_owner"]), "ok": bool(t["ok"]),
             "question": t["question"][:TEXT_CHARS], "answer": (t["answer"] or "")[:TEXT_CHARS],
             "at": t["started_at"], "seconds": _seconds(t["started_at"], t["finished_at"])}
            for t in agent_turns.recent(RECENT_TURNS)]


def stats() -> dict:
    g = wiki_graph.graph()
    titles = {n["event_id"]: n["title"] for n in g["nodes"] if n.get("event_id") is not None}
    with closing(connect()) as conn:
        counts, recent, latency = _counts(conn), _recent(conn, titles), _latency(conn)
    counts["agent_turns"] = agent_turns.count()
    counts["wiki_pages"] = g["counts"]
    counts["wiki_links"] = len(g["links"])
    return {"counts": counts, "latency": latency, "recent": recent, "turns": _turns()}
