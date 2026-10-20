"""이벤트를 판별 모델에 넘길 텍스트(state)로 바꾼다."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from app.core.config import get_settings
from app.services.email_parse import load_parsed

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
    mail = load_parsed(path)
    # 광고 메일의 추적용 쿼리(?utm_...)가 본문을 차지하지 않게 URL 의 쿼리를 뗀다.
    text = re.sub(r"(https?://[^\s?#)>\]]+)[?#][^\s)>\]]*", r"\1", mail["text"])
    lines = [
        f"[EMAIL] 계정: {payload.get('account')}",
        f"보낸 사람: {mail['from']}",
        f"받는 사람: {mail['to']}",
        f"제목: {mail['subject']}",
        f"날짜: {mail['date_text'] or mail['date']}",
    ]
    if mail["attachments"]:
        lines.append("첨부: " + ", ".join(f"{a['content_type']} {a['filename'] or ''}".strip()
                                          for a in mail["attachments"]))
    return "\n".join([*lines, "", text or "(본문 없음)"])
