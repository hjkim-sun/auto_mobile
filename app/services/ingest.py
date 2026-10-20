"""원본을 data_dir/raw 아래 저장하고 events 테이블에 1건당 1행을 넣는다.

각 함수는 (event_id, created) 를 돌려준다. created 가 False 면 이미 받은 데이터다.
"""
from __future__ import annotations

import hashlib
import json
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
from app.repositories import events
from app.services.email_parse import write_parsed

MESSAGE_KEY_RE = re.compile(r"^(sms|mms)-[0-9A-Za-z_]+$")


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


def _register(source: str, dedup_key: str, payload: dict, raw_path: Path, delay: float = 0) -> tuple[int, bool]:
    rel = str(raw_path.relative_to(get_settings().data_dir))
    return events.insert_event(source, dedup_key, payload, rel, delay)


def format_date(value: object) -> str | None:
    # SMS 의 date 는 밀리초, MMS 의 date 는 초 단위 epoch
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts > 1e11:
        ts /= 1000
    return datetime.fromtimestamp(ts, get_settings().tz).strftime("%Y-%m-%d %H:%M:%S")


async def ingest_call(stream: AsyncIterator[bytes], filename: str | None,
                      content_type: str | None) -> tuple[int, bool]:
    name = safe_name(filename, datetime.now(get_settings().tz).strftime("call_%Y%m%d_%H%M%S.m4a"))
    tmp, size, sha = await receive_body(stream, get_settings().raw_dir / "calls")
    key = f"call:{sha}"
    if existing := _existing(key, tmp):
        return existing
    path = tmp.with_name(f"{sha[:12]}_{name}")
    tmp.replace(path)
    payload = {"filename": name, "size": size, "sha256": sha, "content_type": content_type}
    return _register("call", key, payload, path)


def ingest_message(body: bytes) -> tuple[int, bool]:
    try:
        msg = json.loads(body)
        if isinstance(msg, str):   # Automate 가 JSON 문자열을 한 번 더 인코딩해 보낸 경우
            msg = json.loads(msg)
    except (ValueError, UnicodeDecodeError) as e:
        raise IngestError(400, f"invalid json: {e}")
    if not isinstance(msg, dict):
        raise IngestError(400, "json object expected")

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
    day = (msg["date_text"] or msg["received_at"])[:10].replace("-", "")
    path = settings.raw_dir / "messages" / day / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(msg, ensure_ascii=False, indent=2), encoding="utf-8")
    return _register("message", f"message:{key}", msg, path, delay=settings.message_settle_seconds)


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


def ingest_email_bytes(data: bytes, account: str) -> tuple[int, bool]:
    """메일 폴러(app/jobs/mail_poller.py)가 IMAP 으로 받은 원본을 넣는다."""
    return _store_email(*save_body(data, get_settings().raw_dir / "emails"), account)


def _store_email(tmp: Path, size: int, sha: str, account: str) -> tuple[int, bool]:
    with tmp.open("rb") as f:
        mail = BytesParser(policy=policy.default).parse(f, headersonly=True)
    message_id = str(mail.get("Message-ID") or "").strip()
    # 같은 메일이 네이버·Gmail 양쪽으로 와도 Message-ID 가 같으면 한 번만 처리한다.
    key = f"email:{message_id}" if message_id else f"email:sha256:{sha}"
    if existing := _existing(key, tmp):
        return existing
    path = tmp.with_name(f"{sha[:16]}.eml")
    tmp.replace(path)
    write_parsed(path)   # 같은 이름의 .json: 헤더, 본문 텍스트, 첨부 목록
    payload = {"account": account, "message_id": message_id or None,
               "from": str(mail.get("From") or ""), "to": str(mail.get("To") or ""),
               "subject": str(mail.get("Subject") or ""), "date": str(mail.get("Date") or ""),
               "size": size, "sha256": sha}
    return _register("email", key, payload, path)
