"""이벤트를 판별 모델에 넘길 텍스트(state)로 바꾼다."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from app.core.config import get_settings
from app.repositories import transcriptions
from app.services.email_parse import load_parsed

MAX_STATE_CHARS = 12000


def render(event: sqlite3.Row) -> str | None:
    """판별할 텍스트를 돌려준다. 아직 읽을 텍스트가 없으면 None."""
    payload = json.loads(event["payload"])
    if event["source"] == "message":
        text = render_message(payload)
    elif event["source"] == "kakao":
        text = render_kakao(payload)
    elif event["source"] == "email":
        text = render_email(payload, get_settings().data_dir / event["raw_path"])
    else:
        text = render_call(event, payload)
    return text[:MAX_STATE_CHARS] if text else None


def call_transcript(event: sqlite3.Row) -> str | None:
    """전사가 끝났으면 전사문, 아니면 None. 통화 이벤트는 전사가 끝난 뒤에야 워커에 넘어온다."""
    job = transcriptions.latest_for(event["raw_path"])
    if job is None or job["status"] != "done":
        return None
    return (get_settings().data_dir / job["text_path"]).read_text(encoding="utf-8")


DIRECTIONS = {"incoming": "수신", "outgoing": "발신"}


def call_party(payload: dict) -> str | None:
    """폰이 붙인 통화 정보 한 줄. 예: "수신 홍길동 (01012345678)". 정보가 없으면 None."""
    who = " ".join(filter(None, [payload.get("name"),
                                  f"({payload['number']})" if payload.get("number") else None]))
    parts = [DIRECTIONS.get(payload.get("direction") or ""), who]
    return " ".join(filter(None, parts)) or None


def call_header(payload: dict) -> list[str]:
    # 파일명에도 상대 이름과 시각이 들어 있다 (예: "통화 홍길동_261020_195821.m4a")
    lines = [f"[통화녹음] 파일: {payload.get('filename')}"]
    if party := call_party(payload):
        lines.append(f"통화: {party}")
    if payload.get("call_date_text"):
        lines.append(f"시각: {payload['call_date_text']}")
    return lines


def render_call(event: sqlite3.Row, payload: dict) -> str | None:
    text = call_transcript(event)
    if not text or not text.strip():
        return None
    return "\n".join([*call_header(payload), "", text])


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


def render_kakao(msg: dict) -> str:
    lines = ["[카카오톡]" + (f" 방: {msg['room']}" if msg.get("room") else ""),
             f"보낸 사람: {msg.get('sender')}", f"시각: {msg.get('date_text')}"]
    if msg.get("context"):
        # 앞 메시지. 짧은 답장("네 그때 봬요")도 무슨 말인지 알 수 있게 붙인다
        lines += ["", "앞 대화:", *(f"{c.get('sender')}: {c.get('text')}" for c in msg["context"])]
    return "\n".join([*lines, "", "메시지:", msg.get("text") or ""])


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
