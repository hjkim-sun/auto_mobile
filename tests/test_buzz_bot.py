from __future__ import annotations

import pytest

from app.db.session import init_db
from app.jobs import buzz_bot
from app.repositories import events
from app.services import bot, buzz

OWNER = "2b2d578fc2b303bcee9483229cf0ed0c0c02663541289abc580a8f2ce32924d0"
OWNER_NPUB = "npub19vk40r7zkvpmem55sv3feu8dpsxqye34gy5f40zcp28jecefyngqufnm7t"
BOT = "f" * 64
SMS = {"kind": "sms", "id": 1, "from": "01012345678", "date_text": "2026-10-20 09:00:00",
       "text": "홍길동입니다. 내일 3시 회의 가능하세요?", "attachments": []}


class FakeBuzz:
    def __init__(self):
        self.sent = []
        self.inbox = []

    def open_dm(self, pubkey):
        assert pubkey == OWNER
        return "dm-1"

    def send(self, channel, content):
        self.sent.append(content)

    def get_messages(self, channel, since=None, limit=50):
        return [m for m in self.inbox if since is None or m["created_at"] >= since]


@pytest.fixture
def fake_buzz(monkeypatch):
    monkeypatch.setenv("BUZZ_RELAY_URL", "https://example.communities.buzz.xyz")
    monkeypatch.setenv("BUZZ_OWNER_PUBKEY", OWNER_NPUB)
    from app.core.config import get_settings
    get_settings.cache_clear()
    init_db()
    fake = FakeBuzz()
    for name in ("open_dm", "send", "get_messages"):
        monkeypatch.setattr(buzz, name, getattr(fake, name))
    return fake


def kept(payload, key):
    event_id, _ = events.insert_event("message", key, payload)
    events.finish(event_id, "kept", {"useful": 0.9})
    return event_id


def test_npub_is_decoded():
    assert buzz.to_hex_pubkey(OWNER_NPUB) == OWNER
    assert buzz.to_hex_pubkey(OWNER.upper()) == OWNER
    with pytest.raises(ValueError):
        buzz.to_hex_pubkey("nsec1abc")


def test_only_events_after_first_start_are_notified_once(fake_buzz):
    kept(SMS, "message:sms-old")
    buzz_bot.run(once=True)
    assert fake_buzz.sent == []          # 봇을 띄우기 전 이벤트는 알리지 않는다

    new_id = kept(SMS, "message:sms-new")
    discarded, _ = events.insert_event("message", "message:sms-ad", {**SMS, "text": "(광고)"})
    events.finish(discarded, "discarded", {"useful": 0.0})
    buzz_bot.run(once=True)
    buzz_bot.run(once=True)
    assert len(fake_buzz.sent) == 1
    assert fake_buzz.sent[0].startswith(f"#{new_id} [문자]") and "내일 3시 회의" in fake_buzz.sent[0]


def test_owner_commands_are_answered_once(fake_buzz):
    event_id = kept(SMS, "message:sms-1")
    buzz_bot.run(once=True)               # DM 을 열고 dm_since 를 잡는다
    fake_buzz.sent.clear()
    since = buzz_bot.buzz_state.get("dm_since")
    fake_buzz.inbox = [
        {"id": "a", "pubkey": OWNER, "content": f"#{event_id}", "created_at": since},
        {"id": "b", "pubkey": BOT, "content": "봇이 보낸 글", "created_at": since},
        {"id": "c", "pubkey": OWNER, "content": "문자 3", "created_at": since + 1},
        {"id": "z", "pubkey": OWNER, "content": "도움말", "created_at": since - 10},   # 봇 이전 대화
    ]
    buzz_bot.run(once=True)
    buzz_bot.run(once=True)
    assert len(fake_buzz.sent) == 2
    assert "홍길동입니다" in fake_buzz.sent[0] and "kept" in fake_buzz.sent[0]
    assert fake_buzz.sent[1].startswith("최근 문자 (1건)")


def test_unknown_command_shows_help(fake_buzz):
    assert bot.handle("뭐해") == bot.HELP
    assert bot.handle("#999") == "#999 없음"
    assert bot.handle("오늘") == "오늘 남긴 것: 없음"
