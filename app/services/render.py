"""이벤트를 판별 모델에 넘길 텍스트(state)로 바꾼다."""
from __future__ import annotations

import json
import re
import sqlite3
from email import policy
from email.parser import BytesParser
from html import unescape
from pathlib import Path

from app.core.config import get_settings

MAX_STATE_CHARS = 12000


def render(event: sqlite3.Row) -> str | None:
    """판별할 텍스트를 돌려준다. 아직 읽을 텍스트가 없으면 None."""
    payload = json.loads(event["payload"])
    if event["source"] == "message":
        text = render_message(payload)
    elif event["source"] == "email":
        text = render_email(payload, get_settings().data_dir / event["raw_path"])
    else:
        # 통화녹음은 STT 단계가 생기기 전까지 판별할 텍스트가 없다.
        text = None
    return text[:MAX_STATE_CHARS] if text else None


def render_message(msg: dict) -> str | None:
    if not msg.get("text"):
        # 이미지만 있는 MMS 는 OCR/비전 단계가 생기기 전까지 판별할 텍스트가 없다.
        return None
    lines = [f"[{str(msg.get('kind', '')).upper()}] 발신: {msg.get('from')}",
             f"시각: {msg.get('date_text')}"]
    if msg.get("attachments"):
        lines.append("첨부: " + ", ".join(f"{a.get('ct')} {a.get('name') or ''}".strip()
                                          for a in msg["attachments"]))
    lines += ["", msg["text"]]
    return "\n".join(lines)


def render_email(payload: dict, path: Path) -> str:
    with path.open("rb") as f:
        mail = BytesParser(policy=policy.default).parse(f)
    body = mail.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body else ""
    if body and body.get_content_type() == "text/html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()
    return "\n".join([
        f"[EMAIL] 계정: {payload.get('account')}",
        f"보낸 사람: {payload.get('from')}",
        f"받는 사람: {payload.get('to')}",
        f"제목: {payload.get('subject')}",
        f"날짜: {payload.get('date')}",
        "",
        text or "(본문 없음)",
    ])
