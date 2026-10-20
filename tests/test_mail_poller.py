from __future__ import annotations

import json

import pytest

from app.core.config import get_settings
from app.db.session import init_db
from app.jobs import mail_poller
from app.repositories import events, mail_cursors


def eml(n: int) -> bytes:
    return (f"From: 홍길동 <hong@example.com>\r\nTo: me@naver.com\r\nSubject: test {n}\r\n"
            f"Message-ID: <m-{n}@example.com>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            f"본문 {n}\r\n").encode()


def set_accounts(monkeypatch, *accounts):
    monkeypatch.setenv("MAIL_ACCOUNTS", json.dumps(list(accounts)))
    get_settings.cache_clear()


class FakeIMAP:
    """imaplib.IMAP4_SSL 대역. boxes 는 {로그인 user: {uid: 원본}}."""
    boxes: dict[str, dict[int, bytes]] = {}
    uidvalidity = 100
    searches: list[tuple] = []

    def __init__(self, host, timeout=None):
        self.mailbox: dict[int, bytes] = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def login(self, user, password):
        if password != "secret":
            raise mail_poller.imaplib.IMAP4.error("auth failed")
        self.mailbox = self.boxes[user]

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
    set_accounts(monkeypatch, {"name": "naver", "user": "hong", "password": "secret"})
    monkeypatch.setattr(mail_poller.imaplib, "IMAP4_SSL", FakeIMAP)
    FakeIMAP.boxes = {"hong": {3: eml(3), 7: eml(7)}, "kim": {}}
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

    imap.boxes["hong"][9] = eml(9)
    mail_poller.run(once=True)
    rows = events.list_events("email")
    assert len(rows) == 3
    assert '"account": "naver"' in rows[0]["payload"]
    assert mail_cursors.get("naver", "INBOX") == (100, 9)


def test_uidvalidity_change_backfills_without_duplicates(imap):
    mail_poller.run(once=True)
    imap.uidvalidity = 200
    imap.boxes["hong"] = {1: eml(3), 2: eml(7)}
    mail_poller.run(once=True)
    assert imap.searches[-1][0] == "SINCE"
    assert len(events.list_events("email")) == 2
    assert mail_cursors.get("naver", "INBOX") == (200, 2)


def test_multiple_accounts_have_separate_cursors(imap, monkeypatch):
    set_accounts(monkeypatch,
                 {"name": "naver", "user": "hong", "password": "secret"},
                 {"name": "naver-kim", "user": "kim", "password": "secret"})
    imap.boxes["kim"] = {5: eml(5)}
    mail_poller.run(once=True)
    accounts = sorted(json.loads(e["payload"])["account"] for e in events.list_events("email"))
    assert accounts == ["naver", "naver", "naver-kim"]
    assert mail_cursors.get("naver", "INBOX") == (100, 7)
    assert mail_cursors.get("naver-kim", "INBOX") == (100, 5)


def test_auth_failure_does_not_block_other_accounts(imap, monkeypatch, caplog):
    set_accounts(monkeypatch,
                 {"name": "naver", "user": "hong", "password": "wrong"},
                 {"name": "naver-kim", "user": "kim", "password": "secret"})
    imap.boxes["kim"] = {5: eml(5)}
    mail_poller.run(once=True)
    assert "polling naver failed" in caplog.text
    assert mail_cursors.get("naver", "INBOX") is None
    assert mail_cursors.get("naver-kim", "INBOX") == (100, 5)


def test_account_names_must_be_unique(monkeypatch):
    acct = {"name": "naver", "user": "hong", "password": "secret"}
    set_accounts(monkeypatch, acct, acct)
    with pytest.raises(ValueError):
        get_settings()


def test_no_account_configured():
    assert mail_poller.configured_accounts() == []
