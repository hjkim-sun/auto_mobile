"""LLM 위키 빌더. 아직 위키에 넣지 않은 kept 이벤트를 모아 Claude Code 로 data/wiki 에 정리한다.

처음 띄우면 이미 있던 kept 이벤트도 모두 넣는다. 통화는 전사가 끝난 뒤에 넣는다.
넣으면서 Claude 가 고른 캘린더 일정 중 decision=add 는 바로 네이버 캘린더에 넣고,
decision=ask 는 Buzz 봇이 사용자에게 묻는다("등록 N" 이면 봇이 넣는다).
동시에 하나만 돈다(data/wiki.lock). 위키 규칙은 prompts/wiki.md, 구현은 app/services/wiki.py.
실행: python -m app.jobs.wiki_builder [--once | --ask 질문 | --lint | --plan-calendar]
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import time

from app.core.config import get_settings
from app.db.session import init_db
from app.repositories import calendar_items, wiki_ingests
from app.services import calendar, wiki

logger = logging.getLogger(__name__)


def ingest_batch() -> int:
    """준비된 자료를 WIKI_BATCH_SIZE 건까지 넣는다. 넣은 건수."""
    items = []
    for event in wiki_ingests.not_ingested():
        text = wiki.source_text(event)
        if text is not None:
            items.append((event, text))
        if len(items) >= get_settings().wiki_batch_size:
            break
    if not items:
        return 0
    proposals = wiki.ingest(items)
    ids = [event["id"] for event, _ in items]
    wiki_ingests.mark(ids)
    save_proposals(proposals, ids)
    return len(items)


def save_proposals(proposals: list[dict], source_ids: list[int]) -> int:
    saved = 0
    for p in proposals:
        if p.get("source_id") not in source_ids:
            logger.warning("calendar proposal with unknown source %s dropped: %s", p.get("source_id"), p.get("title"))
            continue
        item_id = calendar_items.insert({**p, "event_id": p["source_id"]})
        logger.info("calendar item %d (%s) %s %s", item_id, p["decision"], p["start"], p["title"])
        saved += 1
    return saved


def add_pending_to_calendar() -> int:
    """등록 대기(pending) 일정을 캘린더에 넣는다. 실패하면 남겨 두고 다음 주기에 다시 한다."""
    if not get_settings().calendar_enabled:
        return 0
    added = 0
    for item in calendar_items.with_status("pending"):
        try:
            uid = calendar.add(item)
        except Exception as e:
            logger.exception("calendar item %d failed", item["id"])
            calendar_items.set_error(item["id"], repr(e))
            continue
        calendar_items.set_status(item["id"], "added", caldav_uid=uid)
        added += 1
    return added


def run(once: bool = False) -> None:
    settings = get_settings()
    init_db()
    while True:
        try:
            # 쌓인 것이 있으면 쉬지 않고 이어서 넣는다.
            while n := ingest_batch():
                logger.info("ingested %d events into wiki", n)
        except Exception:
            # claude 가 실패하면 표시하지 않았으므로 다음 주기에 같은 자료를 다시 넣는다.
            logger.exception("wiki ingest failed")
        if n := add_pending_to_calendar():
            logger.info("added %d items to calendar", n)
        if once:
            return
        time.sleep(settings.wiki_poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an LLM-maintained wiki from kept events.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="쌓인 자료만 넣고 종료")
    group.add_argument("--ask", metavar="질문", help="위키에 질문하고 답을 출력")
    group.add_argument("--lint", action="store_true", help="위키를 점검하고 고침 (깨진 링크, 중복, 모순, 지난 일정)")
    group.add_argument("--plan-calendar", action="store_true",
                       help="이미 위키에 있는 일정에서 캘린더 후보를 골라 등록하거나 물을 목록에 넣음")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.ask:
        print(wiki.ask(args.ask))
        return 0
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with open(settings.data_dir / "wiki.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another wiki_builder is running") from None
        if args.lint:
            print(wiki.lint())
        elif args.plan_calendar:
            init_db()
            saved = save_proposals(wiki.plan_calendar(), [r["id"] for r in wiki_ingests.ingested()])
            print(f"{saved} calendar items, {add_pending_to_calendar()} added")
        else:
            run(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
