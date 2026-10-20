"""Jev 판별 내역 조회. 최신순.

GET /v1/verdicts/messages?status=&limit=&before_id=
GET /v1/verdicts/emails?status=&limit=&before_id=
"""
from __future__ import annotations

import json
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Query

from app.api.deps import require_ingest_token
from app.core.config import get_settings
from app.repositories import events
from app.schemas.verdicts import EmailVerdict, MessageVerdict, VerdictPage
from app.services.email_parse import load_parsed

router = APIRouter(prefix="/v1/verdicts", tags=["verdicts"], dependencies=[Depends(require_ingest_token)])

Status = Literal["transcribing", "pending", "processing", "kept", "discarded", "failed"]


def _common(row: sqlite3.Row) -> dict:
    verdict = json.loads(row["verdict"]) if row["verdict"] else {}
    relevance = (verdict.get("answers") or {}).get("relevance") or {}
    return {"event_id": row["id"], "status": row["status"], "useful": verdict.get("useful"),
            "choice": relevance.get("choice"), "reason": verdict.get("reason"),
            "model": verdict.get("model"), "error": row["error"],
            "received_at": row["created_at"], "updated_at": row["updated_at"]}


def _page(rows: list[sqlite3.Row], items: list, limit: int) -> dict:
    return {"items": items, "next_before_id": rows[-1]["id"] if len(rows) == limit else None}


@router.get("/messages", response_model=VerdictPage[MessageVerdict])
def message_verdicts(status: Status | None = None, limit: int = Query(50, ge=1, le=500),
                     before_id: int | None = None) -> dict:
    rows = events.list_events("message", status, limit, before_id)
    items = []
    for row in rows:
        msg = json.loads(row["payload"])
        items.append(MessageVerdict(**_common(row), kind=msg.get("kind"), sender=msg.get("from"),
                                    date=msg.get("date_text"), text=msg.get("text"),
                                    attachments=msg.get("attachments") or []))
    return _page(rows, items, limit)


@router.get("/emails", response_model=VerdictPage[EmailVerdict])
def email_verdicts(status: Status | None = None, limit: int = Query(50, ge=1, le=500),
                   before_id: int | None = None) -> dict:
    rows = events.list_events("email", status, limit, before_id)
    items = []
    data_dir = get_settings().data_dir
    for row in rows:
        account = json.loads(row["payload"]).get("account")
        mail = load_parsed(data_dir / row["raw_path"])
        items.append(EmailVerdict(**_common(row), account=account, sender=mail["from"],
                                  subject=mail["subject"], date=mail["date_text"] or mail["date"],
                                  text=mail["text"], attachments=mail["attachments"]))
    return _page(rows, items, limit)
