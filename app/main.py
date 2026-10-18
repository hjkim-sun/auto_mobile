from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.api.ingest import router as ingest_router
from app.db.session import init_db
from app.services.ingest import IngestError


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title="Auto Mobile API", lifespan=lifespan)
app.include_router(ingest_router)
app.include_router(health_router)


@app.exception_handler(IngestError)
async def ingest_error_handler(_: Request, exc: IngestError) -> JSONResponse:
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
