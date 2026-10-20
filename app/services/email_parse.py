"""원본 .eml 을 사람이 읽을 수 있는 dict 로 푼다. 수신 시 .eml 옆에 같은 이름의 .json 으로 저장한다."""
from __future__ import annotations

import json
import re
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path


def parse_email(path: Path) -> dict:
    with path.open("rb") as f:
        mail = BytesParser(policy=policy.default).parse(f)
    return {
        "message_id": str(mail.get("Message-ID") or "").strip() or None,
        "from": str(mail.get("From") or ""),
        "to": str(mail.get("To") or ""),
        "cc": str(mail.get("Cc") or ""),
        "subject": str(mail.get("Subject") or ""),
        "date": str(mail.get("Date") or ""),
        "date_text": _date_text(mail.get("Date")),
        "text": body_text(mail),
        "attachments": [
            {"filename": part.get_filename(), "content_type": part.get_content_type(),
             "size": len(part.get_payload(decode=True) or b"")}
            for part in mail.iter_attachments()
        ],
    }


def parsed_path(eml_path: Path) -> Path:
    return eml_path.with_suffix(".json")


def write_parsed(eml_path: Path) -> dict:
    parsed = parse_email(eml_path)
    parsed_path(eml_path).write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    return parsed


def load_parsed(eml_path: Path) -> dict:
    """저장된 .json 을 읽는다. 파싱 기능 이전에 받은 메일이면 그 자리에서 만든다."""
    path = parsed_path(eml_path)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return write_parsed(eml_path)


def body_text(mail: EmailMessage) -> str:
    body = mail.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    try:
        text = body.get_content()
    except (LookupError, ValueError):   # 알 수 없는 charset
        text = (body.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
    # 네이버 메일은 text/plain 에도 HTML 주석이 섞여 온다.
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    if body.get_content_type() == "text/html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", text)
        text = unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _date_text(value: object) -> str | None:
    try:
        return parsedate_to_datetime(str(value)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
