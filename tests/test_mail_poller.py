from __future__ import annotations

import json
from contextlib import closing

import pytest

from app.core.config import get_settings
from app.db.session import connect, init_db
from app.jobs import mail_poller
from app.repositories import buzz_state, events, mail_cursors, wiki_ingests
from app.services.render import render


def eml(n: int) -> bytes:
    return (f"From: 홍길동 <hong@example.com>\r\nTo: me@naver.com\r\nSubject: test {n}\r\n"
            f"Message-ID: <m-{n}@example.com>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            f"본문 {n}\r\n").encode()


def set_accounts(monkeypatch, *accounts):
    monkeypatch.setenv("MAIL_ACCOUNTS", json.dumps(list(accounts)))
    get_settings.cache_clear()


class FakeIMAP:
    """imaplib.IMAP4_SSL 대역. boxes 는 {로그인 user: {uid: 원본}} (받은편지함), sent 는 같은 꼴의 보낸메일함."""
    boxes: dict[str, dict[int, bytes]] = {}
    sent: dict[str, dict[int, bytes]] = {}
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
        self.user = user

    def list(self):
        lines = [b'(\\HasNoChildren \\Inbox) "/" "INBOX"', b'(\\HasNoChildren \\Drafts) "/" "Drafts"']
        if self.user in self.sent:
            lines.append(b'(\\HasNoChildren \\Sent) "/" "Sent Messages"')
        return "OK", lines

    def select(self, mailbox, readonly=False):
        assert readonly
        self.mailbox = {'"INBOX"': self.boxes, '"Sent Messages"': self.sent}[mailbox][self.user]
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
    FakeIMAP.sent = {}
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


def test_sent_mailbox_is_polled_with_its_own_cursor(imap):
    imap.sent = {"hong": {4: eml(20)}}
    mail_poller.run(once=True)
    rows = events.list_events("email")
    assert len(rows) == 3
    assert [json.loads(r["payload"])["sent"] for r in rows if "m-20" in r["payload"]] == [True]
    assert sum(json.loads(r["payload"])["sent"] for r in rows) == 1
    assert mail_cursors.get("naver", "INBOX") == (100, 7)
    assert mail_cursors.get("naver", "Sent Messages") == (100, 4)

    # 보낸 메일은 판별 없이 바로 kept 로 들어가 위키 대상이 되고, Buzz 로는 알리지 않는다
    sent_id = next(r["id"] for r in rows if "m-20" in r["payload"])
    assert {r["id"]: r["status"] for r in rows}[sent_id] == "kept"
    assert [r["id"] for r in wiki_ingests.not_ingested()] == [sent_id]
    with closing(connect()) as conn:
        conn.execute("UPDATE events SET status = 'kept'")
    assert sent_id not in [r["id"] for r in buzz_state.unnotified_kept(0)]
    assert len(buzz_state.unnotified_kept(0)) == 2
    assert "보낸메일함" in render(next(r for r in rows if r["id"] == sent_id))


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
