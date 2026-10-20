"""Buzz 봇. kept 로 판별된 이벤트를 사용자에게 DM 으로 알리고, 사용자의 DM 명령에 답한다.

반드시 1개 프로세스만 띄운다. 명령은 app/services/bot.py 참고.
처음 띄울 때 이미 있던 이벤트는 알리지 않는다(buzz_state.notify_after_id).
실행: python -m app.jobs.buzz_bot [--once | --whoami | --invite | --pair-code | --cmd 명령]
"""
from __future__ import annotations

import argparse
import json
import logging
import time

from app.core.config import get_settings
from app.db.session import init_db
from app.repositories import buzz_state, calendar_items
from app.services import bot, buzz

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


def notify(channel: str) -> int:
    after = buzz_state.get("notify_after_id")
    if after is None:
        after = buzz_state.max_event_id()
        buzz_state.put("notify_after_id", after)
    sent = 0
    for event in buzz_state.unnotified_kept(after):
        buzz.send(channel, bot.notification(event))
        buzz_state.mark_notified(event["id"])
        sent += 1
    # 위키 빌더가 캘린더에 넣은 일정은 알리고, 애매한 일정은 넣을지 묻는다.
    for item in calendar_items.unnotified():
        buzz.send(channel, bot.calendar_message(item))
        calendar_items.mark_notified(item["id"])
        sent += 1
    return sent


def answer(channel: str) -> int:
    owner = buzz_state.get("dm_owner")
    since = buzz_state.get("dm_since", int(time.time()))
    seen = set(buzz_state.get("dm_seen", []))
    # --since 는 초 단위라 같은 초의 메시지가 다시 올 수 있다. 마지막 초의 id 를 기억해 거른다.
    messages = sorted(buzz.get_messages(channel, since=since), key=lambda m: m.get("created_at", 0))
    replied = 0
    for msg in messages:
        if msg.get("id") in seen or msg.get("created_at", 0) < since:
            continue
        if msg.get("pubkey") == owner and isinstance(msg.get("content"), str):
            buzz.send(channel, bot.handle(msg["content"]))
            replied += 1
        if msg.get("created_at", 0) > since:
            since, seen = msg["created_at"], set()
        seen.add(msg.get("id"))
        buzz_state.put("dm_since", since)
        buzz_state.put("dm_seen", sorted(seen))
    return replied


def run(once: bool = False) -> None:
    settings = get_settings()
    init_db()
    if not settings.buzz_relay_url or not settings.buzz_owner_pubkey:
        raise SystemExit("BUZZ_RELAY_URL and BUZZ_OWNER_PUBKEY are required")
    while True:
        try:
            channel = dm_channel()
            if n := notify(channel):
                logger.info("notified %d events", n)
            if n := answer(channel):
                logger.info("answered %d commands", n)
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
