"""Buzz 에이전트(app/services/agent.py)가 쓰는 도구. app/jobs/agent_mcp.py 가 MCP 로 내놓는다.

에이전트는 위키(data/wiki)를 Read/Grep 으로 직접 읽고, 원본 이벤트와 캘린더는 이 도구로만 다룬다.
쓰기는 캘린더 결정(decide_calendar) 하나뿐이다.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone

from app.db.session import connect
from app.repositories import calendar_items, events
from app.services import bot

SOURCES = {"message", "kakao", "email", "call"}
STATUSES = {"transcribing", "pending", "processing", "kept", "discarded", "failed"}


class ToolError(ValueError):
    pass


def search_events(query: str | None = None, source: str | None = None, status: str | None = "kept",
                  days: int | None = None, limit: int = 20) -> str:
    """이벤트 목록 (최신순). query 는 문자·카톡·메일 원문(payload)에서 찾는다. 통화 전사는 get_event 나 위키로 본다."""
    sql, args = "SELECT * FROM events WHERE 1=1", []
    if source:
        if source not in SOURCES:
            raise ToolError(f"source 는 {sorted(SOURCES)} 중 하나")
        sql += " AND source = ?"
        args.append(source)
    if status:
        if status not in STATUSES:
            raise ToolError(f"status 는 {sorted(STATUSES)} 중 하나")
        sql += " AND status = ?"
        args.append(status)
    if days:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        sql += " AND created_at >= ?"
        args.append(since.isoformat(timespec="seconds"))
    if query:
        sql += " AND payload LIKE ?"
        args.append(f"%{query}%")
    limit = max(1, min(int(limit), 100))
    with closing(connect()) as conn:
        rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    if not rows:
        return "없음"
    return "\n".join(f"{bot.summary_line(r)} · {r['status']}" for r in rows)


def get_event(event_id: int) -> str:
    event = events.get(int(event_id))
    if event is None:
        raise ToolError(f"#{event_id} 없음")
    return f"{bot.summary_line(event)} · {event['status']}\n\n{bot.body(event)}"


def pending_calendar() -> str:
    rows = calendar_items.with_status("asking")
    return "\n".join(bot.calendar_line(r) for r in rows) or "답을 기다리는 일정 없음"


def decide_calendar(item_id: int, decision: str) -> str:
    """주인이 정한 대로 일정을 캘린더에 넣거나(add) 넣지 않는다(decline)."""
    word = {"add": "등록", "decline": "안함"}.get(decision)
    if word is None:
        raise ToolError("decision 은 add 또는 decline")
    return bot.calendar_command(word, int(item_id))


def status_counts() -> str:
    counts = events.status_counts()
    return "\n".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "이벤트 없음"


TOOLS = {
    "search_events": (search_events, {
        "description": "문자·카톡·메일·통화 이벤트 목록을 최신순으로 본다. 기본은 남긴 것(kept)만."
                       " query 는 문자·카톡·메일 원문에서 찾는 단어. 통화 내용은 get_event 로 본다.",
        "properties": {
            "query": {"type": "string"},
            "source": {"enum": sorted(SOURCES)},
            "status": {"enum": sorted(STATUSES), "description": "기본 kept"},
            "days": {"type": "integer", "description": "최근 며칠"},
            "limit": {"type": "integer", "description": "기본 20, 최대 100"},
        },
    }),
    "get_event": (get_event, {
        "description": "이벤트 하나의 원문. 통화는 전사문.",
        "properties": {"event_id": {"type": "integer"}},
        "required": ["event_id"],
    }),
    "pending_calendar": (pending_calendar, {
        "description": "캘린더에 넣을지 주인의 답을 기다리는 일정 목록.",
        "properties": {},
    }),
    "decide_calendar": (decide_calendar, {
        "description": "주인이 분명히 정한 일정만 처리한다. add 는 네이버 캘린더에 넣고, decline 은 넣지 않음으로 표시한다.",
        "properties": {"item_id": {"type": "integer"}, "decision": {"enum": ["add", "decline"]}},
        "required": ["item_id", "decision"],
    }),
    "status_counts": (status_counts, {
        "description": "상태별 이벤트 건수.",
        "properties": {},
    }),
}
