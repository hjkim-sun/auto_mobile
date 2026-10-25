from __future__ import annotations

import json

import pytest

from app.core.config import get_settings
from app.repositories import call_briefs
from app.services import call_brief

ROOMS = [{"chat_id": 11, "kind": "personal", "name": "홍길동"},
         {"chat_id": 22, "kind": "group", "name": "동창", "members": ["홍길동", "김철수"]}]


@pytest.fixture
def asked(monkeypatch):
    calls = []

    def summarize(name, conversation):
        calls.append((name, conversation))
        return "• 내일 7시 강남역 약속"

    monkeypatch.setattr(call_brief, "summarize", summarize)
    return calls


def history(now):
    return {"rooms": ROOMS, "messages": [
        {"chat_id": 11, "log_id": 3956491548200001001, "sent_at": now - 3600, "author": "홍길동", "text": "내일 7시 강남역?"},
        {"chat_id": 11, "log_id": 3956491548200101001, "sent_at": now - 3500, "author": "나", "text": "좋아"},
        {"chat_id": 11, "log_id": 3956491548100001001, "sent_at": now - 60 * 86400, "author": "홍길동", "text": "오래된 말"},
        {"chat_id": 22, "log_id": 3956491548200201001, "sent_at": now - 3000, "author": "홍길동", "text": "모임 회비 3만원"},
        {"chat_id": 22, "log_id": 3956491548200301001, "sent_at": now - 2900, "author": "김철수", "text": "남의 말"}]}


def test_brief_collects_kakao_and_sms(client, asked):
    import time
    now = int(time.time())
    assert client.post("/v1/ingest/kakao/history", json=history(now)).status_code == 200
    sms = {"kind": "sms", "id": 7, "from": "+82 10-1234-5678", "date": (now - 1800) * 1000, "text": "회의 자료 보냈어요"}
    other = {"kind": "sms", "id": 8, "from": "01099998888", "date": (now - 1700) * 1000, "text": "다른 사람"}
    for m in (sms, other):
        assert client.post("/v1/ingest/messages", content=json.dumps(m)).status_code == 201

    res = client.post("/v1/brief/call", params={"number": "010-1234-5678", "name": "홍길동"})
    assert res.status_code == 200
    body = res.json()
    assert body["title"] == "📞 홍길동" and body["text"] == "• 내일 7시 강남역 약속"
    assert (body["sms"], body["kakao"]) == (1, 3)
    name, conversation = asked[0]
    text = "\n".join(conversation)
    assert name == "홍길동"
    assert "회의 자료 보냈어요" in text and "모임 회비 3만원" in text and "카톡 나: 좋아" in text
    assert "오래된 말" not in text and "남의 말" not in text and "다른 사람" not in text
    assert conversation == sorted(conversation)


def test_brief_without_conversation_skips_claude(client, asked):
    body = client.post("/v1/brief/call", params={"number": "01055556666"}).json()
    assert body["title"] == "📞 01055556666"
    assert body["text"].startswith("최근 30일")
    assert asked == []


def test_brief_reports_failure(client, monkeypatch):
    def fail(name, conversation):
        raise call_brief.BriefError("timeout")

    monkeypatch.setattr(call_brief, "summarize", fail)
    import time
    sms = {"kind": "sms", "id": 9, "from": "01012345678", "date": int(time.time()) * 1000, "text": "택배 도착"}
    client.post("/v1/ingest/messages", content=json.dumps(sms))
    body = client.post("/v1/brief/call", params={"number": "01012345678"}).json()
    assert body["text"] == "요약 실패." and body["summarized_at"] is None


def test_brief_requires_token(client, monkeypatch):
    monkeypatch.setenv("INGEST_TOKEN", "secret")
    get_settings.cache_clear()
    assert client.post("/v1/brief/call", params={"name": "홍길동"}).status_code == 401


def test_brief_finds_name_from_past_calls(client, asked):
    import time
    now = int(time.time())
    client.post("/v1/ingest/kakao/history", json=history(now))
    res = client.post("/v1/ingest/calls", params={"filename": "a.m4a", "number": "01012345678", "name": "홍길동"},
                      content=b"audio")
    assert res.status_code == 201
    body = client.post("/v1/brief/call", params={"number": "+821012345678"}).json()
    assert body["title"] == "📞 홍길동" and body["kakao"] == 3


def test_normalize_number():
    assert call_briefs.normalize_number("+82 10-1234-5678") == "01012345678"
    assert call_briefs.normalize_number("010-1234-5678") == "01012345678"
    assert call_briefs.normalize_number("1588-0000") == "15880000"


def test_brief_strips_number_from_name(client, asked):
    import time
    client.post("/v1/ingest/kakao/history", json=history(int(time.time())))
    body = client.post("/v1/brief/call", params={"number": "01012345678", "name": "홍길동, 010-1234-5678"}).json()
    assert body["title"] == "📞 홍길동" and body["kakao"] == 3


def flags():
    from contextlib import closing
    from app.db.session import connect
    with closing(connect()) as conn:
        return {r["person"]: (r["number"], r["is_updated"]) for r in conn.execute("SELECT * FROM call_briefs")}


def test_new_conversation_marks_person_updated(client, asked):
    import time
    now = int(time.time())
    client.post("/v1/ingest/kakao/history", json=history(now))
    # 1:1 방의 상대는 표시하고, 단톡방에서만 말한 사람(김철수)은 브리핑이 없으니 만들지 않는다
    assert flags() == {"홍길동": (None, 1)}
    sms = {"kind": "sms", "id": 7, "from": "+82 10-9999-0000", "date": now * 1000, "text": "안녕하세요"}
    client.post("/v1/ingest/messages", content=json.dumps(sms))
    client.post("/v1/ingest/messages", content=json.dumps(sms))   # 같은 문자를 다시 보내도 한 번만
    assert flags()["01099990000"] == ("01099990000", 1)


def test_refresh_summarizes_only_updated_people(client, asked):
    import time
    now = int(time.time())
    client.post("/v1/ingest/kakao/history", json=history(now))
    assert call_brief.refresh() == 1 and len(asked) == 1
    assert flags() == {"홍길동": (None, 0)}
    assert call_brief.refresh() == 0 and len(asked) == 1   # 새 대화가 없으면 요약하지 않는다

    # 단톡방 말도 브리핑이 있는 사람이면 다시 요약하게 한다
    group = {"rooms": ROOMS, "messages": [
        {"chat_id": 22, "log_id": 3956491548200401001, "sent_at": now - 100, "author": "홍길동", "text": "회비 냈어"}]}
    client.post("/v1/ingest/kakao/history", json=group)
    assert flags() == {"홍길동": (None, 1)}
    assert call_brief.refresh() == 1 and len(asked) == 2


def test_brief_returns_saved_summary_without_claude(client, asked):
    import time
    client.post("/v1/ingest/kakao/history", json=history(int(time.time())))
    call_brief.refresh()
    body = client.post("/v1/brief/call", params={"number": "01012345678", "name": "홍길동, 010-1234-5678"}).json()
    assert body["text"] == "• 내일 7시 강남역 약속" and body["kakao"] == 3
    assert body["summarized_at"] is not None and len(asked) == 1
    assert "<h1>📞 홍길동</h1><p>• 내일 7시 강남역 약속</p>" in body["html"] and "카톡 3 · 문자 0 · 통화 0" in body["html"]


def test_refresh_failure_keeps_person_updated(client, monkeypatch):
    def fail(name, conversation):
        raise call_brief.BriefError("timeout")

    monkeypatch.setattr(call_brief, "summarize", fail)
    import time
    client.post("/v1/ingest/kakao/history", json=history(int(time.time())))
    assert call_brief.refresh() == 0
    assert flags() == {"홍길동": (None, 1)}


def test_call_recording_merges_number_into_name(client, asked, monkeypatch):
    import time
    now = int(time.time())
    sms = {"kind": "sms", "id": 7, "from": "010-1234-5678", "date": now * 1000, "text": "회의 자료 보냈어요"}
    client.post("/v1/ingest/messages", content=json.dumps(sms))
    assert flags() == {"01012345678": ("01012345678", 1)}

    monkeypatch.setattr(call_brief, "call_transcript", lambda event: "[화자1] 내일 회의 3시로 옮기자\n[화자2] 좋아")
    client.post("/v1/ingest/calls", params={"filename": "a.m4a", "number": "01012345678", "name": "홍길동",
                                            "call_date": str(now * 1000)}, content=b"audio")
    # 전사 스레드가 끝나면 부르는 것과 같다
    from contextlib import closing
    from app.db.session import connect
    with closing(connect()) as conn:
        raw_path = conn.execute("SELECT raw_path FROM events WHERE source = 'call'").fetchone()[0]
    call_briefs.mark_call(raw_path)
    assert flags() == {"홍길동": ("01012345678", 1)}

    call_brief.refresh()
    name, conversation = asked[0]
    text = "\n".join(conversation)
    assert name == "홍길동" and "회의 자료 보냈어요" in text and "통화: [화자1] 내일 회의 3시로 옮기자 / [화자2] 좋아" in text
    body = client.post("/v1/brief/call", params={"number": "+821012345678"}).json()
    assert body["title"] == "📞 홍길동" and (body["sms"], body["calls"]) == (1, 1) and body["summarized_at"]


def test_seed_marks_people_with_recent_conversation(client, asked):
    import time
    now = int(time.time())
    client.post("/v1/ingest/kakao/history", json=history(now))
    sms = {"kind": "sms", "id": 7, "from": "01099990000", "date": now * 1000, "text": "안녕하세요"}
    client.post("/v1/ingest/messages", content=json.dumps(sms))
    from contextlib import closing
    from app.db.session import connect
    with closing(connect()) as conn:
        conn.execute("DELETE FROM call_briefs")
    assert call_brief.seed() == 2
    assert set(flags()) == {"홍길동", "01099990000"}
