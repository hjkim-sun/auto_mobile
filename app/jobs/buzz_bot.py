"""Buzz 봇. kept 로 판별된 이벤트를 사용자에게 알리고, 말을 걸면 답한다.

- 알림은 BUZZ_NOTIFY_CHANNEL(없으면 DM)로 보낸다. AGENT_BRIEFING 이면 에이전트가 위키 맥락을 붙여 브리핑하고,
  실패하면 원문 알림을 보낸다.

- 주인의 DM 과, 다른 채널에서 봇을 멘션한 말(스레드로 답)에 답한다.
- 듣는 채널(BUZZ_LISTEN_CHANNELS)에서는 주인의 말에 멘션 없이도 답한다.
- 명령(app/services/bot.py)은 바로 답하고, 그 밖의 말은 에이전트(app/services/agent.py, Claude Code)가 답한다.
- 다른 에이전트가 멘션하면 주인이 다시 말할 때까지 AGENT_PEER_REPLIES 번까지만 답한다.
반드시 1개 프로세스만 띄운다.
처음 띄울 때 이미 있던 이벤트는 알리지 않는다(buzz_state.notify_after_id).
실행: python -m app.jobs.buzz_bot [--once | --whoami | --invite | --pair-code | --cmd 명령]
"""
from __future__ import annotations

import argparse
import json
import logging
import queue
import re
import threading
import time

from app.core.config import get_settings
from app.db.session import init_db
from app.repositories import agent_turns, buzz_state, calendar_items
from app.repositories.events import now
from app.services import agent, bot, buzz, nostr

HISTORY_MESSAGES = 10

logger = logging.getLogger(__name__)


def dm_channel() -> str:
    owner = buzz.to_hex_pubkey(get_settings().buzz_owner_pubkey)
    channel = buzz_state.get("dm_channel")
    if buzz_state.get("dm_owner") != owner or not channel:
        channel = buzz.open_dm(owner)
        buzz_state.put("dm_channel", channel)
        buzz_state.put("dm_owner", owner)
        buzz_state.put("dm_since", int(time.time()))   # 봇을 띄우기 전 대화에는 답하지 않는다
        logger.info("opened DM channel %s with owner", channel)
    return channel


def notify(dm: str) -> int:
    """kept 알림과 캘린더 알림. BUZZ_NOTIFY_CHANNEL 이 있으면 그 채널로, 없으면 DM 으로 보낸다."""
    channel = get_settings().buzz_notify_channel or dm
    after = buzz_state.get("notify_after_id")
    if after is None:
        after = buzz_state.max_event_id()
        buzz_state.put("notify_after_id", after)
    rows = buzz_state.unnotified_kept(after)
    settings = get_settings()
    if rows and settings.agent_enabled and settings.agent_briefing:
        # 브리핑은 작업 스레드에서 수십 초 걸린다. 다음 주기에 또 넘기지 않게 먼저 알린 것으로 적는다.
        for event in rows:
            buzz_state.mark_notified(event["id"])
        dispatch(lambda: send_briefing(channel, rows))
    else:
        for event in rows:
            buzz.send(channel, bot.notification(event))
            buzz_state.mark_notified(event["id"])
    sent = len(rows)
    # 위키 빌더가 캘린더에 넣은 일정은 알리고, 애매한 일정은 넣을지 묻는다.
    for item in calendar_items.unnotified():
        buzz.send(channel, bot.calendar_message(item))
        calendar_items.mark_notified(item["id"])
        sent += 1
    return sent


def send_briefing(channel: str, rows: list) -> None:
    try:
        text = agent.brief(rows)
    except Exception:
        logger.exception("briefing failed, sending plain notifications")
        for event in rows:
            buzz.send(channel, bot.notification(event))
        return
    refs = " ".join(f"#{r['id']}" for r in rows)
    buzz.send(channel, f"{text}\n\n원문: {refs}")


def _unseen(key: str, fetch) -> list[dict]:
    """fetch(since) 로 받은 메시지 중 아직 처리하지 않은 것. 처리했다고 바로 기록한다.

    --since 는 초 단위라 같은 초의 메시지가 다시 올 수 있다. 마지막 초의 id 를 기억해 거른다.
    """
    since = buzz_state.get(f"{key}_since", int(time.time()))
    seen = set(buzz_state.get(f"{key}_seen", []))
    fresh = []
    for msg in sorted(fetch(since), key=lambda m: m.get("created_at", 0)):
        if msg.get("id") in seen or msg.get("created_at", 0) < since:
            continue
        fresh.append(msg)
        if msg.get("created_at", 0) > since:
            since, seen = msg["created_at"], set()
        seen.add(msg.get("id"))
    buzz_state.put(f"{key}_since", since)
    buzz_state.put(f"{key}_seen", sorted(seen))
    return fresh


def answer(channel: str) -> int:
    """주인과의 DM. 주인이 보낸 말에만 답한다."""
    owner = buzz_state.get("dm_owner")
    replied = 0
    for msg in _unseen("dm", lambda since: buzz.get_messages(channel, since=since)):
        if msg.get("pubkey") == owner and isinstance(msg.get("content"), str):
            respond(channel, msg, reply_to=None, with_history=False)
            replied += 1
    return replied


def answer_mentions(dm: str) -> int:
    """다른 채널에서 봇을 멘션한 말. 스레드로 답한다.

    DM 은 answer 가, 듣는 채널(BUZZ_LISTEN_CHANNELS)에서 주인이 한 말은 answer_listening 이 맡는다.
    """
    me, owner = bot_pubkey(), buzz_state.get("dm_owner")
    listening = set(get_settings().buzz_listen_channels)
    replied = 0
    for msg in _unseen("mention", lambda since: buzz.mentions(since)):
        channel = buzz.channel_of(msg)
        if channel in (None, dm) or msg.get("pubkey") == me or not isinstance(msg.get("content"), str):
            continue
        if channel in listening and msg.get("pubkey") == owner:
            continue
        respond(channel, msg, reply_to=msg.get("id"), with_history=True)
        replied += 1
    return replied


def answer_listening() -> int:
    """듣는 채널에서는 주인이 한 말에 멘션 없이도 답한다. 다른 사람을 콕 집어 부른 말에는 끼어들지 않는다.

    스레드 안에서 한 말은 그 스레드로, 채널에서 한 말은 채널로 답한다.
    """
    me, owner = bot_pubkey(), buzz_state.get("dm_owner")
    replied = 0
    for channel in get_settings().buzz_listen_channels:
        for msg in _unseen(f"listen:{channel}", lambda since: buzz.get_messages(channel, since=since)):
            if msg.get("pubkey") != owner or msg.get("kind") != 9 or not isinstance(msg.get("content"), str):
                continue
            mentioned = {t[1] for t in msg.get("tags", []) if len(t) > 1 and t[0] == "p"}
            if mentioned and me not in mentioned:
                continue
            in_thread = any(t and t[0] == "e" for t in msg.get("tags", []))
            respond(channel, msg, reply_to=msg.get("id") if in_thread else None, with_history=True)
            replied += 1
    return replied


def bot_pubkey() -> str:
    return nostr.public_key(get_settings().buzz_private_key.get_secret_value())


def strip_mention(text: str) -> str:
    name = re.escape(get_settings().buzz_bot_name)
    return re.sub(rf"@{name}\b|nostr:npub1[02-9ac-hj-np-z]+", "", text).strip()


def respond(channel: str, msg: dict, reply_to: str | None, with_history: bool) -> None:
    """명령이면 바로 답하고, 아니면 에이전트에 넘긴다."""
    settings = get_settings()
    is_owner = msg.get("pubkey") == buzz_state.get("dm_owner")
    text = strip_mention(msg["content"])
    if is_owner:
        buzz_state.put(f"peer_replies:{channel}", 0)
        if text.replace(" ", "") == "새대화":
            agent.reset(channel)
            buzz.send(channel, "새 대화를 시작합니다.", reply_to)
            return
        if (result := bot.command(text)) is not None:
            buzz.send(channel, result, reply_to)
            return
        if not settings.agent_enabled:
            buzz.send(channel, bot.HELP, reply_to)
            return
    else:
        # 다른 에이전트가 부른 경우. 에이전트끼리 끝없이 주고받지 않게 주인이 다시 말할 때까지 횟수를 센다.
        count = buzz_state.get(f"peer_replies:{channel}", 0)
        if not settings.agent_enabled or count >= settings.agent_peer_replies:
            logger.info("skip peer mention %s in %s (%d replies)", msg.get("id"), channel, count)
            return
        buzz_state.put(f"peer_replies:{channel}", count + 1)
    dispatch(lambda: agent_reply(channel, msg, text, is_owner, reply_to, with_history))


def agent_reply(channel: str, msg: dict, text: str, is_owner: bool, reply_to: str | None,
                with_history: bool) -> None:
    try:
        buzz.react(msg["id"], "👀")   # 답을 만드는 중이라는 표시
    except buzz.BuzzError:
        logger.warning("could not react to %s", msg.get("id"))
    started, ok = now(), True
    try:
        names = buzz.user_names([msg["pubkey"]])
        history = channel_history(channel, msg) if with_history else []
        result = agent.reply(channel, text or "(빈 메시지)", names.get(msg["pubkey"], msg["pubkey"][:8]),
                             is_owner, history)
    except Exception as e:
        logger.exception("agent reply failed")
        result, ok = f"에이전트 오류: {str(e)[:300]}", False
    agent_turns.record(channel, is_owner, text, result, ok, started)   # 웹 흐름 화면에 보여 준다
    buzz.send(channel, result, reply_to)


def channel_history(channel: str, current: dict, limit: int = HISTORY_MESSAGES) -> list[str]:
    """에이전트에 같이 넘기는 채널의 최근 대화. 다른 에이전트와 토론할 때 맥락이 된다."""
    messages = [m for m in buzz.get_messages(channel, limit=limit + 1)
                if m.get("id") != current.get("id") and m.get("kind") == 9
                and m.get("created_at", 0) <= current.get("created_at", 0)]
    messages = sorted(messages, key=lambda m: m.get("created_at", 0))[-limit:]
    names = buzz.user_names(sorted({m["pubkey"] for m in messages}))
    return [f"- {names.get(m['pubkey'], m['pubkey'][:8])}: {m.get('content', '')[:500]}" for m in messages]


_jobs: queue.Queue | None = None


def dispatch(job) -> None:
    """에이전트 답은 수십 초 걸리므로 작업 스레드에서 돌려 알림이 밀리지 않게 한다. --once 면 바로 돈다."""
    if _jobs is None:
        job()
    else:
        _jobs.put(job)


def _worker() -> None:
    while True:
        job = _jobs.get()
        try:
            job()
        except Exception:
            logger.exception("agent job failed")


def run(once: bool = False) -> None:
    settings = get_settings()
    init_db()
    if not settings.buzz_relay_url or not settings.buzz_owner_pubkey:
        raise SystemExit("BUZZ_RELAY_URL and BUZZ_OWNER_PUBKEY are required")
    global _jobs
    if not once:
        _jobs = queue.Queue()
        threading.Thread(target=_worker, name="agent", daemon=True).start()
    try:
        buzz.set_name(settings.buzz_bot_name)
    except buzz.BuzzError:
        logger.exception("could not set bot profile name")
    while True:
        try:
            channel = dm_channel()
            if n := notify(channel):
                logger.info("notified %d events", n)
            if n := answer(channel) + answer_mentions(channel) + answer_listening():
                logger.info("answered %d messages", n)
        except Exception:
            # relay 가 잠시 안 되면 다음 주기에 다시 시도한다. 알림은 보낸 것만 기록하므로 빠지지 않는다.
            logger.exception("buzz bot cycle failed")
        if once:
            return
        time.sleep(settings.buzz_poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Notify kept events and answer commands on Buzz.")
    parser.add_argument("--once", action="store_true", help="한 번만 돌고 종료")
    parser.add_argument("--whoami", action="store_true", help="봇 프로필(pubkey)을 출력하고 종료. 연결 확인용")
    parser.add_argument("--invite", action="store_true",
                        help="커뮤니티 초대 링크(7일, 1회용)를 만들어 출력하고 종료. 봇이 relay owner 여야 한다")
    parser.add_argument("--pair-code", action="store_true",
                        help="새 사용자 키로 폰 앱 페어링 코드를 만들어 출력하고 종료. pubkey 는 relay 멤버로 따로 넣는다")
    parser.add_argument("--cmd", metavar="명령", help="DM 명령 하나를 Buzz 없이 처리하고 답을 출력 (예: --cmd '등록 2')")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.cmd:
        init_db()
        print(bot.handle(args.cmd))
        return 0
    if args.whoami:
        print(json.dumps(buzz.whoami(), ensure_ascii=False, indent=2))
        return 0
    if args.invite:
        print(json.dumps(buzz.mint_invite(), ensure_ascii=False, indent=2))
        return 0
    if args.pair_code:
        print(json.dumps(buzz.pairing_code(), ensure_ascii=False, indent=2))
        return 0
    run(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
