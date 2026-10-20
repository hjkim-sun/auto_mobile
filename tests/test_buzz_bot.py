from __future__ import annotations

import pytest

from app.db.session import init_db
from app.jobs import buzz_bot
from app.repositories import events
from app.services import bot, buzz

OWNER = "2b2d578fc2b303bcee9483229cf0ed0c0c02663541289abc580a8f2ce32924d0"
OWNER_NPUB = "npub19vk40r7zkvpmem55sv3feu8dpsxqye34gy5f40zcp28jecefyngqufnm7t"
BOT_KEY = "0000000000000000000000000000000000000000000000000000000000000003"
BOT = "f9308a019258c31049344f85f89d5229b531c845836f99b08601f113bce036f9"   # BOT_KEY 의 pubkey
PEER = "e" * 64
SMS = {"kind": "sms", "id": 1, "from": "01012345678", "date_text": "2026-10-20 09:00:00",
       "text": "홍길동입니다. 내일 3시 회의 가능하세요?", "attachments": []}


class FakeBuzz:
    def __init__(self):
        self.sent = []
        self.inbox = []
        self.feed = []
        self.threads = []
        self.reactions = []

    def open_dm(self, pubkey):
        assert pubkey == OWNER
        return "dm-1"

    def send(self, channel, content, reply_to=None):
        self.sent.append(content)
        self.threads.append((channel, reply_to))

    def react(self, event_id, emoji):
        self.reactions.append(event_id)

    def user_names(self, pubkeys):
        return {OWNER: "주인", PEER: "codex"}

    def mentions(self, since, limit=50):
        return [m for m in self.feed if m["created_at"] >= since]

    def get_messages(self, channel, since=None, limit=50):
        return [m for m in self.inbox if since is None or m["created_at"] >= since]


@pytest.fixture
def fake_buzz(monkeypatch):
    monkeypatch.setenv("BUZZ_RELAY_URL", "https://example.communities.buzz.xyz")
    monkeypatch.setenv("BUZZ_OWNER_PUBKEY", OWNER_NPUB)
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", BOT_KEY)
    monkeypatch.setenv("BUZZ_BOT_NAME", "auto_mobile")   # .env 의 실제 봇 이름과 상관없이
    from app.core.config import get_settings
    get_settings.cache_clear()
    init_db()
    fake = FakeBuzz()
    for name in ("open_dm", "send", "get_messages", "react", "user_names", "mentions"):
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


@pytest.fixture
def fake_agent(monkeypatch):
    calls = []

    def reply(channel, text, sender, is_owner, history=None):
        calls.append({"channel": channel, "text": text, "sender": sender, "owner": is_owner,
                      "history": history})
        return f"에이전트 답: {text}"
    monkeypatch.setattr(buzz_bot.agent, "reply", reply)
    return calls


def test_free_text_goes_to_agent_and_commands_do_not(fake_buzz, fake_agent):
    buzz_bot.run(once=True)
    fake_buzz.sent.clear()
    since = buzz_bot.buzz_state.get("dm_since")
    fake_buzz.inbox = [
        {"id": "a", "pubkey": OWNER, "content": "상태", "created_at": since},
        {"id": "b", "pubkey": OWNER, "content": "안함2 안함 4", "created_at": since + 1},
    ]
    buzz_bot.run(once=True)
    assert fake_buzz.sent[0] == "이벤트 없음"
    assert fake_buzz.sent[1] == "에이전트 답: 안함2 안함 4"
    assert fake_agent == [{"channel": "dm-1", "text": "안함2 안함 4", "sender": "주인", "owner": True,
                           "history": []}]
    assert fake_buzz.reactions == ["b"] and fake_buzz.threads[1] == ("dm-1", None)


def test_channel_mentions_are_answered_in_thread_with_peer_limit(fake_buzz, fake_agent, monkeypatch):
    monkeypatch.setenv("AGENT_PEER_REPLIES", "2")
    from app.core.config import get_settings
    get_settings.cache_clear()
    buzz_bot.run(once=True)
    since = buzz_bot.buzz_state.get("mention_since")
    tag = [["h", "room-1"], ["p", BOT]]
    fake_buzz.inbox = [{"id": "h1", "pubkey": OWNER, "kind": 9, "content": "이번 주 약속 같이 보자",
                        "created_at": since - 5}]
    fake_buzz.feed = [
        {"id": "m0", "pubkey": OWNER, "content": "@auto_mobile 새 메일 있어?", "created_at": since,
         "tags": [["h", "dm-1"], ["p", BOT]]},                                            # DM 은 answer 가 맡는다
        {"id": "m1", "pubkey": OWNER, "content": "@auto_mobile 이번 주 약속 알려줘", "created_at": since, "tags": tag},
        {"id": "m2", "pubkey": PEER, "content": "@auto_mobile 근거는?", "created_at": since + 1, "tags": tag},
        {"id": "m3", "pubkey": PEER, "content": "@auto_mobile 또?", "created_at": since + 2, "tags": tag},
        {"id": "m4", "pubkey": PEER, "content": "@auto_mobile 또또?", "created_at": since + 3, "tags": tag},
        {"id": "m5", "pubkey": BOT, "content": "내 글", "created_at": since + 4, "tags": tag},
    ]
    buzz_bot.run(once=True)
    buzz_bot.run(once=True)
    assert [c["text"] for c in fake_agent] == ["이번 주 약속 알려줘", "근거는?", "또?"]   # m4 는 횟수 초과
    assert fake_agent[0]["history"] == ["- 주인: 이번 주 약속 같이 보자"]
    assert [c["owner"] for c in fake_agent] == [True, False, False]
    assert fake_buzz.threads == [("room-1", "m1"), ("room-1", "m2"), ("room-1", "m3")]

    fake_buzz.feed.append({"id": "m6", "pubkey": OWNER, "content": "@auto_mobile 상태", "created_at": since + 5,
                           "tags": tag})
    fake_buzz.feed.append({"id": "m7", "pubkey": PEER, "content": "@auto_mobile 다시", "created_at": since + 6,
                           "tags": tag})
    buzz_bot.run(once=True)
    assert fake_agent[-1]["text"] == "다시"      # 주인이 말하면 횟수가 다시 시작된다
    assert fake_buzz.threads[-2] == ("room-1", "m6")


def test_unknown_command_shows_help(fake_buzz):
    assert bot.command("뭐해") is None
    assert bot.handle("뭐해") == bot.HELP
    assert bot.handle("#999") == "#999 없음"
    assert bot.handle("오늘") == "오늘 남긴 것: 없음"


def test_bip340_vector_and_key_encoding():
    from app.services import nostr

    # BIP-340 공식 test vector 0
    sk = "0000000000000000000000000000000000000000000000000000000000000003"
    assert nostr.public_key(sk).upper() == "F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9"
    sig = nostr.schnorr_sign(bytes(32), sk, aux=bytes(32)).hex().upper()
    assert sig == ("E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA8215"
                   "25F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0")
    assert nostr.bech32_encode("npub", bytes.fromhex(OWNER)) == OWNER_NPUB
    nsec = nostr.bech32_encode("nsec", bytes.fromhex(sk))
    assert nostr.public_key(nsec) == nostr.public_key(sk)
