"""원본을 data_dir/raw 아래 저장하고 events 테이블에 1건당 1행을 넣는다.

각 함수는 (event_id, created) 를 돌려준다. created 가 False 면 이미 받은 데이터다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
from collections.abc import AsyncIterator
from datetime import datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from uuid import uuid4

from app.core.config import get_settings
from app.repositories import call_briefs, events
from app.services.email_parse import date_text, write_parsed

log = logging.getLogger(__name__)

MESSAGE_KEY_RE = re.compile(r"^(sms|mms)-[0-9A-Za-z_]+$")
# 통화녹음 파일명 속 녹음 시각: "통화 홍길동_261020_133023.m4a", "call_20261020_133023.m4a"
CALL_DATE_RE = re.compile(r"(?<!\d)(\d{6}|\d{8})_\d{6}(?!\d)")
CALL_DIRECTIONS = ("incoming", "outgoing", "unknown")
PHONE_NUMBER_RE = re.compile(r"[0-9+*#() -]{1,40}")
KAKAO_CONTEXT = 5   # 카톡 메시지마다 앞 메시지를 이만큼 맥락으로 남긴다
KAKAO_TICKER_RE = re.compile(r"^(.+?) : ", re.S)


class IngestError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def safe_name(name: str | None, default: str) -> str:
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    return name if name not in ("", ".", "..") else default


async def receive_body(stream: AsyncIterator[bytes], dest_dir: Path) -> tuple[Path, int, str]:
    """body 를 dest_dir 안 임시 파일로 받으며 sha256 을 계산한다."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / f".incoming-{uuid4().hex}"
    digest = hashlib.sha256()
    size = 0
    with tmp.open("wb") as f:
        async for chunk in stream:
            f.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    if size == 0:
        tmp.unlink()
        raise IngestError(400, "empty body")
    return tmp, size, digest.hexdigest()


def save_body(data: bytes, dest_dir: Path) -> tuple[Path, int, str]:
    """receive_body 의 동기판. 폴러처럼 이미 메모리에 받은 데이터를 저장할 때 쓴다."""
    if not data:
        raise IngestError(400, "empty body")
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / f".incoming-{uuid4().hex}"
    tmp.write_bytes(data)
    return tmp, len(data), hashlib.sha256(data).hexdigest()


def _existing(dedup_key: str, tmp: Path) -> tuple[int, bool] | None:
    """이미 받은 것이면 방금 받은 임시 파일을 지우고 기존 id 를 돌려준다."""
    event_id = events.find_event_id(dedup_key)
    if event_id is None:
        return None
    tmp.unlink()
    return event_id, False


def _register(source: str, dedup_key: str, payload: dict, raw_path: Path, delay: float = 0,
              status: str = "pending") -> tuple[int, bool]:
    rel = str(raw_path.relative_to(get_settings().data_dir))
    return events.insert_event(source, dedup_key, payload, rel, delay, status)


def format_date(value: object) -> str | None:
    # SMS 의 date 는 밀리초, MMS 의 date 는 초 단위 epoch
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts > 1e11:
        ts /= 1000
    return datetime.fromtimestamp(ts, get_settings().tz).strftime("%Y-%m-%d %H:%M:%S")


def day_folder(date_text: str | None) -> str:
    """"YYYY-MM-DD ..." 를 날짜 폴더 이름 YYYYMMDD 로 바꾼다. 없으면 지금(TIMEZONE) 날짜."""
    return (date_text or datetime.now(get_settings().tz).isoformat())[:10].replace("-", "")


def call_info(direction: str | None, number: str | None, name: str | None,
              call_date: str | None) -> dict:
    """폰이 통화 기록에서 찾아 붙인 값을 고른다. 빈 값과 형식이 틀린 값은 버린다.

    값이 틀렸다고 녹음까지 거절하면 녹음을 잃으므로 422 를 내지 않는다.
    """
    raw = {"direction": direction, "number": number, "name": name, "call_date": call_date}
    raw = {k: v.strip() for k, v in raw.items() if v and v.strip()}
    info = {}
    if raw.get("direction") in CALL_DIRECTIONS:
        info["direction"] = raw["direction"]
    if PHONE_NUMBER_RE.fullmatch(raw.get("number", "")):
        info["number"] = raw["number"]
    if raw.get("name"):
        info["name"] = raw["name"][:100]
    try:   # Automate 가 숫자를 1.79E12 처럼 보낼 수도 있어 float 로 읽는다
        if (ms := int(float(raw.get("call_date", "")))) > 0:
            info["call_date"] = ms
            info["call_date_text"] = format_date(ms)
    except (ValueError, OverflowError):
        pass
    if dropped := raw.keys() - info.keys():
        log.warning("call info dropped: %s", {k: raw[k] for k in dropped})
    return info


def _call_day(name: str) -> str:
    m = CALL_DATE_RE.search(name)
    if m:
        day = m.group(1) if len(m.group(1)) == 8 else "20" + m.group(1)
        try:
            return datetime.strptime(day, "%Y%m%d").strftime("%Y%m%d")
        except ValueError:
            pass
    return day_folder(None)


async def ingest_call(stream: AsyncIterator[bytes], filename: str | None,
                      content_type: str | None, info: dict | None = None) -> tuple[int, bool]:
    """info: call_info() 가 고른 direction, number, name, call_date, call_date_text (모두 선택)."""
    name = safe_name(filename, datetime.now(get_settings().tz).strftime("call_%Y%m%d_%H%M%S.m4a"))
    tmp, size, sha = await receive_body(stream, get_settings().raw_dir / "calls")
    key = f"call:{sha}"
    info = info or {}
    if existing := _existing(key, tmp):
        # 통화 정보를 붙이기 전에 받은 녹음을 폰이 다시 보내면 비어 있던 값만 채운다.
        events.fill_payload(existing[0], info)
        return existing
    # 날짜 폴더는 파일명 속 녹음 시각 기준이다. 없으면 받은 날짜: calls/YYYYMMDD/
    path = tmp.parent / _call_day(name) / f"{sha[:12]}_{name}"
    path.parent.mkdir(exist_ok=True)
    tmp.replace(path)
    payload = {"filename": name, "size": size, "sha256": sha, "content_type": content_type, **info}
    # 전사가 끝나야 판별할 수 있다. 전사 스레드가 끝날 때 pending 으로 바꾼다.
    return _register("call", key, payload, path, status="transcribing")


def _json_object(body: bytes) -> dict:
    try:
        obj = json.loads(body)
        if isinstance(obj, str):   # Automate 가 JSON 문자열을 한 번 더 인코딩해 보낸 경우
            obj = json.loads(obj)
    except (ValueError, UnicodeDecodeError) as e:
        raise IngestError(400, f"invalid json: {e}")
    if not isinstance(obj, dict):
        raise IngestError(400, "json object expected")
    return obj


def ingest_message(body: bytes) -> tuple[int, bool]:
    msg = _json_object(body)

    key = f"{str(msg.get('kind') or '').lower()}-{msg.get('id')}"
    if not MESSAGE_KEY_RE.match(key):
        raise IngestError(422, "kind must be sms|mms and id is required")

    # MMS 는 parts(content://mms/part 행 목록)에서 text/plain 본문을 모아 text 로 채운다.
    parts = [p for p in msg.get("parts") or [] if isinstance(p, dict)]
    if not msg.get("text") and parts:
        msg["text"] = "\n".join(p["text"] for p in parts if p.get("ct") == "text/plain" and p.get("text"))
    msg["attachments"] = [
        {"part": p.get("_id"), "ct": p.get("ct"), "name": p.get("cl") or p.get("name")}
        for p in parts if p.get("ct") not in ("text/plain", "application/smil")
    ]
    msg["date_text"] = format_date(msg.get("date"))
    msg["received_at"] = datetime.now(get_settings().tz).isoformat(timespec="seconds")

    settings = get_settings()
    # 날짜 폴더는 문자 자체의 수신 시각 기준이다. 같은 문자가 다른 날 재전송돼도 같은 경로에 쌓인다.
    path = settings.raw_dir / "messages" / day_folder(msg["date_text"] or msg["received_at"]) / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(msg, ensure_ascii=False, indent=2), encoding="utf-8")
    result = _register("message", f"message:{key}", msg, path, delay=settings.message_settle_seconds)
    if result[1]:
        call_briefs.mark(number=msg.get("from"))   # 전화 수신 브리핑을 다시 만들게 한다
    return result


def kakao_message(note: dict) -> dict | None:
    """Automate 의 Notification posted? 출력(note)에서 메시지 1건을 꺼낸다. 받지 않을 알림이면 None.

    카카오톡은 방마다 알림 하나를 새 메시지로 갱신하고, Automate 는 갱신마다 블록을 통과시킨다.
    - android.title 은 방 이름이 아니라 마지막 메시지를 보낸 사람이다. 단톡방 이름은 알림 어디에도 없다
      (안드로이드는 shortcut 라벨로 보여 주는데 Automate 는 읽지 못한다). 단톡 여부는 isGroupConversation 으로 안다.
    - Automate 는 extras 의 android.messages(Bundle 배열)를 넘기지 않는다. 마지막 메시지의 chatLogId 는
      extras 최상위에 있다.
    """
    if note.get("channel") not in get_settings().kakao_channels:
        return None
    extras = note.get("extras") if isinstance(note.get("extras"), dict) else {}
    title = str(extras.get("android.title") or note.get("title") or "").strip()
    text = extras.get("android.text") or note.get("message")
    if not title or not text:   # 방마다 알림을 묶는 요약 알림(GROUP_SUMMARY)은 제목과 본문이 비어 있다
        return None
    m = KAKAO_TICKER_RE.match(str(note.get("ticker") or ""))   # "보낸사람 : 본문"
    room = extras.get("android.conversationTitle") or extras.get("android.hiddenConversationTitle")
    group = extras.get("android.isGroupConversation")
    try:   # Automate 숫자는 double 이라 19자리 chatLogId 는 끝자리가 뭉개져 올 수 있다
        chat_log_id = int(float(extras["chatLogId"]))
    except (KeyError, TypeError, ValueError, OverflowError):
        chat_log_id = None
    return {"sender": m.group(1) if m else title, "room": str(room) if room else None,
            "group": group if isinstance(group, bool) else None, "text": str(text),
            "time": note.get("when"), "channel": note.get("channel"), "chat_log_id": chat_log_id}


def ingest_kakao(body: bytes) -> tuple[int, bool] | None:
    """카카오톡 알림 1건 → (event_id, created). 받지 않을 알림이면 None."""
    m = kakao_message(_json_object(body))
    if m is None:
        return None
    if m["chat_log_id"] is not None:
        msg_id = str(m["chat_log_id"])
    else:   # chatLogId 가 없으면 보낸 사람, 시각, 본문으로 같은 메시지를 알아본다
        msg_id = "h" + hashlib.sha256(json.dumps([m["sender"], m["time"], m["text"]],
                                                 ensure_ascii=False).encode()).hexdigest()[:20]
    key = f"kakao:{msg_id}"
    if (event_id := events.find_event_id(key)) is not None:
        return event_id, False
    settings = get_settings()
    m["date_text"] = format_date(m["time"])
    m["received_at"] = datetime.now(settings.tz).isoformat(timespec="seconds")
    # 짧은 답장("네 그때 봬요")도 무슨 말인지 알 수 있게 앞 메시지를 붙인다.
    # 방 이름이 없으면 같은 사람이 보낸 것만 모은다 (1:1 방이면 그 방의 대화다).
    m["context"] = events.recent_kakao(m["room"], m["sender"], KAKAO_CONTEXT)
    path = settings.raw_dir / "kakao" / day_folder(m["date_text"] or m["received_at"]) / f"{msg_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    return _register("kakao", key, m, path)


async def ingest_attachment(stream: AsyncIterator[bytes], message_key: str, part_id: str,
                            filename: str | None, content_type: str | None) -> tuple[int, bool]:
    if not MESSAGE_KEY_RE.match(message_key):
        raise IngestError(404, "invalid message_key")
    event = events.find_event(f"message:{message_key}")
    if event is None:
        raise IngestError(404, "message not found; POST /v1/ingest/messages first")
    event_id = event["id"]

    settings = get_settings()
    ct = (content_type or "").split(";")[0].strip()
    default = f"part_{safe_name(part_id, 'x')}{mimetypes.guess_extension(ct) or ''}"
    # 첨부는 본문 JSON 옆, 확장자를 뗀 같은 이름의 폴더에 둔다: messages/YYYYMMDD/mms-10453/
    msg_dir = (settings.data_dir / event["raw_path"]).with_suffix("")
    tmp, _, _ = await receive_body(stream, msg_dir)
    path = msg_dir / safe_name(filename, default)
    existed = path.exists()
    tmp.replace(path)
    events.postpone(event_id, settings.message_settle_seconds)
    return event_id, not existed


async def ingest_email(stream: AsyncIterator[bytes], account: str) -> tuple[int, bool]:
    return _store_email(*await receive_body(stream, get_settings().raw_dir / "emails"), account)


def ingest_email_bytes(data: bytes, account: str, sent: bool = False) -> tuple[int, bool]:
    """메일 폴러(app/jobs/mail_poller.py)가 IMAP 으로 받은 원본을 넣는다. sent 는 보낸메일함에서 가져온 메일."""
    return _store_email(*save_body(data, get_settings().raw_dir / "emails"), account, sent)


def _store_email(tmp: Path, size: int, sha: str, account: str, sent: bool = False) -> tuple[int, bool]:
    with tmp.open("rb") as f:
        mail = BytesParser(policy=policy.default).parse(f, headersonly=True)
    message_id = str(mail.get("Message-ID") or "").strip()
    # 같은 메일이 네이버·Gmail 양쪽으로 와도 Message-ID 가 같으면 한 번만 처리한다.
    key = f"email:{message_id}" if message_id else f"email:sha256:{sha}"
    if existing := _existing(key, tmp):
        return existing
    # 날짜 폴더는 Date 헤더(TIMEZONE 기준) 기준이다. 없거나 깨졌으면 받은 날짜: emails/YYYYMMDD/
    path = tmp.parent / day_folder(date_text(mail.get("Date"))) / f"{sha[:16]}.eml"
    path.parent.mkdir(exist_ok=True)
    tmp.replace(path)
    write_parsed(path)   # 같은 이름의 .json: 헤더, 본문 텍스트, 첨부 목록
    payload = {"account": account, "sent": sent, "message_id": message_id or None,
               "from": str(mail.get("From") or ""), "to": str(mail.get("To") or ""),
               "subject": str(mail.get("Subject") or ""), "date": str(mail.get("Date") or ""),
               "size": size, "sha256": sha}
    return _register("email", key, payload, path)
