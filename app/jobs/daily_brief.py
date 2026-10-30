"""하루 브리핑. 오늘 할 일 위주로 에이전트가 정리해 Slack 으로 보낸다. cron 이 매일 아침 한 번 띄운다(deploy/crontab).

재료는 캘린더 후보(오늘·앞으로 며칠·답을 기다리는 것)와 지난 하루의 kept 자료이고, 위키의 schedule.md 는 에이전트가
직접 읽는다. 규칙은 prompts/agent.md 의 '하루 브리핑'. 에이전트가 실패하면 일정 목록만 보낸다.
같은 날 두 번 보내지 않는다(buzz_state.daily_brief_date). 보내는 곳은 kept 알림과 같다(SLACK_NOTIFY_CHANNEL, 없으면 DM).
실행: python -m app.jobs.daily_brief [--dry-run | --force]
"""
from __future__ import annotations

import argparse
import logging
from contextlib import closing
from datetime import date, datetime, timedelta

from app.core.config import get_settings
from app.db.session import connect, init_db
from app.jobs.slack_bot import dm_channel
from app.repositories import buzz_state, calendar_items
from app.repositories.events import now
from app.services import agent, bot, slack

AHEAD_DAYS = 3   # 오늘 뒤로 미리 볼 날 수

logger = logging.getLogger(__name__)


def recent_kept(hours: int = 24) -> list:
    with closing(connect()) as conn:
        return conn.execute("SELECT * FROM events WHERE status = 'kept' AND created_at >= ? ORDER BY id",
                            (now(-hours * 3600),)).fetchall()


def facts(today: date) -> tuple[list, list, list]:
    """(오늘 일정, 앞으로 며칠 일정, 답을 기다리는 일정). 캘린더에 넣었거나 넣을 것만."""
    end = (today + timedelta(days=AHEAD_DAYS + 1)).isoformat()
    items = [i for i in calendar_items.upcoming(today.isoformat())
             if i["start"] < end and i["status"] in ("added", "pending")]
    todays = [i for i in items if i["start"][:10] == today.isoformat()]
    ahead = [i for i in items if i["start"][:10] != today.isoformat()]
    return todays, ahead, calendar_items.with_status("asking")


def _lines(items: list) -> list[str]:
    return [f"- {bot.calendar_line(i)}" for i in items] or ["- 없음"]


def brief_prompt(today: date, todays: list, ahead: list, asking: list, events: list) -> str:
    day = f"{today.isoformat()} ({bot.WEEKDAYS[today.weekday()]})"
    parts = [f"하루 브리핑을 만든다 (규칙의 '하루 브리핑'). 오늘은 {day} 이다.", "",
             "오늘 캘린더 일정:", *_lines(todays), "",
             f"앞으로 {AHEAD_DAYS}일 캘린더 일정:", *_lines(ahead), "",
             "캘린더에 넣을지 주인의 답을 기다리는 일정:", *_lines(asking), "",
             "지난 24시간에 남긴 자료:", *([f"- {bot.summary_line(e)}" for e in events] or ["- 없음"])]
    return "\n".join(parts)


def plain(today: date, todays: list, ahead: list, asking: list) -> str:
    """에이전트가 실패했을 때 보내는 일정 목록."""
    parts = [f"오늘 {today.month}/{today.day}({bot.WEEKDAYS[today.weekday()]}) 일정", *_lines(todays), "",
             f"앞으로 {AHEAD_DAYS}일", *_lines(ahead)]
    if asking:
        parts += ["", "넣을지 답을 기다리는 일정", *_lines(asking)]
    return "\n".join(parts)


def build(today: date) -> str:
    todays, ahead, asking = facts(today)
    if get_settings().agent_enabled:
        try:
            out = agent._run(None, False, brief_prompt(today, todays, ahead, asking, recent_kept()), research=True)
            if text := (out.get("result") or "").strip():
                return text
        except agent.AgentError:
            logger.exception("daily brief agent failed, sending plain schedule")
    return plain(today, todays, ahead, asking)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send today's to-do briefing to Slack.")
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    parser.add_argument("--force", action="store_true", help="send even if already sent today")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()
    settings = get_settings()
    today = datetime.now(settings.tz).date()
    if not args.dry_run and not args.force and buzz_state.get("daily_brief_date") == today.isoformat():
        logger.info("already sent for %s", today)
        return 0
    text = build(today)
    if args.dry_run:
        print(text)
        return 0
    slack.send(settings.slack_notify_channel or dm_channel(), text)
    buzz_state.put("daily_brief_date", today.isoformat())
    logger.info("sent daily brief for %s", today)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
