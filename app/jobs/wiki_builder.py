"""LLM 위키 빌더. 아직 위키에 넣지 않은 kept 이벤트를 모아 Claude Code 로 data/wiki 에 정리한다.

처음 띄우면 이미 있던 kept 이벤트도 모두 넣는다. 통화는 전사가 끝난 뒤에 넣는다.
동시에 하나만 돈다(data/wiki.lock). 위키 규칙은 prompts/wiki.md, 구현은 app/services/wiki.py.
실행: python -m app.jobs.wiki_builder [--once | --ask 질문 | --lint]
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import time

from app.core.config import get_settings
from app.db.session import init_db
from app.repositories import wiki_ingests
from app.services import wiki

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
    wiki.ingest(items)
    wiki_ingests.mark([event["id"] for event, _ in items])
    return len(items)


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
        if once:
            return
        time.sleep(settings.wiki_poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an LLM-maintained wiki from kept events.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="쌓인 자료만 넣고 종료")
    group.add_argument("--ask", metavar="질문", help="위키에 질문하고 답을 출력")
    group.add_argument("--lint", action="store_true", help="위키를 점검하고 고침 (깨진 링크, 중복, 모순, 지난 일정)")
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
        else:
            run(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
