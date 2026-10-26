"""Slack Web API 와 주고받기 (slack_sdk). 이벤트 수신(Socket Mode)은 app/jobs/slack_bot.py 가 한다."""
from __future__ import annotations

import functools
import html
import re

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from app.core.config import get_settings

# markdown 블록 한 개에 들어가는 글자 수. 넘으면 블록 없이 mrkdwn 글로만 보낸다
MARKDOWN_CHARS = 12000
TEXT_CHARS = 39000


@functools.cache
def client() -> WebClient:
    return WebClient(token=get_settings().slack_bot_token.get_secret_value())


@functools.cache
def bot_user_id() -> str:
    return client().auth_test()["user_id"]


def whoami() -> dict:
    return client().auth_test().data


def open_dm(user: str) -> str:
    return client().conversations_open(users=user)["channel"]["id"]


def send(channel: str, text: str, thread_ts: str | None = None) -> dict:
    """에이전트 답은 Markdown 이라 markdown 블록으로 보낸다. text 는 알림 미리보기에 쓰인다."""
    kwargs = {"channel": channel, "thread_ts": thread_ts, "text": text[:TEXT_CHARS]}
    if len(text) <= MARKDOWN_CHARS:
        try:
            return client().chat_postMessage(**kwargs, blocks=[{"type": "markdown", "text": text}]).data
        except SlackApiError as e:
            if e.response.get("error") not in ("invalid_blocks", "invalid_blocks_format"):
                raise
    return client().chat_postMessage(**kwargs).data


def react(channel: str, ts: str, emoji: str) -> None:
    client().reactions_add(channel=channel, timestamp=ts, name=emoji)


@functools.lru_cache(maxsize=256)
def user_name(user: str) -> str:
    try:
        info = client().users_info(user=user)["user"]
    except SlackApiError:
        return user
    profile = info.get("profile") or {}
    return profile.get("display_name") or profile.get("real_name") or info.get("name") or user


def history(channel: str, before_ts: str, thread_ts: str | None = None, limit: int = 10) -> list[dict]:
    """before_ts 앞의 메시지, 오래된 것부터. 스레드 안이면 그 스레드의 메시지."""
    if thread_ts:
        messages = client().conversations_replies(channel=channel, ts=thread_ts, limit=200)["messages"]
    else:
        messages = client().conversations_history(channel=channel, latest=before_ts, limit=limit)["messages"]
    messages = [m for m in messages if float(m["ts"]) < float(before_ts)]
    return sorted(messages, key=lambda m: float(m["ts"]))[-limit:]


def mentioned(text: str) -> set[str]:
    return set(re.findall(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>", text))


def plain(text: str) -> str:
    """Slack 표기를 사람이 친 글로: 봇 멘션은 지우고, 다른 멘션·링크는 글자로, &lt; 같은 것은 되돌린다."""
    text = re.sub(rf"<@{re.escape(bot_user_id())}(?:\|[^>]*)?>", "", text)
    text = re.sub(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>", lambda m: "@" + user_name(m.group(1)), text)
    text = re.sub(r"<#[CG][A-Z0-9]+\|([^>]*)>", r"#\1", text)
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    return html.unescape(text).strip()
