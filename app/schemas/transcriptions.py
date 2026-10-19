from __future__ import annotations

from pydantic import BaseModel, Field


class TranscriptionRequest(BaseModel):
    input_path: str = Field(min_length=1)          # data_dir 기준. 예: raw/calls/a9f0d8065fb3_통화.m4a
    output_dir: str = Field("transcripts", min_length=1)  # data_dir 기준. 없으면 만든다
    # 생략하면 WHISPER_LANGUAGE(기본 ko). "auto" 면 자동 감지
    language: str | None = Field(None, pattern=r"^([a-z]{2,3}|auto)$")


class Transcription(BaseModel):
    id: int
    status: str                  # queued | running | done | failed
    input_path: str
    output_dir: str
    language: str | None = None
    text_path: str | None = None  # done 일 때 data_dir 기준 .txt 경로 (.json 은 같은 이름)
    duration: float | None = None
    text: str | None = None       # done 일 때 전사 결과
    error: str | None = None
    created_at: str
    updated_at: str
