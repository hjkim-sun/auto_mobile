"""네이버 캘린더(CalDAV)에 일정을 넣는다.

메일 계정(MAIL_ACCOUNTS)의 애플리케이션 비밀번호로 로그인하고, CALENDAR_NAME 캘린더에 일정마다 .ics 를 PUT 한다.
넣은 일정은 UID 가 auto-mobile- 로 시작하고 설명 끝에 근거 자료 번호가 붙는다.
"""
from __future__ import annotations

import sqlite3
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import httpx

from app.core.config import MailAccountConfig, get_settings

DAV = "{DAV:}"
CALDAV = "{urn:ietf:params:xml:ns:caldav}"


class CalendarError(RuntimeError):
    pass


def _account() -> MailAccountConfig:
    s = get_settings()
    for a in s.mail_accounts:
        if a.name == s.calendar_account or (not s.calendar_account and a.host == "imap.naver.com"):
            return a
    raise CalendarError("no mail account for calendar (CALENDAR_ACCOUNT / MAIL_ACCOUNTS)")


def _client() -> httpx.Client:
    a = _account()
    return httpx.Client(base_url=get_settings().calendar_caldav_url, timeout=30,
                        auth=(a.user, a.password.get_secret_value()))


def _propfind(client: httpx.Client, url: str, depth: str, props: str) -> ET.Element:
    body = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            f"<d:prop>{props}</d:prop></d:propfind>")
    r = client.request("PROPFIND", url, content=body,
                       headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"})
    if r.status_code != 207:
        raise CalendarError(f"PROPFIND {url}: HTTP {r.status_code}")
    return ET.fromstring(r.content)


def _href(root: ET.Element, prop: str) -> str:
    el = root.find(f".//{prop}/{DAV}href")
    if el is None or not el.text:
        raise CalendarError(f"no {prop} in PROPFIND response")
    return el.text


def calendar_url(client: httpx.Client) -> str:
    """CALENDAR_NAME 캘린더의 경로를 찾는다."""
    principal = _href(_propfind(client, "/", "0", "<d:current-user-principal/>"), f"{DAV}current-user-principal")
    home = _href(_propfind(client, principal, "0", "<c:calendar-home-set/>"), f"{CALDAV}calendar-home-set")
    name = get_settings().calendar_name
    for r in _propfind(client, home, "1", "<d:displayname/><d:resourcetype/>").findall(f"{DAV}response"):
        if (r.find(f".//{DAV}resourcetype/{CALDAV}calendar") is not None
                and r.findtext(f".//{DAV}displayname") == name):
            return r.findtext(f"{DAV}href")
    raise CalendarError(f"calendar {name!r} not found")


def _escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line: str) -> str:
    """RFC 5545: 한 줄은 75바이트까지. UTF-8 글자를 자르지 않고 접는다."""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode()) > (75 if not out else 74):
            out.append(cur)
            cur = ""
        cur += ch
    return "\r\n ".join([*out, cur])


def _when(value: str) -> date | datetime:
    if "T" in value:
        return datetime.fromisoformat(value).replace(tzinfo=get_settings().tz)
    return date.fromisoformat(value)


def _ics_time(name: str, value: date | datetime) -> str:
    if isinstance(value, datetime):
        return f"{name}:{value.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
    return f"{name};VALUE=DATE:{value:%Y%m%d}"


def to_ics(item: sqlite3.Row | dict, uid: str) -> str:
    start = _when(item["start"])
    if item["end"]:
        end = _when(item["end"])
    else:
        end = start + (timedelta(hours=1) if isinstance(start, datetime) else timedelta(days=1))
    note = "\n".join(x for x in (item["note"], item["event_id"] and f"auto_mobile #{item['event_id']}") if x)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//auto_mobile//wiki//KO", "BEGIN:VEVENT",
             f"UID:{uid}", f"DTSTAMP:{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
             _ics_time("DTSTART", start), _ics_time("DTEND", end), f"SUMMARY:{_escape(item['title'])}"]
    if item["location"]:
        lines.append(f"LOCATION:{_escape(item['location'])}")
    lines += [f"DESCRIPTION:{_escape(note)}", "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def add(item: sqlite3.Row | dict) -> str:
    """일정을 넣고 UID 를 돌려준다."""
    uid = f"auto-mobile-{item['id']}-{uuid.uuid4().hex[:8]}"
    with _client() as client:
        url = calendar_url(client) + uid + ".ics"
        r = client.put(url, content=to_ics(item, uid).encode(),
                       headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"})
    if r.status_code not in (200, 201, 204):
        raise CalendarError(f"PUT {url}: HTTP {r.status_code} {r.text[:200]}")
    return uid


def remove(uid: str) -> None:
    """add() 로 넣은 일정을 지운다. 이미 없으면 그대로 둔다."""
    with _client() as client:
        url = calendar_url(client) + uid + ".ics"
        r = client.delete(url)
    if r.status_code not in (200, 204, 404):
        raise CalendarError(f"DELETE {url}: HTTP {r.status_code} {r.text[:200]}")
