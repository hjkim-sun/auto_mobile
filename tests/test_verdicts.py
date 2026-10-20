from __future__ import annotations

from app.jobs.worker import run
from tests.test_ingest import EMAIL, SMS
from tests.test_worker import AD, IMAGE_ONLY, fake_jev  # noqa: F401


def test_message_verdicts_newest_first_with_filter_and_paging(client, fake_jev):
    for msg in (AD, SMS, IMAGE_ONLY):
        client.post("/v1/ingest/messages", json=msg)
    run(once=True)

    items = client.get("/v1/verdicts/messages").json()["items"]
    assert [i["event_id"] for i in items] == [3, 2, 1]
    image, sms, ad = items
    assert (sms["status"], sms["useful"], sms["choice"], sms["sender"], sms["text"]) == \
        ("kept", 0.9, "useful", "01012345678", "안녕하세요 홍길동입니다")
    assert (ad["status"], ad["choice"]) == ("discarded", "noise")
    assert (image["status"], image["useful"], image["reason"]) == ("kept", None, "no_text")

    discarded = client.get("/v1/verdicts/messages?status=discarded").json()["items"]
    assert [i["event_id"] for i in discarded] == [1]

    page = client.get("/v1/verdicts/messages?limit=2").json()
    assert page["next_before_id"] == 2
    rest = client.get("/v1/verdicts/messages?limit=2&before_id=2").json()
    assert [i["event_id"] for i in rest["items"]] == [1] and rest["next_before_id"] is None


def test_email_verdicts(client, fake_jev):
    client.post("/v1/ingest/emails?account=gmail", content=EMAIL)
    client.post("/v1/ingest/messages", json=SMS)
    run(once=True)

    items = client.get("/v1/verdicts/emails").json()["items"]
    assert len(items) == 1
    assert (items[0]["account"], items[0]["subject"], items[0]["status"]) == ("gmail", "meeting", "kept")
    assert items[0]["text"] == "다음주 화요일 오후 3시 회의 가능하신가요?"


def test_verdicts_require_token(client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("INGEST_TOKEN", "secret")
    get_settings.cache_clear()
    assert client.get("/v1/verdicts/messages").status_code == 401
    assert client.get("/v1/verdicts/messages", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_docs_only_without_proxy(client):
    for path in ("/docs", "/openapi.json", "/redoc"):
        assert client.get(path).status_code == 200
        assert client.get(path, headers={"X-Forwarded-For": "203.0.113.5"}).status_code == 404
