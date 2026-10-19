from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.db.session import init_db
from app.jobs import mail_poller
from app.repositories import events, mail_cursors


def eml(n: int) -> bytes:
    return (f"From: 홍길동 <hong@example.com>\r\nTo: me@naver.com\r\nSubject: test {n}\r\n"
            f"Message-ID: <m-{n}@example.com>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            f"본문 {n}\r\n").encode()


class FakeIMAP:
    """imaplib.IMAP4_SSL 대역. mailbox 는 {uid: 원본}."""
    mailbox: dict[int, bytes] = {}
    uidvalidity = 100
    searches: list[tuple] = []

    def __init__(self, host, timeout=None):
        self.state = "NONAUTH"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def login(self, user, password):
        if password != "secret":
            raise mail_poller.imaplib.IMAP4.error("auth failed")

    def select(self, mailbox, readonly=False):
        assert readonly
        return "OK", [str(len(self.mailbox)).encode()]

    def response(self, code):
        return code, [str(self.uidvalidity).encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            FakeIMAP.searches.append(args[1:])
            uids = sorted(self.mailbox)
            if args[1].startswith("UID "):
                lo = int(args[1].split()[1].split(":")[0])
                # 실제 서버처럼 범위 밖이어도 가장 큰 UID 를 돌려준다
                uids = [u for u in uids if u >= lo] or uids[-1:]
            return "OK", [" ".join(map(str, uids)).encode()]
        if command == "FETCH":
            assert args[1] == "(BODY.PEEK[])"
            uid = int(args[0])
            return "OK", [(f"1 (UID {uid} BODY[] {{1}}".encode(), self.mailbox[uid]), b")"]
        raise AssertionError(command)


@pytest.fixture
def imap(monkeypatch):
    monkeypatch.setenv("NAVER_IMAP_USER", "hong")
    monkeypatch.setenv("NAVER_IMAP_PASSWORD", "secret")
    get_settings.cache_clear()
    monkeypatch.setattr(mail_poller.imaplib, "IMAP4_SSL", FakeIMAP)
    FakeIMAP.mailbox = {3: eml(3), 7: eml(7)}
    FakeIMAP.uidvalidity = 100
    FakeIMAP.searches = []
    init_db()
    return FakeIMAP



def test_poll_ingests_new_mail_and_moves_cursor(imap):
    mail_poller.run(once=True)
    assert len(events.list_events("email")) == 2
    assert imap.searches[0][0] == "SINCE"
    assert mail_cursors.get("naver", "INBOX") == (100, 7)

    mail_poller.run(once=True)
    assert imap.searches[1] == ("UID 8:*",)
    assert len(events.list_events("email")) == 2

    imap.mailbox[9] = eml(9)
    mail_poller.run(once=True)
    rows = events.list_events("email")
    assert len(rows) == 3
    assert '"account": "naver"' in rows[0]["payload"]
    assert mail_cursors.get("naver", "INBOX") == (100, 9)


def test_uidvalidity_change_backfills_without_duplicates(imap):
    mail_poller.run(once=True)
    imap.uidvalidity = 200
    imap.mailbox = {1: eml(3), 2: eml(7)}
    mail_poller.run(once=True)
    assert imap.searches[-1][0] == "SINCE"
    assert len(events.list_events("email")) == 2
    assert mail_cursors.get("naver", "INBOX") == (200, 2)


def test_auth_failure_is_logged_and_skipped(imap, monkeypatch, caplog):
    monkeypatch.setenv("NAVER_IMAP_PASSWORD", "wrong")
    get_settings.cache_clear()
    mail_poller.run(once=True)
    assert "polling naver failed" in caplog.text
    assert mail_cursors.get("naver", "INBOX") is None


def test_no_account_configured(monkeypatch):
    monkeypatch.setenv("NAVER_IMAP_USER", "")
    get_settings.cache_clear()
    assert mail_poller.configured_accounts() == []
