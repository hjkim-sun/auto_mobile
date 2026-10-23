from __future__ import annotations

import json
import time
from contextlib import closing

import pytest

from app.core.config import get_settings
from app.db.session import connect, init_db
from app.repositories import calendar_items
from app.services import calendar, jev, kakao_history, kakao_judge

ROOMS = [{"chat_id": 11, "kind": "personal", "name": "홍길동"},
         {"chat_id": 22, "kind": "group", "name": "동창", "members": ["홍길동", "김철수"]}]
LOG = 3956491548200000001


def history(client, chat_id, texts, ago=60, start=0):
    """texts 를 ago 초 전부터 1초 간격으로 보낸 메시지로 넣는다."""
    now = int(time.time())
    messages = [{"chat_id": chat_id, "log_id": LOG + (start + i) * 20000, "sent_at": now - ago + i,
                 "author": author, "text": text} for i, (author, text) in enumerate(texts)]
    assert client.post("/v1/ingest/kakao/history", json={"rooms": ROOMS, "messages": messages}).status_code == 200


def new_lines(state):
    return [line for line in state.splitlines() if line.startswith("▶")]


def csv_marks(file):
    return [(r[2], r[4], r[5]) for r in kakao_history.read_rows(get_settings().raw_dir / "kakao" / file)]


def rooms():
    return kakao_history.load_rooms()


@pytest.fixture(autouse=True)
def judge_env(monkeypatch):
    monkeypatch.setenv("KAKAO_JUDGE_SETTLE_SECONDS", "0")
    monkeypatch.setenv("CALENDAR_ENABLED", "true")
    get_settings.cache_clear()
    init_db()


@pytest.fixture
def fake(monkeypatch):
    calls = {"jev": [], "claude": [], "calendar": []}

    def classify(state, questions):
        calls["jev"].append(state)
        useful = 0.9 if any("토요일" in line for line in new_lines(state)) else 0.1
        return {"answers": {"relevance": {"probabilities": {"useful": useful, "noise": 1 - useful}}}}

    def ask_claude(state):
        calls["claude"].append(state)
        if "7시" in state:
            return {"decision": "calendar", "reason": "토요일 저녁 모임을 정함",
                    "calendar": [{"title": "동창 모임", "start": "2026-10-24T19:00", "end": None,
                                  "location": "강남역", "note": None, "decision": "add", "reason": "참석하기로 함"}]}
        return {"decision": "deny", "reason": "날짜를 정하지 않은 제안", "calendar": []}

    def add(item):
        calls["calendar"].append(item["title"])
        return "uid-1"

    monkeypatch.setattr(jev, "classify", classify)
    monkeypatch.setattr(kakao_judge, "ask_claude", ask_claude)
    monkeypatch.setattr(calendar, "add", add)
    return calls


def test_noise_is_marked_by_jev_only(client, fake):
    history(client, 11, [("홍길동", "ㅋㅋㅋ"), ("나", "ㅎㅎ")])
    assert kakao_judge.run_due() == 1
    assert csv_marks("personal/홍길동.csv") == [("ㅋㅋㅋ", "noise 0.10", ""), ("ㅎㅎ", "noise 0.10", "")]
    assert fake["claude"] == []
    assert "unjudged_since" not in rooms()["11"]
    assert kakao_judge.run_due() == 0   # 새 메시지가 없으면 다시 묻지 않는다


def test_claude_adds_calendar_item_and_marks_rows(client, fake):
    history(client, 22, [("홍길동", "이번 토요일 모일까?"), ("김철수", "좋아 7시 강남역"), ("나", "갈게")])
    assert kakao_judge.run_due() == 1
    assert len(new_lines(fake["jev"][0])) == 3 and "단톡방 \"동창\"" in fake["jev"][0]
    with closing(connect()) as conn:
        event = conn.execute("SELECT * FROM events").fetchone()
        notified = conn.execute("SELECT event_id FROM notifications").fetchall()
    assert event["source"] == "kakao" and event["status"] == "kept"
    assert json.loads(event["payload"])["room"] == "동창"
    assert [r["event_id"] for r in notified] == [event["id"]]   # 브리핑하지 않고 캘린더 알림으로 알린다
    [item] = calendar_items.with_status("added")
    assert (item["title"], item["event_id"]) == ("동창 모임", event["id"])
    assert fake["calendar"] == ["동창 모임"]
    assert calendar_items.unnotified()[0]["id"] == item["id"]   # Buzz 봇이 알린다
    code = f"claude-add #{event['id']}"
    assert [m[2] for m in csv_marks("group/22.csv")] == [code] * 3
    assert csv_marks("group/22.csv")[0][1] == "useful 0.90"


def test_claude_deny_is_marked(client, fake):
    history(client, 22, [("홍길동", "토요일에 언제 한번 보자")])
    kakao_judge.run_due()
    assert csv_marks("group/22.csv") == [("토요일에 언제 한번 보자", "useful 0.90", "claude-deny")]
    assert calendar_items.with_status("added", "pending", "asking") == []


def test_only_new_rows_are_judged_and_old_ones_are_context(client, fake):
    history(client, 11, [("홍길동", "토요일 어때")], ago=20 * 3600)   # 맥을 오래 꺼 뒀다 채운 메시지
    history(client, 11, [("홍길동", "ㅋㅋ")], start=1)
    kakao_judge.run_due()
    assert csv_marks("personal/홍길동.csv") == [("토요일 어때", "", ""), ("ㅋㅋ", "noise 0.10", "")]
    state = fake["jev"][0]
    assert "  " + state.splitlines()[4][2:] == state.splitlines()[4]   # 오래된 줄은 ▶ 없이 맥락으로만
    assert state.splitlines()[5].startswith("▶")


def test_many_new_rows_are_split_into_windows(client, fake, monkeypatch):
    monkeypatch.setenv("KAKAO_JUDGE_WINDOW", "20")
    get_settings.cache_clear()
    history(client, 11, [("홍길동", f"잡담 {i}") for i in range(25)])
    kakao_judge.run_due()
    assert [len(new_lines(s)) for s in fake["jev"]] == [20, 5]
    assert len(fake["jev"][1].splitlines()) == 4 + 20   # 두 번째 창도 앞 대화 15건을 맥락으로 본다
    assert all(m[1] == "noise 0.10" for m in csv_marks("personal/홍길동.csv"))


def test_conversation_still_going_waits_for_settle(client, fake, monkeypatch):
    monkeypatch.setenv("KAKAO_JUDGE_SETTLE_SECONDS", "70")
    get_settings.cache_clear()
    history(client, 11, [("홍길동", "ㅋㅋ")])
    assert kakao_judge.run_due() == 0
    monkeypatch.setenv("KAKAO_JUDGE_MAX_WAIT_SECONDS", "0")
    get_settings.cache_clear()
    assert kakao_judge.run_due() == 1


def test_failure_keeps_room_waiting_and_retries_later(client, fake, monkeypatch):
    def broken(state):
        raise RuntimeError("claude down")

    monkeypatch.setattr(kakao_judge, "ask_claude", broken)
    history(client, 22, [("홍길동", "토요일 7시?")])
    assert kakao_judge.run_due() == 0
    room = rooms()["22"]
    assert "unjudged_since" in room and room["retry_at"] > time.time()
    assert kakao_judge.run_due() == 0   # retry_at 전에는 다시 묻지 않는다
    assert csv_marks("group/22.csv") == [("토요일 7시?", "", "")]


def test_phone_notification_in_known_room_is_left_to_room_judge(client, fake):
    from app.jobs.worker import run

    history(client, 11, [("홍길동", "안녕")])
    kakao_judge.run_due()
    note = {"title": "홍길동", "message": "토요일 7시 어때", "ticker": "홍길동 : 토요일 7시 어때", "when": time.time(),
            "channel": "new_message_v6", "extras": {"android.isGroupConversation": False, "chatLogId": LOG + 99 * 20000}}
    client.post("/v1/ingest/kakao", json=note)
    run(once=True)
    with closing(connect()) as conn:
        assert conn.execute("SELECT status FROM events").fetchone()["status"] == "filed"
    assert kakao_judge.run_due() == 1
    assert csv_marks("personal/홍길동.csv")[-1][2].startswith("claude-add #")
