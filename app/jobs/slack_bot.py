"""Slack 봇. kept 로 판별된 이벤트를 사용자에게 알리고, 말을 걸면 답한다. app/jobs/buzz_bot.py 를 Slack 으로 옮긴 것.

- 알림은 SLACK_NOTIFY_CHANNEL(없으면 주인과의 DM)로 보낸다. AGENT_BRIEFING 이면 에이전트가 위키 맥락을 붙여
  브리핑하고, 실패하면 원문 알림을 보낸다.
- 주인의 DM 과, 다른 채널에서 봇을 멘션한 말(스레드로 답)에 답한다.
- 듣는 채널(SLACK_LISTEN_CHANNELS)에서는 주인의 말에 멘션 없이도 답한다.
- 명령(app/services/bot.py)은 바로 답하고, 그 밖의 말은 에이전트(app/services/agent.py, Claude Code)가 답한다.
- 다른 봇이 멘션하면 주인이 다시 말할 때까지 AGENT_PEER_REPLIES 번까지만 답한다.
이벤트는 Socket Mode(웹소켓)로 받는다. 반드시 1개 프로세스만 띄운다. Buzz 봇과 함께 띄우지 않는다(알림 기록을 같이 쓴다).
처음 띄울 때 이미 있던 이벤트는 알리지 않는다(buzz_state.notify_after_id).
실행: python -m app.jobs.slack_bot [--once | --whoami | --cmd 명령]
"""
from __future__ import annotations

import argparse
import json
import logging
import queue
import threading
import time
from collections import OrderedDict

from app.core.config import get_settings
from app.db.session import init_db
from app.repositories import agent_turns, buzz_state, calendar_items
from app.repositories.events import now
from app.services import agent, bot, slack

HISTORY_MESSAGES = 10

logger = logging.getLogger(__name__)


def dm_channel() -> str:
    owner = get_settings().slack_owner_user_id
    channel = buzz_state.get("slack_dm_channel")
    if buzz_state.get("slack_dm_owner") != owner or not channel:
        channel = slack.open_dm(owner)
        buzz_state.put("slack_dm_channel", channel)
        buzz_state.put("slack_dm_owner", owner)
        logger.info("opened DM channel %s with owner", channel)
    return channel


def notify() -> int:
    """kept 알림과 캘린더 알림. SLACK_NOTIFY_CHANNEL 이 있으면 그 채널로, 없으면 DM 으로 보낸다."""
    settings = get_settings()
    channel = settings.slack_notify_channel or dm_channel()
    after = buzz_state.get("notify_after_id")
    if after is None:
        after = buzz_state.max_event_id()
        buzz_state.put("notify_after_id", after)
    rows = buzz_state.unnotified_kept(after)
    if rows and settings.agent_enabled and settings.agent_briefing:
        # 브리핑은 작업 스레드에서 수십 초 걸린다. 다음 주기에 또 넘기지 않게 먼저 알린 것으로 적는다.
        for event in rows:
            buzz_state.mark_notified(event["id"])
        dispatch(lambda: send_briefing(channel, rows))
    else:
        for event in rows:
            slack.send(channel, bot.notification(event))
            buzz_state.mark_notified(event["id"])
    sent = len(rows)
    # 위키 빌더가 캘린더에 넣은 일정은 알리고, 애매한 일정은 넣을지 묻는다.
    for item in calendar_items.unnotified():
        slack.send(channel, bot.calendar_message(item))
        calendar_items.mark_notified(item["id"])
        sent += 1
    return sent


def send_briefing(channel: str, rows: list) -> None:
    try:
        text = agent.brief(rows)
    except Exception:
        logger.exception("briefing failed, sending plain notifications")
        for event in rows:
            slack.send(channel, bot.notification(event))
        return
    refs = " ".join(f"#{r['id']}" for r in rows)
    slack.send(channel, f"{text}\n\n원문: {refs}")


_seen: OrderedDict[tuple[str, str], None] = OrderedDict()


def _first_time(channel: str, ts: str) -> bool:
    """Slack 은 응답이 늦으면 같은 이벤트를 다시 보내고, 멘션은 message 와 app_mention 으로 두 번 온다."""
    key = (channel, ts)
    if key in _seen:
        return False
    _seen[key] = None
    while len(_seen) > 1000:
        _seen.popitem(last=False)
    return True


def handle_event(event: dict) -> bool:
    """답할 말이면 respond 로 넘기고 True."""
    settings = get_settings()
    kind, user, text = event.get("type"), event.get("user"), event.get("text")
    if kind not in ("message", "app_mention") or event.get("subtype") not in (None, "file_share", "thread_broadcast"):
        return False
    me, owner = slack.bot_user_id(), settings.slack_owner_user_id
    if not user or user == me or not isinstance(text, str):
        return False
    channel, ts = event["channel"], event["ts"]
    thread = event.get("thread_ts")
    in_thread = thread is not None and thread != ts
    if kind == "message":
        if event.get("channel_type") == "im":
            if user != owner:
                return False
            reply_to, with_history = (thread if in_thread else None), False
        elif channel in settings.slack_listen_channels and user == owner:
            # 다른 사람을 콕 집어 부른 말에는 끼어들지 않는다
            mentioned = slack.mentioned(text)
            if mentioned and me not in mentioned:
                return False
            reply_to, with_history = (thread if in_thread else None), True
        else:
            return False   # 그 밖의 채널은 멘션(app_mention)일 때만 답한다
    else:
        if channel in settings.slack_listen_channels and user == owner:
            return False   # 위의 message 로 이미 받는다
        reply_to, with_history = thread or ts, True
    if not _first_time(channel, ts):
        return False
    respond(channel, event, reply_to, with_history)
    return True


def respond(channel: str, msg: dict, reply_to: str | None, with_history: bool) -> None:
    """명령이면 바로 답하고, 아니면 에이전트에 넘긴다."""
    settings = get_settings()
    is_owner = msg["user"] == settings.slack_owner_user_id
    text = slack.plain(msg["text"])
    if is_owner:
        buzz_state.put(f"peer_replies:{channel}", 0)
        if text.replace(" ", "") == "새대화":
            agent.reset(channel)
            slack.send(channel, "새 대화를 시작합니다.", reply_to)
            return
        if (result := bot.command(text)) is not None:
            slack.send(channel, result, reply_to)
            return
        if not settings.agent_enabled:
            slack.send(channel, bot.HELP, reply_to)
            return
    else:
        # 다른 봇이나 사람이 부른 경우. 에이전트끼리 끝없이 주고받지 않게 주인이 다시 말할 때까지 횟수를 센다.
        count = buzz_state.get(f"peer_replies:{channel}", 0)
        if not settings.agent_enabled or count >= settings.agent_peer_replies:
            logger.info("skip peer mention %s in %s (%d replies)", msg.get("ts"), channel, count)
            return
        buzz_state.put(f"peer_replies:{channel}", count + 1)
    dispatch(lambda: agent_reply(channel, msg, text, is_owner, reply_to, with_history))


def agent_reply(channel: str, msg: dict, text: str, is_owner: bool, reply_to: str | None,
                with_history: bool) -> None:
    try:
        slack.react(channel, msg["ts"], "eyes")   # 답을 만드는 중이라는 표시
    except Exception:
        logger.warning("could not react to %s", msg.get("ts"))
    started, ok = now(), True
    try:
        history = channel_history(channel, msg) if with_history else []
        result = agent.reply(channel, text or "(빈 메시지)", slack.user_name(msg["user"]), is_owner, history)
    except Exception as e:
        logger.exception("agent reply failed")
        result, ok = f"에이전트 오류: {str(e)[:300]}", False
    agent_turns.record(channel, is_owner, text, result, ok, started)   # 웹 흐름 화면에 보여 준다
    slack.send(channel, result, reply_to)


def channel_history(channel: str, current: dict, limit: int = HISTORY_MESSAGES) -> list[str]:
    """에이전트에 같이 넘기는 채널(스레드 안이면 그 스레드)의 최근 대화. 다른 에이전트와 토론할 때 맥락이 된다."""
    thread = current.get("thread_ts")
    messages = slack.history(channel, current["ts"], thread if thread != current["ts"] else None, limit)
    lines = []
    for m in messages:
        who = slack.user_name(m["user"]) if m.get("user") else m.get("username") or "bot"
        lines.append(f"- {who}: {slack.plain(m.get('text') or '')[:500]}")
    return lines


_jobs: queue.Queue | None = None


def dispatch(job) -> None:
    """에이전트 답은 수십 초 걸리므로 작업 스레드에서 돌려 알림과 이벤트 수신이 밀리지 않게 한다. --once 면 바로 돈다."""
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


def _on_request(client, req) -> None:
    from slack_sdk.socket_mode.response import SocketModeResponse

    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))   # 3초 안에 받았다고 답한다
    if req.type != "events_api":
        return
    try:
        handle_event(req.payload.get("event") or {})
    except Exception:
        logger.exception("slack event failed")


def run(once: bool = False) -> None:
    settings = get_settings()
    init_db()
    if not settings.slack_bot_token.get_secret_value() or not settings.slack_owner_user_id:
        raise SystemExit("SLACK_BOT_TOKEN and SLACK_OWNER_USER_ID are required")
    if once:
        notify()
        return
    if not settings.slack_app_token.get_secret_value():
        raise SystemExit("SLACK_APP_TOKEN is required")
    from slack_sdk.socket_mode import SocketModeClient

    global _jobs
    _jobs = queue.Queue()
    threading.Thread(target=_worker, name="agent", daemon=True).start()
    socket = SocketModeClient(app_token=settings.slack_app_token.get_secret_value(), web_client=slack.client())
    socket.socket_mode_request_listeners.append(_on_request)
    socket.connect()   # 끊기면 slack_sdk 가 다시 잇는다
    logger.info("connected to slack as %s", slack.bot_user_id())
    while True:
        try:
            if n := notify():
                logger.info("notified %d events", n)
        except Exception:
            # Slack 이 잠시 안 되면 다음 주기에 다시 시도한다. 알림은 보낸 것만 기록하므로 빠지지 않는다.
            logger.exception("slack notify failed")
        time.sleep(settings.slack_notify_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Notify kept events and answer commands on Slack.")
    parser.add_argument("--once", action="store_true", help="알릴 것만 한 번 보내고 종료")
    parser.add_argument("--whoami", action="store_true", help="봇 정보(auth.test)를 출력하고 종료. 토큰 확인용")
    parser.add_argument("--cmd", metavar="명령", help="명령 하나를 Slack 없이 처리하고 답을 출력 (예: --cmd '등록 2')")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.cmd:
        init_db()
        print(bot.handle(args.cmd))
        return 0
    if args.whoami:
        print(json.dumps(slack.whoami(), ensure_ascii=False, indent=2))
        return 0
    run(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
