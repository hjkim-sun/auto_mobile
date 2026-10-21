from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.api.ingest import router as ingest_router
from app.api.transcriptions import router as transcriptions_router
from app.api.verdicts import router as verdicts_router
from app.api.web import api as web_api_router
from app.api.web import router as web_router
from app.db.session import init_db
from app.services import transcribe
from app.services.ingest import IngestError


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    transcribe.resume_unfinished()
    yield
    transcribe.shutdown()


app = FastAPI(title="Auto Mobile API", lifespan=lifespan)
app.include_router(ingest_router)
app.include_router(verdicts_router)
app.include_router(transcriptions_router)
app.include_router(health_router)
app.include_router(web_router)
app.include_router(web_api_router)

# API 문서는 VPS 안에서 localhost:8080 으로 직접 붙을 때만 연다.
# Caddy 를 거친 요청은 항상 X-Forwarded-For 가 붙고, uvicorn 은 127.0.0.1 에서만 받으므로 밖에서 직접 올 수 없다.
DOC_PATHS = {app.docs_url, app.redoc_url, app.openapi_url, app.swagger_ui_oauth2_redirect_url}


@app.middleware("http")
async def local_only_docs(request: Request, call_next):
    if request.url.path in DOC_PATHS and "x-forwarded-for" in request.headers:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return await call_next(request)


@app.exception_handler(IngestError)
async def ingest_error_handler(_: Request, exc: IngestError) -> JSONResponse:
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
