"""mail_cursors 테이블: 계정·메일함별로 마지막으로 가져온 IMAP UID 를 기억한다.

UID 는 UIDVALIDITY 가 같을 때만 의미가 있다. 서버가 UIDVALIDITY 를 바꾸면 커서를 버리고 다시 잡는다.
"""
from __future__ import annotations

from contextlib import closing

from app.db.session import connect
from app.repositories.events import now


def get(account: str, mailbox: str) -> tuple[int, int] | None:
    """(uidvalidity, last_uid). 아직 없으면 None."""
    with closing(connect()) as conn:
        row = conn.execute("SELECT uidvalidity, last_uid FROM mail_cursors WHERE account = ? AND mailbox = ?",
                           (account, mailbox)).fetchone()
        return (row["uidvalidity"], row["last_uid"]) if row else None


def save(account: str, mailbox: str, uidvalidity: int, last_uid: int) -> None:
    with closing(connect()) as conn:
        conn.execute(
            "INSERT INTO mail_cursors (account, mailbox, uidvalidity, last_uid, updated_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(account, mailbox) DO UPDATE SET uidvalidity = excluded.uidvalidity,"
            " last_uid = excluded.last_uid, updated_at = excluded.updated_at",
            (account, mailbox, uidvalidity, last_uid, now()),
        )
