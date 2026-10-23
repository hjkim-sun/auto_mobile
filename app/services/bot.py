"""Buzz 봇이 보내는 글: kept 알림과 명령 응답. Buzz 와 직접 통신하지 않아 테스트하기 쉽다.

명령 (DM 으로 보낸다)
  도움말            명령 목록
  오늘              오늘(TIMEZONE) 남긴 것 목록
  문자|카톡|메일|통화 [n]  종류별 최근 남긴 것 n건 (기본 5)
  버린 [n]          최근 버린 것 n건. 판별이 놓친 게 없는지 볼 때
  상태              상태별 건수
  #123              이벤트 123 의 본문 (통화는 전사)
  위키 질문          LLM 위키에 묻기 (app/services/wiki.py)
  일정              캘린더에 넣을지 답을 기다리는 일정
  등록 N / 안함 N    일정 N 을 네이버 캘린더에 넣는다 / 넣지 않는다
  새대화            에이전트 대화 맥락을 비운다 (app/jobs/buzz_bot.py 가 처리)
명령이 아닌 말은 app/services/agent.py 의 에이전트가 답한다.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, time, timezone

from app.core.config import get_settings
from app.db.session import connect
from app.repositories import calendar_items, events
from app.services import calendar, wiki
from app.services.render import call_party, call_transcript, render

SOURCES = {"문자": "message", "카톡": "kakao", "메일": "email", "통화": "call"}
LABELS = {"message": "문자", "kakao": "카톡", "email": "메일", "call": "통화"}
PREVIEW_CHARS = 300
DETAIL_CHARS = 3500
HELP = """명령
• 오늘 — 오늘 남긴 것
• 문자 / 카톡 / 메일 / 통화 [n] — 종류별 최근 남긴 것 (기본 5건)
• 버린 [n] — 최근 버린 것
• 상태 — 상태별 건수
• #번호 — 원문 보기 (통화는 전사)
• 위키 질문 — 위키에 묻기 (예: 위키 홍길동과 약속 언제야?)
• 일정 — 캘린더에 넣을지 답을 기다리는 일정
• 등록 N / 안함 N — 일정 N 을 캘린더에 넣기 / 넣지 않기
• 새대화 — 에이전트와의 대화 맥락을 비우기
그 밖의 말은 에이전트(Claude)가 위키와 자료를 보고 답합니다."""
WEEKDAYS = "월화수목금토일"


def _local(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(get_settings().tz).strftime("%m-%d %H:%M")


def summary_line(event: sqlite3.Row) -> str:
    """목록에 쓰는 한 줄."""
    p = json.loads(event["payload"])
    label = LABELS.get(event["source"], event["source"])
    if event["source"] == "message":
        what = f"{p.get('from')}: {(p.get('text') or '(첨부)').splitlines()[0][:60]}"
    elif event["source"] == "kakao" and p.get("log_ids"):   # 방 단위 판단(kakao_judge)이 만든 이벤트
        what = f"{p.get('room')}: {p['claude']['reason'][:60]}"
    elif event["source"] == "kakao":
        what = f"{p.get('sender')}: {((p.get('text') or '').splitlines() or [''])[0][:60]}"
    elif event["source"] == "email":
        what = f"{_sender(p.get('from', ''))}: {p.get('subject') or '(제목 없음)'}"
    else:
        what = call_party(p) or p.get("filename", "")
    return f"#{event['id']} [{label}] {_local(event['created_at'])} {what}"


def _sender(value: str) -> str:
    # "홍길동 <hong@example.com>" → 홍길동
    m = re.match(r'\s*"?([^"<]+?)"?\s*<', value)
    return m.group(1) if m else value


def body(event: sqlite3.Row) -> str:
    if event["source"] == "call":
        return call_transcript(event) or "(전사 중이거나 전사 결과가 없음)"
    return render(event) or "(본문 없음)"


def notification(event: sqlite3.Row) -> str:
    """kept 판별 직후 보내는 알림."""
    text = body(event).strip()
    if len(text) > PREVIEW_CHARS:
        text = text[:PREVIEW_CHARS] + f"…\n(전체: #{event['id']})"
    return f"{summary_line(event)}\n\n{text}"


def _when(item: sqlite3.Row) -> str:
    def one(value: str, with_date: bool = True) -> str:
        d = datetime.fromisoformat(value)
        day = f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"
        if "T" not in value:
            return day
        return f"{day} {d:%H:%M}" if with_date else f"{d:%H:%M}"
    text = one(item["start"])
    if item["end"]:
        same_day = item["end"][:10] == item["start"][:10]
        text += "~" + one(item["end"], with_date=not same_day)
    return text


def calendar_line(item: sqlite3.Row) -> str:
    where = f" @ {item['location']}" if item["location"] else ""
    return f"[일정 {item['id']}] {_when(item)} {item['title']}{where} (#{item['event_id']})"


def calendar_message(item: sqlite3.Row) -> str:
    """위키 빌더가 고른 일정: 자동으로 넣은 것은 알리고, 애매한 것은 묻는다."""
    if item["status"] == "added":
        return f"캘린더에 넣었습니다\n{calendar_line(item)}\n이유: {item['reason']}\n잘못 넣었으면 캘린더에서 지워 주세요."
    return (f"캘린더에 넣을까요?\n{calendar_line(item)}\n{item['reason']}\n"
            f"넣으려면 \"등록 {item['id']}\", 넣지 않으려면 \"안함 {item['id']}\"")


def calendar_command(word: str, item_id: int) -> str:
    item = calendar_items.get(item_id)
    if item is None:
        return f"일정 {item_id} 없음"
    if item["status"] == "added":
        return f"이미 넣었습니다\n{calendar_line(item)}"
    if word == "안함":
        calendar_items.set_status(item_id, "declined")
        return f"넣지 않습니다\n{calendar_line(item)}"
    calendar_items.set_status(item_id, "pending")
    try:
        uid = calendar.add(item)
    except Exception as e:
        # pending 으로 남으므로 위키 빌더가 다음 주기에 다시 넣는다.
        calendar_items.set_error(item_id, repr(e))
        return f"캘린더에 넣지 못했습니다. 잠시 뒤 다시 시도합니다: {e}"
    calendar_items.set_status(item_id, "added", caldav_uid=uid)
    return f"캘린더에 넣었습니다\n{calendar_line(item)}"


def _recent(where: str, args: list, limit: int) -> list[sqlite3.Row]:
    with closing(connect()) as conn:
        return conn.execute(f"SELECT * FROM events WHERE {where} ORDER BY id DESC LIMIT ?",
                            (*args, limit)).fetchall()


def _listing(title: str, rows: list[sqlite3.Row]) -> str:
    if not rows:
        return f"{title}: 없음"
    return "\n".join([f"{title} ({len(rows)}건)", *(summary_line(r) for r in rows)])


def handle(text: str) -> str:
    """명령 한 건에 대한 답. 명령이 아니면 도움말."""
    return command(text) or HELP


def command(text: str) -> str | None:
    """명령이면 답, 명령이 아니면 None (Buzz 봇은 에이전트에 넘긴다)."""
    text = text.strip()
    word, _, rest = text.partition(" ")
    n = int(rest) if rest.strip().isdigit() else 5
    n = max(1, min(n, 30))

    if m := re.fullmatch(r"#?(\d+)", text):
        event = events.get(int(m.group(1)))
        if event is None:
            return f"#{m.group(1)} 없음"
        content = body(event)
        if len(content) > DETAIL_CHARS:
            content = content[:DETAIL_CHARS] + "\n…(잘림)"
        return f"{summary_line(event)} · {event['status']}\n\n{content}"
    if word == "오늘":
        tz = get_settings().tz
        start = datetime.combine(datetime.now(tz).date(), time(), tz).astimezone(timezone.utc)
        rows = _recent("status = 'kept' AND created_at >= ?", [start.isoformat(timespec="seconds")], 100)
        return _listing("오늘 남긴 것", rows)
    if word in SOURCES:
        return _listing(f"최근 {word}", _recent("status = 'kept' AND source = ?", [SOURCES[word]], n))
    if word == "버린":
        return _listing("최근 버린 것", _recent("status = 'discarded'", [], n))
    if word in ("등록", "안함") and rest.strip().isdigit():
        return calendar_command(word, int(rest))
    if word == "일정":
        rows = calendar_items.with_status("asking")
        if not rows:
            return "답을 기다리는 일정 없음"
        return "\n".join([f"넣을지 답을 기다리는 일정 ({len(rows)}건)", *(calendar_line(r) for r in rows),
                          "\"등록 N\" 또는 \"안함 N\""])
    if word == "위키" and rest.strip():
        try:
            return wiki.ask(rest.strip())[:DETAIL_CHARS]
        except wiki.WikiError as e:
            return f"위키 질문 실패: {e}"
    if word == "상태":
        counts = events.status_counts()
        return "\n".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "이벤트 없음"
    if text in ("도움말", "help", "?"):
        return HELP
    return None
