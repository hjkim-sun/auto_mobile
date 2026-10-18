"""수신 API. 새로 받으면 201, 이미 받은 것이면 200.

POST /v1/ingest/calls?filename=<파일명>        통화녹음 raw body. 중복 기준: sha256
POST /v1/ingest/messages                       SMS/MMS JSON. 중복 기준: kind + id
PUT  /v1/ingest/messages/{message_key}/attachments/{part_id}?filename=<파일명>
                                               MMS 첨부 raw body. message_key 는 "mms-10453" 형식
POST /v1/ingest/emails?account=<naver|gmail>  raw .eml body. 중복 기준: Message-ID (없으면 sha256)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response

from app.api.deps import require_ingest_token
from app.schemas.ingest import IngestResponse
from app.services import ingest

router = APIRouter(prefix="/v1/ingest", tags=["ingest"], dependencies=[Depends(require_ingest_token)])


def _result(response: Response, event_id: int, created: bool) -> IngestResponse:
    response.status_code = 201 if created else 200
    return IngestResponse(event_id=event_id, duplicate=not created)


@router.post("/calls", response_model=IngestResponse, status_code=201)
async def ingest_call(request: Request, response: Response, filename: str | None = None) -> IngestResponse:
    return _result(response, *await ingest.ingest_call(
        request.stream(), filename, request.headers.get("content-type")))


@router.post("/messages", response_model=IngestResponse, status_code=201)
async def ingest_message(request: Request, response: Response) -> IngestResponse:
    return _result(response, *ingest.ingest_message(await request.body()))


@router.put("/messages/{message_key}/attachments/{part_id}", response_model=IngestResponse, status_code=201)
async def ingest_attachment(message_key: str, part_id: str, request: Request, response: Response,
                            filename: str | None = None) -> IngestResponse:
    return _result(response, *await ingest.ingest_attachment(
        request.stream(), message_key, part_id, filename, request.headers.get("content-type")))


@router.post("/emails", response_model=IngestResponse, status_code=201)
async def ingest_email(request: Request, response: Response,
                       account: str = Query(..., pattern=r"^[a-z0-9_.-]+$")) -> IngestResponse:
    return _result(response, *await ingest.ingest_email(request.stream(), account))
