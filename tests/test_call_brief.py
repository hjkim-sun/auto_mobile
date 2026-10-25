from __future__ import annotations

import json

import pytest

from app.core.config import get_settings
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


def test_brief_falls_back_to_last_message(client, monkeypatch):
    def fail(name, conversation):
        raise call_brief.BriefError("timeout")

    monkeypatch.setattr(call_brief, "summarize", fail)
    import time
    sms = {"kind": "sms", "id": 9, "from": "01012345678", "date": int(time.time()) * 1000, "text": "택배 도착"}
    client.post("/v1/ingest/messages", content=json.dumps(sms))
    body = client.post("/v1/brief/call", params={"number": "01012345678"}).json()
    assert body["text"].startswith("요약 실패") and "택배 도착" in body["text"]


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
    assert call_brief.normalize_number("+82 10-1234-5678") == "01012345678"
    assert call_brief.normalize_number("010-1234-5678") == "01012345678"
    assert call_brief.normalize_number("1588-0000") == "15880000"


def test_brief_strips_number_from_name(client, asked):
    import time
    client.post("/v1/ingest/kakao/history", json=history(int(time.time())))
    body = client.post("/v1/brief/call", params={"number": "01012345678", "name": "홍길동, 010-1234-5678"}).json()
    assert body["title"] == "📞 홍길동" and body["kakao"] == 3
