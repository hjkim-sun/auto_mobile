from __future__ import annotations

import json
from contextlib import closing

import pytest

from app.core.config import get_settings
from app.db.session import connect
from app.jobs.worker import run
from app.services import jev

ROOMS = [{"chat_id": 11, "kind": "personal", "name": "홍길동"},
         {"chat_id": 22, "kind": "group", "name": "동창", "members": ["홍길동", "김철수"]},
         {"chat_id": 33, "kind": "group", "name": "회사", "members": ["김철수", "이영희"]}]
HISTORY = [{"chat_id": 22, "log_id": 3956491548200000001, "sent_at": 1792560700, "author": "홍길동", "text": "다음 모임 언제?"},
           {"chat_id": 33, "log_id": 3956491548200100001, "sent_at": 1792560710, "author": "이영희", "text": "보고서 올렸어요"}]


def notification(sender, text, chat_log_id, group):
    return {"title": sender, "message": text, "ticker": f"{sender} : {text}", "when": 1792560802.5,
            "channel": "new_message_v6",
            "extras": {"android.title": sender, "android.text": text, "android.isGroupConversation": group,
                       "chatLogId": chat_log_id}}


def csv_rows(file):
    path = get_settings().raw_dir / "kakao" / file
    return [line.split(",") for line in path.read_text(encoding="utf-8").splitlines()[1:]] if path.exists() else []


def verdicts():
    with closing(connect()) as conn:
        return {json.loads(r["payload"])["text"]: json.loads(r["verdict"])
                for r in conn.execute("SELECT payload, verdict FROM events WHERE source = 'kakao'")}


@pytest.fixture
def fake_jev(monkeypatch):
    rooms_asked = []

    def classify(state, questions):
        if "room" in questions:
            criteria = questions["room"]["criteria"]
            rooms_asked.append(sorted(criteria))
            # 보고서 얘기면 회사 방, 아니면 동창 방
            pick = "room_33" if "보고서" in state else "room_22"
            probs = {k: (0.9 if k == pick else 0.1) for k in criteria}
            return {"answers": {"room": {"probabilities": probs}}}
        return {"answers": {"relevance": {"probabilities": {"useful": 0.9, "noise": 0.1}}}}

    monkeypatch.setattr(jev, "classify", classify)
    return rooms_asked


def test_phone_notifications_are_filed_into_room_csv(client, fake_jev):
    client.post("/v1/ingest/kakao/history", json={"rooms": ROOMS, "messages": HISTORY})
    client.post("/v1/ingest/kakao", json=notification("홍길동", "내일 어때요?", 3956491548218755072, False))
    client.post("/v1/ingest/kakao", json=notification("홍길동", "토요일 좋아", 3956491548219000064, True))
    client.post("/v1/ingest/kakao", json=notification("김철수", "보고서 확인했어요", 3956491548219500032, True))
    client.post("/v1/ingest/kakao", json=notification("박모름", "안녕하세요", 3956491548219900416, True))
    client.post("/v1/ingest/kakao", json=notification("새친구", "처음 연락드려요", 3956491548220300288, False))
    run(once=True)

    assert csv_rows("personal/홍길동.csv") == [["2026-10-21 14:33:22", "홍길동", "내일 어때요?", "3956491548218755072"]]
    assert [r[2] for r in csv_rows("group/22.csv")] == ["다음 모임 언제?", "토요일 좋아"]   # 홍길동의 단톡은 1곳뿐
    assert [r[2] for r in csv_rows("group/33.csv")] == ["보고서 올렸어요", "보고서 확인했어요"]   # 후보 2곳 중 Jev 가 고름
    assert fake_jev == [["room_22", "room_33"]]
    assert [r[2] for r in csv_rows("group/unassigned.csv")] == ["안녕하세요"]
    assert [r[2] for r in csv_rows("personal/새친구.csv")] == ["처음 연락드려요"]

    v = verdicts()
    assert v["토요일 좋아"]["kakao_room"] == {"chat_id": "22", "by": "only_group", "file": "group/22.csv"}
    assert v["보고서 확인했어요"]["kakao_room"]["by"] == "jev"
    assert v["안녕하세요"]["kakao_room"]["by"] == "unassigned"

    # 맥에서 같은 메시지가 정확한 log_id 로 오면 새로 넣지 않고 그 값으로 바꾼다. 알림이 놓친 내 메시지는 채운다
    mac = [{"chat_id": 11, "log_id": 3956491548218755074, "sent_at": 1792560802, "author": "홍길동", "text": "내일 어때요?"},
           {"chat_id": 11, "log_id": 3956491548218855074, "sent_at": 1792560805, "author": "나", "text": "좋아요"},
           {"chat_id": 99, "log_id": 3956491548220300290, "sent_at": 1792560810, "author": "새친구", "text": "처음 연락드려요"}]
    new_friend = {"chat_id": 99, "kind": "personal", "name": "새친구"}
    res = client.post("/v1/ingest/kakao/history", json={"rooms": [ROOMS[0], new_friend], "messages": mac})
    assert res.json()["added"] == 1
    assert [r[3] for r in csv_rows("personal/홍길동.csv")] == ["3956491548218755074", "3956491548218855074"]
    # 알림으로 먼저 만든 임시 방은 맥의 방이 이어받는다
    assert [r[3] for r in csv_rows("personal/새친구.csv")] == ["3956491548220300290"]
    rooms = json.loads((get_settings().raw_dir / "kakao" / "rooms.json").read_text(encoding="utf-8"))
    assert "name:새친구" not in rooms and rooms["99"]["file"] == "personal/새친구.csv"
