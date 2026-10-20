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
