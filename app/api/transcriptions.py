"""음성 전사. 접수하면 202 와 작업 id 를 돌려주고, 결과는 조회로 확인한다.

POST /v1/transcriptions          {"input_path", "output_dir"?, "language"?}  경로는 data_dir 기준
GET  /v1/transcriptions/{id}     status 가 done 이면 text 와 text_path 가 채워진다
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_ingest_token
from app.core.config import get_settings
from app.repositories import transcriptions
from app.schemas.transcriptions import Transcription, TranscriptionRequest
from app.services import transcribe

router = APIRouter(prefix="/v1/transcriptions", tags=["transcriptions"],
                   dependencies=[Depends(require_ingest_token)])


def _out(row: sqlite3.Row) -> Transcription:
    text = None
    if row["status"] == "done" and row["text_path"]:
        path = get_settings().data_dir / row["text_path"]
        text = path.read_text(encoding="utf-8") if path.is_file() else None
    return Transcription(**{k: row[k] for k in row.keys()}, text=text)


@router.post("", response_model=Transcription, status_code=202)
def create_transcription(body: TranscriptionRequest) -> Transcription:
    language = body.language or get_settings().whisper_language
    job_id = transcribe.submit(body.input_path, body.output_dir, None if language == "auto" else language)
    return _out(transcriptions.get(job_id))


@router.get("/{job_id}", response_model=Transcription)
def get_transcription(job_id: int) -> Transcription:
    row = transcriptions.get(job_id)
    if row is None:
        raise HTTPException(404, "transcription not found")
    return _out(row)
