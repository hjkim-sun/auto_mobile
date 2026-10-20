"""이벤트를 판별 모델에 넘길 텍스트(state)로 바꾼다."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from app.core.config import get_settings
from app.repositories import events, transcriptions
from app.services.email_parse import load_parsed

MAX_STATE_CHARS = 12000
# 통화 이벤트가 생기고 이 시간이 지나도 전사 작업이 없으면 전사 없이 판별한다.
TRANSCRIPT_GRACE_SECONDS = 120


class NotReady(Exception):
    """아직 판별할 수 없다(통화 전사 중). 워커가 나중에 다시 꺼낸다."""


def render(event: sqlite3.Row) -> str | None:
    """판별할 텍스트를 돌려준다. 아직 읽을 텍스트가 없으면 None."""
    payload = json.loads(event["payload"])
    if event["source"] == "message":
        text = render_message(payload)
    elif event["source"] == "email":
        text = render_email(payload, get_settings().data_dir / event["raw_path"])
    else:
        text = render_call(event, payload)
    return text[:MAX_STATE_CHARS] if text else None


def call_transcript(event: sqlite3.Row) -> str | None:
    """전사가 끝났으면 전사문. 전사 중이면 NotReady, 실패했거나 전사 작업이 없으면 None."""
    job = transcriptions.latest_for(event["raw_path"])
    if job is None:
        # API 는 이벤트를 넣은 직후 전사를 접수한다. 그 사이에 꺼냈을 수 있다.
        if event["created_at"] > events.now(-TRANSCRIPT_GRACE_SECONDS):
            raise NotReady("transcription not submitted yet")
        return None
    if job["status"] in ("queued", "running"):
        raise NotReady(f"transcription {job['id']} {job['status']}")
    if job["status"] != "done":
        return None
    return (get_settings().data_dir / job["text_path"]).read_text(encoding="utf-8")


def render_call(event: sqlite3.Row, payload: dict) -> str | None:
    text = call_transcript(event)
    if not text or not text.strip():
        return None
    # 파일명에 상대 이름과 시각이 들어 있다 (예: "통화 홍길동_261020_195821.m4a")
    return "\n".join([f"[통화녹음] 파일: {payload.get('filename')}", "", text])


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
