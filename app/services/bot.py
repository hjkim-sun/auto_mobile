"""Buzz 봇이 보내는 글: kept 알림과 명령 응답. Buzz 와 직접 통신하지 않아 테스트하기 쉽다.

명령 (DM 으로 보낸다)
  도움말            명령 목록
  오늘              오늘(TIMEZONE) 남긴 것 목록
  문자|메일|통화 [n]  종류별 최근 남긴 것 n건 (기본 5)
  버린 [n]          최근 버린 것 n건. 판별이 놓친 게 없는지 볼 때
  상태              상태별 건수
  #123              이벤트 123 의 본문 (통화는 전사)
  위키 질문          LLM 위키에 묻기 (app/services/wiki.py)
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, time, timezone

from app.core.config import get_settings
from app.db.session import connect
from app.repositories import events
from app.services import wiki
from app.services.render import render

SOURCES = {"문자": "message", "메일": "email", "통화": "call"}
LABELS = {"message": "문자", "email": "메일", "call": "통화"}
PREVIEW_CHARS = 300
DETAIL_CHARS = 3500
HELP = """명령
• 오늘 — 오늘 남긴 것
• 문자 / 메일 / 통화 [n] — 종류별 최근 남긴 것 (기본 5건)
• 버린 [n] — 최근 버린 것
• 상태 — 상태별 건수
• #번호 — 원문 보기 (통화는 전사)
• 위키 질문 — 위키에 묻기 (예: 위키 홍길동과 약속 언제야?)"""


def _local(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(get_settings().tz).strftime("%m-%d %H:%M")


def summary_line(event: sqlite3.Row) -> str:
    """목록에 쓰는 한 줄."""
    p = json.loads(event["payload"])
    label = LABELS.get(event["source"], event["source"])
    if event["source"] == "message":
        what = f"{p.get('from')}: {(p.get('text') or '(첨부)').splitlines()[0][:60]}"
    elif event["source"] == "email":
        what = f"{_sender(p.get('from', ''))}: {p.get('subject') or '(제목 없음)'}"
    else:
        what = p.get("filename", "")
    return f"#{event['id']} [{label}] {_local(event['created_at'])} {what}"


def _sender(value: str) -> str:
    # "홍길동 <hong@example.com>" → 홍길동
    m = re.match(r'\s*"?([^"<]+?)"?\s*<', value)
    return m.group(1) if m else value


def transcript(event: sqlite3.Row) -> str | None:
    with closing(connect()) as conn:
        row = conn.execute("SELECT text_path FROM transcriptions WHERE input_path = ? AND status = 'done'"
                           " ORDER BY id DESC LIMIT 1", (event["raw_path"],)).fetchone()
    if row is None:
        return None
    return (get_settings().data_dir / row["text_path"]).read_text(encoding="utf-8")


def body(event: sqlite3.Row) -> str:
    if event["source"] == "call":
        return transcript(event) or "(전사 중이거나 전사 결과가 없음)"
    return render(event) or "(본문 없음)"


def notification(event: sqlite3.Row) -> str:
    """kept 판별 직후 보내는 알림."""
    text = body(event).strip()
    if len(text) > PREVIEW_CHARS:
        text = text[:PREVIEW_CHARS] + f"…\n(전체: #{event['id']})"
    return f"{summary_line(event)}\n\n{text}"


def _recent(where: str, args: list, limit: int) -> list[sqlite3.Row]:
    with closing(connect()) as conn:
        return conn.execute(f"SELECT * FROM events WHERE {where} ORDER BY id DESC LIMIT ?",
                            (*args, limit)).fetchall()


def _listing(title: str, rows: list[sqlite3.Row]) -> str:
    if not rows:
        return f"{title}: 없음"
    return "\n".join([f"{title} ({len(rows)}건)", *(summary_line(r) for r in rows)])


def handle(command: str) -> str:
    """사용자 DM 한 건에 대한 답."""
    text = command.strip()
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
    if word == "위키" and rest.strip():
        try:
            return wiki.ask(rest.strip())[:DETAIL_CHARS]
        except wiki.WikiError as e:
            return f"위키 질문 실패: {e}"
    if word == "상태":
        counts = events.status_counts()
        return "\n".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "이벤트 없음"
    return HELP
