"""카카오톡 방 단위 판단. 새 메시지가 들어온 방의 최근 대화를 묶어 Jev → Claude 순으로 본다.

메시지마다 묻지 않는다. 메시지마다 최근 대화를 묶어 던지면 창이 거의 겹쳐 같은 판단이 연달아 나오기 때문이다.
1. 방에 새 메시지가 들어가면 kakao_history 가 그 방을 판단 대기로 둔다. 대화가 KAKAO_JUDGE_SETTLE_SECONDS
   (폰 알림이 들어온 방은 실시간이라 KAKAO_JUDGE_PHONE_SETTLE_SECONDS) 동안 멈추거나,
   첫 새 메시지 뒤 KAKAO_JUDGE_MAX_WAIT_SECONDS 가 지나면 판단한다.
2. Jev: 방의 최근 KAKAO_JUDGE_WINDOW 건을 주고, ▶ 를 붙인 새 메시지에 챙길 일이 있는지 묻는다 (prompts/kakao.toml).
   앞 메시지는 맥락으로만 본다. noise 면 새 메시지의 jev 판단 열에 "noise 0.12" 를 적고 끝낸다.
3. Claude: Jev 가 useful 이면 claude -p 가 같은 대화를 위키와 함께 다시 본다 (prompts/kakao_judge.md).
   - 캘린더에 넣을 일정이 있으면 events 에 source=kakao 이벤트(kept, dedup_key kakao_room:방:마지막 log_id)를
     만들고 calendar_items 에 넣는다.
     decision=add 는 바로 네이버 캘린더에 넣고, Buzz 봇이 calendar_items 를 보고 알린다(ask 는 넣을지 묻는다).
     claude 판단 열: "claude-add #이벤트번호" 또는 "claude-ask #이벤트번호".
   - 없으면 오탐으로 보고 claude 판단 열에 "claude-deny" 를 적는다.
새 메시지가 창보다 많으면 창 크기씩 나눠 판단한다. KAKAO_JUDGE_MAX_AGE_HOURS 보다 오래된 메시지는 판단하지 않는다.
워커(app/jobs/worker.py)가 스레드 하나로 KAKAO_JUDGE_POLL_SECONDS 마다 run_due 를 부른다.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from app.core.config import get_settings
from app.repositories import buzz_state, calendar_items, events
from app.services import calendar, jev, kakao_history, prompts, wiki

logger = logging.getLogger(__name__)

NEW = "▶"
ITEM_SCHEMA = wiki.CALENDAR_SCHEMA["properties"]["calendar"]["items"]
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"enum": ["calendar", "deny"]},
        "reason": {"type": "string"},
        "calendar": {"type": "array", "items": {
            "type": "object",
            "properties": {k: v for k, v in ITEM_SCHEMA["properties"].items() if k != "source_id"},
            "required": [k for k in ITEM_SCHEMA["required"] if k != "source_id"],
        }},
    },
    "required": ["decision", "reason", "calendar"],
}


def room_title(room: dict) -> str:
    if room["kind"] == "group":
        return f"단톡방 \"{room['name']}\" (참여자: {', '.join(room.get('members', [])) or '모름'})"
    return f"1:1 대화 (상대: {room['name']})"


def render_window(room: dict, rows: list[list[str]], new_ids: set[str]) -> str:
    lines = [f"[카카오톡 {room_title(room)}]",
             f"지금: {datetime.now(get_settings().tz):%Y-%m-%d %H:%M}",
             f"최근 대화 {len(rows)}건 (오래된 것부터). {NEW} 는 이번에 새로 들어온 메시지, '나'는 주인이다.", ""]
    for when, author, text, log_id, *_ in rows:
        lines.append(f"{NEW if log_id in new_ids else ' '} {when[:16]} {author}: {text}")
    return "\n".join(lines)


def windows(pending: list[int], size: int) -> list[tuple[int, list[int]]]:
    """새 메시지(pending 행 번호)를 창 크기씩 나눈다. [(창 시작 행, 이 창에서 판단할 행들)].
    창은 그 창의 마지막 새 메시지에서 끝나는 size 건이다."""
    out, i = [], 0
    while i < len(pending):
        chunk = [p for p in pending[i:] if p < pending[i] + size]
        out.append((max(0, chunk[-1] + 1 - size), chunk))
        i += len(chunk)
    return out


def ask_jev(state: str) -> float:
    prompt = prompts.load("kakao")
    result = jev.classify(state, prompt.questions)
    return result["answers"][prompt.keep_question]["probabilities"][prompt.keep_choice]


def ask_claude(state: str) -> dict:
    s = get_settings()
    today = datetime.now(s.tz).date().isoformat()
    upcoming = [f"- {r['start']} {r['title']} ({r['status']})" for r in calendar_items.upcoming(today)]
    prompt = "\n".join([s.kakao_judge_prompt_path.read_text(encoding="utf-8"), "",
                        "이미 잡힌 캘린더 후보 (다시 고르지 않는다):", *(upcoming or ["- 없음"]), "",
                        "<source kind=\"카톡\">", state, "</source>"])
    wiki.ensure_wiki()
    return wiki.run_claude(prompt, wiki.READ_TOOLS, JUDGE_SCHEMA)["structured_output"]


def accept(chat_id: str, room: dict, state: str, new_ids: list[str], useful: float, out: dict) -> tuple[int, str]:
    """Claude 가 고른 일정을 이벤트와 calendar_items 로 남기고 add 는 바로 캘린더에 넣는다. (이벤트 id, 판단 코드)"""
    # 폰 알림 이벤트와 같은 source=kakao 로 둔다. log_ids 가 있으면 방 단위 판단으로 만든 이벤트다(render_kakao)
    payload = {"chat_id": chat_id, "room": room["name"], "sender": room["name"], "group": room["kind"] == "group",
               "text": state, "log_ids": new_ids, "claude": out}
    event_id, created = events.insert_event("kakao", f"kakao_room:{chat_id}:{new_ids[-1]}", payload,
                                            status="kept")
    decisions = {item["decision"] for item in out["calendar"]}
    code = f"claude-{'add' if 'add' in decisions else 'ask'} #{event_id}"
    if not created:   # 판단 열을 적기 전에 죽어 같은 창을 다시 본 경우. 일정을 두 번 넣지 않는다
        return event_id, code
    events.finish(event_id, "kept", {"useful": useful, "prompt": "kakao_room", "claude": out})
    # 브리핑 대신 캘린더 알림(bot.calendar_message)으로 알리고, 위키 빌더는 이 이벤트를 위키에 넣는다
    buzz_state.mark_notified(event_id)
    for item in out["calendar"]:
        item_id = calendar_items.insert({**item, "event_id": event_id})
        if item["decision"] == "add" and get_settings().calendar_enabled:
            try:
                calendar_items.set_status(item_id, "added", caldav_uid=calendar.add(calendar_items.get(item_id)))
            except Exception as e:   # pending 으로 남으므로 위키 빌더가 다음 주기에 다시 넣는다
                logger.exception("calendar item %d failed", item_id)
                calendar_items.set_error(item_id, repr(e))
    return event_id, code


def judge_room(chat_id: str, room: dict, marks: dict[str, tuple[str, str]]) -> None:
    """방의 새 메시지를 판단해 marks(log_id → (jev 판단, claude 판단))에 채운다.
    중간에 실패해도 그때까지 판단한 창은 marks 에 남아 다시 묻지 않는다."""
    s = get_settings()
    rows = kakao_history.read_rows(kakao_history.kakao_dir() / room["file"])
    oldest = (datetime.now(s.tz) - timedelta(hours=s.kakao_judge_max_age_hours)).strftime("%Y-%m-%d %H:%M:%S")
    pending = [i for i, r in enumerate(rows) if not r[4] and r[0] >= oldest]
    for start, chunk in windows(pending, s.kakao_judge_window):
        new_ids = [rows[i][3] for i in chunk]
        state = render_window(room, rows[start:chunk[-1] + 1], set(new_ids))
        useful = ask_jev(state)
        if useful < prompts.load("kakao").threshold:
            marks.update(dict.fromkeys(new_ids, (f"noise {useful:.2f}", "")))
            continue
        out = ask_claude(state)
        if out["decision"] == "calendar" and out["calendar"]:
            event_id, code = accept(chat_id, room, state, new_ids, useful, out)
            logger.info("kakao room %s: event %d %s", chat_id, event_id, out["reason"])
        else:
            code = "claude-deny"
            logger.info("kakao room %s: claude denied (%s)", chat_id, out["reason"])
        marks.update(dict.fromkeys(new_ids, (f"useful {useful:.2f}", code)))


def run_due() -> int:
    """판단할 때가 된 방을 판단한다. 판단한 방 수."""
    s = get_settings()
    now = time.time()
    done = 0
    for chat_id, room in kakao_history.due_rooms(now, s.kakao_judge_settle_seconds, s.kakao_judge_phone_settle_seconds,
                                                 s.kakao_judge_max_wait_seconds).items():
        started = time.time()
        marks: dict[str, tuple[str, str]] = {}
        try:
            judge_room(chat_id, room, marks)
        except Exception:
            logger.exception("kakao room %s judge failed", chat_id)
            kakao_history.mark_judged(chat_id, started, marks, retry_at=time.time() + s.kakao_judge_retry_seconds)
            continue
        kakao_history.mark_judged(chat_id, started, marks)
        done += 1
    return done
