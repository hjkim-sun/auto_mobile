"""events 테이블에서 pending 이벤트를 하나씩 꺼내 쓸모 여부를 판별한다.

반드시 1개 프로세스만 띄운다.
실행: python -m app.jobs.worker [--once]
"""
from __future__ import annotations

import argparse
import logging
import time

from app.core.config import get_settings
from app.db.session import init_db
from app.repositories import events
from app.services.classify import classify_event

logger = logging.getLogger(__name__)


def run(once: bool = False) -> None:
    settings = get_settings()
    init_db()
    if n := events.reset_stale():
        logger.warning("reset %d stale processing events", n)
    while True:
        event = events.claim_next()
        if event is None:
            if once:
                return
            time.sleep(settings.worker_poll_seconds)
            continue
        try:
            status, verdict = classify_event(event)
        except Exception as e:
            logger.exception("event %d failed (attempt %d)", event["id"], event["attempts"])
            if event["attempts"] >= settings.worker_max_attempts:
                events.finish(event["id"], "failed", error=repr(e))
            else:
                events.retry_later(event["id"], repr(e), delay=min(60 * 2 ** event["attempts"], 3600))
            continue
        events.finish(event["id"], status, verdict)
        logger.info("event %d %s → %s (useful=%s)", event["id"], event["source"], status,
                    verdict.get("useful", "-"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify pending ingest events with Jev.")
    parser.add_argument("--once", action="store_true", help="대기 중인 이벤트만 처리하고 종료")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    run(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
