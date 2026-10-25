"""events 테이블에서 pending 이벤트를 하나씩 꺼내 쓸모 여부를 판별한다.

카톡 알림은 방 CSV 에 넣고(status=filed) 방 단위 판단(app/services/kakao_judge.py)에 맡긴다.
방 단위 판단은 이 프로세스의 스레드 하나가 KAKAO_JUDGE_POLL_SECONDS 마다 돌린다. 방을 모르는 알림만 하나씩 판별한다.
전화 수신 브리핑도 스레드 하나가 BRIEF_REFRESH_SECONDS 마다 새 대화가 들어온 사람만 다시 요약한다(app/services/call_brief.py).
반드시 1개 프로세스만 띄운다.
실행: python -m app.jobs.worker [--once]
"""
from __future__ import annotations

import argparse
import json
import logging
import threading
import time

from app.core.config import get_settings
from app.db.session import init_db
from app.repositories import call_briefs, events
from app.services import call_brief, kakao_history, kakao_judge
from app.services.classify import classify_event

logger = logging.getLogger(__name__)


def judge_loop() -> None:
    while True:
        try:
            if n := kakao_judge.run_due():
                logger.info("judged %d kakao rooms", n)
        except Exception:
            logger.exception("kakao judge failed")
        time.sleep(get_settings().kakao_judge_poll_seconds)


def brief_loop() -> None:
    if not call_briefs.any_brief() and (n := call_brief.seed()):
        logger.info("seeded %d call briefs", n)
    while True:
        try:
            if n := call_brief.refresh():
                logger.info("refreshed %d call briefs", n)
        except Exception:
            logger.exception("call brief refresh failed")
        time.sleep(get_settings().brief_refresh_seconds)


def run(once: bool = False) -> None:
    settings = get_settings()
    init_db()
    if n := events.reset_stale():
        logger.warning("reset %d stale processing events", n)
    if settings.kakao_judge_enabled and not once:
        # Claude 판단은 수십 초 걸리므로 문자·메일 판별을 막지 않게 따로 돈다
        threading.Thread(target=judge_loop, name="kakao-judge", daemon=True).start()
    if not once:
        threading.Thread(target=brief_loop, name="call-brief", daemon=True).start()
    while True:
        event = events.claim_next()
        if event is None:
            if once:
                return
            time.sleep(settings.worker_poll_seconds)
            continue
        try:
            # 폰 카톡 알림은 방 CSV 에 넣는다. 단톡 방을 Jev 가 골랐으면 그 결과도 verdict 에 남긴다.
            # 방을 찾았으면 판별은 방 단위 판단이 하고, 방을 모르면(unassigned) 이 메시지만 판별한다
            room = (kakao_history.file_notification(json.loads(event["payload"]))
                    if event["source"] == "kakao" else None)
            if room and room["chat_id"] and settings.kakao_judge_enabled:
                status, verdict = "filed", {"kakao_room": room}
            else:
                status, verdict = classify_event(event)
                if room:
                    verdict["kakao_room"] = room
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
