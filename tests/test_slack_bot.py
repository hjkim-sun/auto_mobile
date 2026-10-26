from __future__ import annotations

import pytest

from app.db.session import init_db
from app.jobs import slack_bot
from app.repositories import buzz_state, events
from app.services import agent, slack

OWNER, BOT, PEER = "U0OWNER", "U0BOT", "U0PEER"
SMS = {"kind": "sms", "id": 1, "from": "01012345678", "date_text": "2026-10-20 09:00:00",
       "text": "홍길동입니다. 내일 3시 회의 가능하세요?", "attachments": []}


class FakeSlack:
    def __init__(self):
        self.sent = []
        self.reactions = []
        self.messages = []

    def open_dm(self, user):
        assert user == OWNER
        return "D1"

    def send(self, channel, text, thread_ts=None):
        self.sent.append((channel, text, thread_ts))

    def react(self, channel, ts, emoji):
        self.reactions.append(ts)

    def user_name(self, user):
        return {OWNER: "주인", PEER: "codex"}.get(user, user)

    def history(self, channel, before_ts, thread_ts=None, limit=10):
        return [m for m in self.messages if float(m["ts"]) < float(before_ts)][-limit:]


@pytest.fixture
def fake_slack(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_OWNER_USER_ID", OWNER)
    monkeypatch.setenv("SLACK_NOTIFY_CHANNEL", "")
    monkeypatch.setenv("SLACK_LISTEN_CHANNELS", '["C1"]')
    monkeypatch.setenv("AGENT_BRIEFING", "false")
    from app.core.config import get_settings
    get_settings.cache_clear()
    init_db()
    slack_bot._seen.clear()
    fake = FakeSlack()
    for name in ("open_dm", "send", "react", "user_name", "history"):
        monkeypatch.setattr(slack, name, getattr(fake, name))
    monkeypatch.setattr(slack, "bot_user_id", lambda: BOT)
    return fake


@pytest.fixture
def replies(monkeypatch):
    calls = []

    def fake_reply(channel, text, sender, is_owner, history=None):
        calls.append((channel, text, sender, is_owner, history))
        return "답"
    monkeypatch.setattr(agent, "reply", fake_reply)
    return calls


def msg(text, user=OWNER, channel="D1", ts="100.1", kind="message", **extra):
    event = {"type": kind, "user": user, "text": text, "channel": channel, "ts": ts, **extra}
    if kind == "message":
        event.setdefault("channel_type", "im" if channel.startswith("D") else "channel")
    return event


def kept(key):
    event_id, _ = events.insert_event("message", key, SMS)
    events.finish(event_id, "kept", {"useful": 0.9})
    return event_id


def test_only_events_after_first_start_are_notified_once(fake_slack):
    kept("message:sms-1")
    assert slack_bot.notify() == 0
    new = kept("message:sms-2")
    assert slack_bot.notify() == 1 and slack_bot.notify() == 0
    channel, text, _ = fake_slack.sent[0]
    assert channel == "D1" and f"#{new}" in text


def test_dm_command_and_agent(fake_slack, replies):
    assert slack_bot.handle_event(msg("상태"))
    assert fake_slack.sent[-1][0] == "D1" and replies == []
    assert slack_bot.handle_event(msg("내일 뭐 있어?", ts="101.1"))
    assert replies[0][:4] == ("D1", "내일 뭐 있어?", "주인", True) and replies[0][4] == []
    assert fake_slack.sent[-1] == ("D1", "답", None) and fake_slack.reactions == ["101.1"]
    # 같은 이벤트가 다시 오거나 주인이 아닌 DM, 봇 자신의 글, 수정 이벤트에는 답하지 않는다
    assert not slack_bot.handle_event(msg("내일 뭐 있어?", ts="101.1"))
    assert not slack_bot.handle_event(msg("hi", user=PEER, ts="102.1"))
    assert not slack_bot.handle_event(msg("hi", user=BOT, ts="103.1"))
    assert not slack_bot.handle_event(msg("hi", ts="104.1", subtype="message_changed"))


def test_new_conversation_resets_session(fake_slack, replies):
    buzz_state.put("agent_session:D1", {"id": "s", "at": 0})
    slack_bot.handle_event(msg("새 대화"))
    assert buzz_state.get("agent_session:D1") is None and fake_slack.sent[-1][1] == "새 대화를 시작합니다."


def test_listen_channel_answers_owner_without_mention(fake_slack, replies):
    fake_slack.messages = [{"ts": "99.0", "user": PEER, "text": "회의는 3시"}]
    assert slack_bot.handle_event(msg("정리해 줘", channel="C1"))
    assert replies[0][4] == ["- codex: 회의는 3시"] and fake_slack.sent[-1] == ("C1", "답", None)
    # 스레드 안에서 한 말은 스레드로
    slack_bot.handle_event(msg("더", channel="C1", ts="100.5", thread_ts="100.1"))
    assert fake_slack.sent[-1] == ("C1", "답", "100.1")
    # 다른 사람을 콕 집어 부른 말에는 끼어들지 않는다. 같은 말의 app_mention 도 무시한다
    assert not slack_bot.handle_event(msg(f"<@{PEER}> 어때?", channel="C1", ts="101.1"))
    assert not slack_bot.handle_event(msg(f"<@{BOT}> 어때?", channel="C1", ts="102.1", kind="app_mention"))
    # 주인이 아닌 사람의 말은 멘션 없이는 듣지 않는다
    assert not slack_bot.handle_event(msg("안녕", user=PEER, channel="C1", ts="103.1"))


def test_mention_elsewhere_replies_in_thread_and_limits_peers(fake_slack, replies, monkeypatch):
    monkeypatch.setenv("AGENT_PEER_REPLIES", "1")
    from app.core.config import get_settings
    get_settings.cache_clear()
    assert slack_bot.handle_event(msg(f"<@{BOT}> 근거는?", user=PEER, channel="C9", ts="200.1", kind="app_mention"))
    assert replies[0][1:4] == ("근거는?", "codex", False) and fake_slack.sent[-1] == ("C9", "답", "200.1")
    slack_bot.handle_event(msg(f"<@{BOT}> 또", user=PEER, channel="C9", ts="201.1", kind="app_mention"))
    assert len(replies) == 1
    # 주인이 말하면 다시 센다
    slack_bot.handle_event(msg(f"<@{BOT}> 계속", channel="C9", ts="202.1", kind="app_mention"))
    slack_bot.handle_event(msg(f"<@{BOT}> 또", user=PEER, channel="C9", ts="203.1", kind="app_mention"))
    assert len(replies) == 3
    # 멘션 없는 채널 글은 듣지 않는다
    assert not slack_bot.handle_event(msg("그냥 글", channel="C9", ts="204.1"))


def test_plain_text(fake_slack):
    assert slack.plain(f"<@{BOT}> <@{PEER}> 봐 &lt;3 <https://example.com|예시> <#C1|general>") == \
        "@codex 봐 <3 예시 (https://example.com) #general"
