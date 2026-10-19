"""IMAP 메일함을 주기적으로 폴링해 새 메일을 events 에 넣는다. 판별은 판별 워커가 한다.

메일은 읽음 처리하지 않는다(BODY.PEEK, 읽기 전용 SELECT). 어디까지 가져왔는지는 mail_cursors 에 남긴다.
같은 메일이 다시 와도 Message-ID 로 중복을 거른다(app/services/ingest.py).
실행: python -m app.jobs.mail_poller [--once]
"""
from __future__ import annotations

import argparse
import imaplib
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta

from app.core.config import get_settings
from app.db.session import init_db
from app.repositories import mail_cursors
from app.services.ingest import ingest_email_bytes

logger = logging.getLogger(__name__)

MAILBOX = "INBOX"
IMAP_TIMEOUT = 60
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


@dataclass(frozen=True)
class MailAccount:
    name: str          # events.payload.account
    host: str
    user: str
    password: str


def configured_accounts() -> list[MailAccount]:
    s = get_settings()
    accounts = []
    if s.naver_imap_user:
        accounts.append(MailAccount("naver", s.naver_imap_host, s.naver_imap_user,
                                    s.naver_imap_password.get_secret_value()))
    return accounts


def _check(result: tuple[str, list]) -> list:
    typ, data = result
    if typ != "OK":
        raise imaplib.IMAP4.error(f"{typ} {data!r}")
    return data


def _imap_date(d: date) -> str:
    # SEARCH SINCE 는 로캘과 무관하게 영어 월 이름을 쓴다: 19-Oct-2026
    return f"{d.day}-{MONTHS[d.month - 1]}-{d.year}"


def _new_uids(imap: imaplib.IMAP4, cursor: tuple[int, int] | None, uidvalidity: int) -> list[int]:
    if cursor and cursor[0] == uidvalidity:
        last = cursor[1]
        data = _check(imap.uid("SEARCH", None, f"UID {last + 1}:*"))
    else:
        if cursor:
            logger.warning("UIDVALIDITY changed (%s → %s); backfilling", cursor[0], uidvalidity)
        last = 0
        since = date.today() - timedelta(days=get_settings().mail_backfill_days)
        data = _check(imap.uid("SEARCH", None, "SINCE", _imap_date(since)))
    # "n:*" 는 새 메일이 없어도 가장 큰 UID 하나를 돌려주므로 last 이하는 버린다.
    return sorted(u for u in map(int, (data[0] or b"").split()) if u > last)


def _fetch(imap: imaplib.IMAP4, uid: int) -> bytes:
    for item in _check(imap.uid("FETCH", str(uid), "(BODY.PEEK[])")):
        if isinstance(item, tuple):
            return item[1]
    raise imaplib.IMAP4.error(f"no body for uid {uid}")


def poll_account(account: MailAccount) -> int:
    """새로 넣은 메일 수를 돌려준다."""
    created = 0
    with imaplib.IMAP4_SSL(account.host, timeout=IMAP_TIMEOUT) as imap:
        imap.login(account.user, account.password)
        _check(imap.select(MAILBOX, readonly=True))
        _, (raw_validity,) = imap.response("UIDVALIDITY")
        uidvalidity = int(re.sub(rb"\D", b"", raw_validity))
        cursor = mail_cursors.get(account.name, MAILBOX)
        uids = _new_uids(imap, cursor, uidvalidity)
        if not uids and (cursor is None or cursor[0] != uidvalidity):
            mail_cursors.save(account.name, MAILBOX, uidvalidity, 0)
        for uid in uids:
            event_id, new = ingest_email_bytes(_fetch(imap, uid), account.name)
            # 한 통씩 커서를 옮겨, 도중에 끊겨도 다음 폴링이 이어서 가져간다.
            mail_cursors.save(account.name, MAILBOX, uidvalidity, uid)
            if new:
                created += 1
                logger.info("%s uid %d → event %d", account.name, uid, event_id)
    return created


def run(once: bool = False) -> None:
    settings = get_settings()
    init_db()
    accounts = configured_accounts()
    if not accounts:
        logger.error("no mail account configured (NAVER_IMAP_USER)")
    while True:
        for account in accounts:
            try:
                poll_account(account)
            except Exception:
                # 인증 실패·네트워크 오류는 다음 주기에 다시 시도한다.
                logger.exception("polling %s failed", account.name)
        if once:
            return
        time.sleep(settings.mail_poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Poll IMAP mailboxes and ingest new mail.")
    parser.add_argument("--once", action="store_true", help="한 번만 폴링하고 종료")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
