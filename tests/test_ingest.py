from __future__ import annotations

import json

from app.core.config import get_settings

SMS = {"kind": "sms", "id": 18664, "from": "01012345678", "date": 1792247764000, "text": "안녕하세요 홍길동입니다"}
MMS = {
    "kind": "mms", "id": 10453, "from": "01012345678", "date": 1792247679,
    "parts": [
        {"_id": 9590, "ct": "application/smil", "text": "<smil/>"},
        {"_id": 9591, "ct": "image/jpeg", "cl": "photo.jpeg"},
        {"_id": 9592, "ct": "text/plain", "text": "사진 봐"},
    ],
}
EMAIL = (
    "From: 홍길동 <hong@example.com>\r\nTo: me@gmail.com\r\nSubject: meeting\r\n"
    "Message-ID: <p-456@example.com>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
    "다음주 화요일 오후 3시 회의 가능하신가요?\r\n"
).encode()


def raw_files(sub):
    return sorted(p.name for p in (get_settings().raw_dir / sub).rglob("*") if p.is_file())


def test_message_is_stored_and_deduplicated(client):
    first = client.post("/v1/ingest/messages", json=SMS)
    assert first.status_code == 201
    assert first.json() == {"event_id": 1, "duplicate": False}

    stored = json.loads((get_settings().raw_dir / "messages/20261017/sms-18664.json").read_text())
    assert stored["date_text"] == "2026-10-17 23:36:04"   # 한국 시간

    again = client.post("/v1/ingest/messages", json=SMS)
    assert again.status_code == 200
    assert again.json() == {"event_id": 1, "duplicate": True}


def test_message_accepts_double_encoded_json(client):
    res = client.post("/v1/ingest/messages", content=json.dumps(json.dumps(SMS)))
    assert res.status_code == 201


def test_mms_text_and_attachments_are_normalized(client):
    client.post("/v1/ingest/messages", json=MMS)
    stored = json.loads((get_settings().raw_dir / "messages/20261017/mms-10453.json").read_text())
    assert stored["text"] == "사진 봐"
    assert stored["attachments"] == [{"part": 9591, "ct": "image/jpeg", "name": "photo.jpeg"}]


def test_message_requires_kind_and_id(client):
    assert client.post("/v1/ingest/messages", json={"kind": "fax", "id": 1}).status_code == 422
    assert client.post("/v1/ingest/messages", content=b"not json").status_code == 400


def test_attachment_requires_existing_message(client):
    res = client.put("/v1/ingest/messages/mms-999/attachments/1", content=b"x")
    assert res.status_code == 404

    client.post("/v1/ingest/messages", json=MMS)
    url = "/v1/ingest/messages/mms-10453/attachments/9591?filename=photo.jpeg"
    assert client.put(url, content=b"jpeg").json() == {"event_id": 1, "duplicate": False}
    assert client.put(url, content=b"jpeg").json() == {"event_id": 1, "duplicate": True}
    assert (get_settings().raw_dir / "messages/20261017/mms-10453/photo.jpeg").read_bytes() == b"jpeg"


def test_call_is_deduplicated_by_content(client):
    path = "%2Fstorage%2Femulated%2F0%2FRecordings%2FCall%2Fcall_261019_121733.m4a"
    first = client.post(f"/v1/ingest/calls?filename={path}", content=b"audio")
    assert first.status_code == 201

    renamed = client.post("/v1/ingest/calls?filename=other.m4a", content=b"audio")
    assert renamed.json() == {"event_id": first.json()["event_id"], "duplicate": True, "transcription_id": None}
    assert len(raw_files("calls")) == 1
    (stored,) = (get_settings().raw_dir / "calls").rglob("*.m4a")
    assert stored.parent.name == "20261019" and stored.name.endswith("_call_261019_121733.m4a")


CALL_INFO = "direction=outgoing&number=01012345678&name=%ED%99%8D%EA%B8%B8%EB%8F%99&call_date=1792383789902"


def call_payload(event_id):
    from app.repositories import events
    return json.loads(events.get(event_id)["payload"])


def test_call_info_is_stored(client):
    res = client.post(f"/v1/ingest/calls?filename=call_261019_132315.m4a&{CALL_INFO}", content=b"audio")
    assert res.status_code == 201
    payload = call_payload(res.json()["event_id"])
    assert payload["direction"] == "outgoing"
    assert payload["number"] == "01012345678"
    assert payload["name"] == "홍길동"
    assert payload["call_date"] == 1792383789902
    assert payload["call_date_text"] == "2026-10-19 13:23:09"


def test_call_without_info_or_with_blank_info_works_as_before(client):
    plain = client.post("/v1/ingest/calls?filename=a.m4a", content=b"a").json()["event_id"]
    blank = client.post("/v1/ingest/calls?filename=b.m4a&direction=&number=&name=&call_date=",
                        content=b"b").json()["event_id"]
    for event_id in (plain, blank):
        assert not {"direction", "number", "name", "call_date"} & call_payload(event_id).keys()


def test_bad_call_info_is_dropped_but_recording_is_kept(client):
    res = client.post("/v1/ingest/calls?direction=sideways&number=abc&call_date=yesterday&name=%20",
                      content=b"a")
    assert res.status_code == 201
    assert not {"direction", "number", "name", "call_date"} & call_payload(res.json()["event_id"]).keys()


def test_call_date_in_exponent_form_is_read(client):
    res = client.post("/v1/ingest/calls?call_date=1.792383789902E12", content=b"a")
    assert call_payload(res.json()["event_id"])["call_date"] == 1792383789902


def test_call_resent_with_info_fills_missing_values(client):
    event_id = client.post("/v1/ingest/calls?filename=a.m4a", content=b"audio").json()["event_id"]
    again = client.post(f"/v1/ingest/calls?filename=a.m4a&{CALL_INFO}", content=b"audio")
    assert again.json()["duplicate"] is True
    assert call_payload(event_id)["direction"] == "outgoing"

    # 이미 있는 값은 덮어쓰지 않는다
    client.post("/v1/ingest/calls?filename=a.m4a&direction=incoming", content=b"audio")
    assert call_payload(event_id)["direction"] == "outgoing"


def test_empty_body_is_rejected(client):
    assert client.post("/v1/ingest/calls", content=b"").status_code == 400


def test_email_is_deduplicated_by_message_id_across_accounts(client):
    assert client.post("/v1/ingest/emails?account=gmail", content=EMAIL).status_code == 201
    assert client.post("/v1/ingest/emails?account=naver", content=EMAIL).status_code == 200
    assert len([n for n in raw_files("emails") if n.endswith(".eml")]) == 1
    assert client.post("/v1/ingest/emails", content=EMAIL).status_code == 422


def test_token_is_required_when_configured(client, monkeypatch):
    monkeypatch.setenv("INGEST_TOKEN", "secret")
    get_settings.cache_clear()
    assert client.post("/v1/ingest/messages", json=SMS).status_code == 401
    ok = client.post("/v1/ingest/messages", json=SMS, headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 201


def test_health_counts_events(client):
    client.post("/v1/ingest/messages", json=SMS)
    assert client.get("/v1/health").json() == {"status": "ok", "events": {"pending": 1}}



def kakao(sender="홍길동", text="내일 3시에 봬요", chat_log_id=3900000000000000000, channel="new_message_v6"):
    """Automate Notification posted? 출력을 그대로 jsonEncode 한 모양 (값은 가짜). title 은 보낸 사람이다."""
    return {"title": sender, "message": text, "ticker": f"{sender} : {text}", "when": 1792592690.867,
            "channel": channel,
            "extras": {"android.title": sender, "android.text": text, "android.isGroupConversation": True,
                       "android.hiddenConversationTitle": None, "android.selfDisplayName": "나",
                       "threadId": 0, "chatLogId": chat_log_id}}


def test_kakao_message_is_stored_and_deduplicated(client):
    res = client.post("/v1/ingest/kakao", json=kakao())
    assert res.status_code == 201
    assert res.json() == {"event_id": 1, "duplicate": False, "ignored": False}

    stored = json.loads((get_settings().raw_dir / "kakao/20261021/3900000000000000000.json").read_text())
    assert stored["sender"] == "홍길동" and stored["room"] is None
    assert stored["date_text"] == "2026-10-21 23:24:50"
    assert stored["context"] == []

    again = client.post("/v1/ingest/kakao", json=kakao())
    assert again.status_code == 200
    assert again.json() == {"event_id": 1, "duplicate": True, "ignored": False}


def test_kakao_context_is_previous_messages_from_same_sender(client):
    client.post("/v1/ingest/kakao", json=kakao("홍길동", "몇 시에 와?", 100))
    client.post("/v1/ingest/kakao", json=kakao("김철수", "안녕", 200))
    client.post("/v1/ingest/kakao", json=kakao("홍길동", "7시", 300))
    stored = json.loads((get_settings().raw_dir / "kakao/20261021/300.json").read_text())
    assert stored["context"] == [{"sender": "홍길동", "text": "몇 시에 와?"}]


def test_kakao_without_chat_log_id_is_deduplicated_by_content(client):
    note = kakao(chat_log_id=None)
    assert client.post("/v1/ingest/kakao", json=note).status_code == 201
    assert client.post("/v1/ingest/kakao", json=note).status_code == 200


def test_kakao_summary_and_muted_room_notifications_are_ignored(client):
    summary = {"title": None, "message": None, "when": 1792592690.32, "channel": "new_message_v6",
               "extras": {"android.title": None, "android.text": None}}
    res = client.post("/v1/ingest/kakao", json=summary)
    assert res.status_code == 200 and res.json()["ignored"] is True

    # 알림을 끈 방(오픈채팅 등)은 quiet_new_message 채널로 온다
    res = client.post("/v1/ingest/kakao", json=kakao(channel="quiet_new_message"))
    assert res.status_code == 200
    assert res.json() == {"event_id": None, "duplicate": False, "ignored": True}
    assert raw_files("kakao") == []
