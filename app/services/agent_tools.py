"""Buzz 에이전트(app/services/agent.py)가 쓰는 도구. app/jobs/agent_mcp.py 가 MCP 로 내놓는다.

에이전트는 위키(data/wiki)와 kept 메일의 첨부(data/attachments)를 파일 도구로 직접 읽고, 원본 이벤트와 캘린더는 이 도구로만 다룬다.
캘린더를 바꾸는 도구(WRITE)는 주인과의 대화에서만 내놓는다(AGENT_OWNER_ENV=1, app/services/agent.py 가 정한다).
"""
from __future__ import annotations

import os
from contextlib import closing
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.db.session import connect
from app.repositories import calendar_items, events
from app.services import bot, calendar, email_parse

SOURCES = {"message", "kakao", "email", "call"}
STATUSES = {"transcribing", "pending", "processing", "kept", "discarded", "failed"}
AGENT_OWNER_ENV = "AUTO_MOBILE_AGENT_OWNER"
WRITE = {"decide_calendar", "add_calendar", "remove_calendar"}


class ToolError(ValueError):
    pass


def owner_session() -> bool:
    return os.environ.get(AGENT_OWNER_ENV) == "1"


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


def attachment_note(event) -> str:
    """kept 메일에서 꺼내 둔 첨부 경로. 에이전트가 Read 로 연다(--add-dir ATTACHMENTS)."""
    if event["source"] != "email" or not event["raw_path"]:
        return ""
    paths = email_parse.saved_attachments(get_settings().data_dir / event["raw_path"])
    if not paths:
        return ""
    return "\n\n첨부 파일 (Read 로 열어 본다):\n" + "\n".join(f"- {p.resolve()}" for p in paths)


def get_event(event_id: int) -> str:
    event = events.get(int(event_id))
    if event is None:
        raise ToolError(f"#{event_id} 없음")
    return f"{bot.summary_line(event)} · {event['status']}\n\n{bot.body(event)}{attachment_note(event)}"


def pending_calendar() -> str:
    rows = calendar_items.with_status("asking")
    return "\n".join(bot.calendar_line(r) for r in rows) or "답을 기다리는 일정 없음"


def decide_calendar(item_id: int, decision: str) -> str:
    """주인이 정한 대로 일정을 캘린더에 넣거나(add) 넣지 않는다(decline)."""
    word = {"add": "등록", "decline": "안함"}.get(decision)
    if word is None:
        raise ToolError("decision 은 add 또는 decline")
    return bot.calendar_command(word, int(item_id))


def upcoming_calendar(days: int = 30) -> str:
    """오늘부터 days 일 안의 일정 (넣은 것, 넣는 중, 답을 기다리는 것)."""
    today = datetime.now(get_settings().tz).date()
    until = (today + timedelta(days=int(days) + 1)).isoformat()
    rows = [r for r in calendar_items.upcoming(today.isoformat())
            if r["start"] < until and r["status"] in ("added", "pending", "asking")]
    labels = {"added": "", "pending": " · 넣는 중", "asking": " · 답 기다림"}
    return "\n".join(bot.calendar_line(r) + labels[r["status"]] for r in rows) or "일정 없음"


def _check_time(name: str, value: str | None) -> None:
    if value is None:
        return
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise ToolError(f"{name} 는 2026-10-24T13:30 또는 2026-10-24 (하루 종일) 형식") from None


def add_calendar(title: str, start: str, end: str | None = None, location: str | None = None,
                 note: str | None = None, event_id: int | None = None) -> str:
    """주인이 말한 일정을 네이버 캘린더에 바로 넣는다."""
    _check_time("start", start)
    _check_time("end", end)
    if not title.strip():
        raise ToolError("title 이 비어 있음")
    item_id = calendar_items.insert({"event_id": int(event_id or 0), "title": title.strip(), "start": start,
                                     "end": end, "location": location, "note": note, "decision": "add",
                                     "reason": "주인이 Buzz 에서 넣어 달라고 함"})
    calendar_items.mark_notified(item_id)   # 봇이 따로 "넣었습니다" 알림을 보내지 않게
    return bot.calendar_command("등록", item_id)


def remove_calendar(item_id: int) -> str:
    """캘린더에 넣은 일정을 지운다."""
    item = calendar_items.get(int(item_id))
    if item is None:
        raise ToolError(f"일정 {item_id} 없음")
    if item["status"] != "added" or not item["caldav_uid"]:
        calendar_items.set_status(item["id"], "declined")
        return f"캘린더에 없는 일정이라 넣지 않음으로만 표시했습니다\n{bot.calendar_line(item)}"
    calendar.remove(item["caldav_uid"])
    calendar_items.set_status(item["id"], "removed")
    return f"캘린더에서 지웠습니다\n{bot.calendar_line(item)}"


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
    "upcoming_calendar": (upcoming_calendar, {
        "description": "앞으로의 일정 목록(캘린더에 넣은 것과 답을 기다리는 것). [일정 N] 의 N 이 item_id 다.",
        "properties": {"days": {"type": "integer", "description": "오늘부터 며칠, 기본 30"}},
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
    "add_calendar": (add_calendar, {
        "description": "주인이 넣으라고 한 일정을 네이버 캘린더에 바로 넣는다. 대기 목록에 없는 새 일정용."
                       " 시각은 TIMEZONE 기준 2026-10-24T13:30, 하루 종일이면 2026-10-24. end 를 비우면 1시간(하루 종일은 하루).",
        "properties": {
            "title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"},
            "location": {"type": "string"}, "note": {"type": "string"},
            "event_id": {"type": "integer", "description": "근거 자료 번호(#123)가 있으면"},
        },
        "required": ["title", "start"],
    }),
    "remove_calendar": (remove_calendar, {
        "description": "캘린더에 넣은 일정을 지운다. item_id 는 upcoming_calendar 의 [일정 N].",
        "properties": {"item_id": {"type": "integer"}},
        "required": ["item_id"],
    }),
    "status_counts": (status_counts, {
        "description": "상태별 이벤트 건수.",
        "properties": {},
    }),
}
