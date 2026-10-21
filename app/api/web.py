"""웹 화면: 위키 그래프와 시나리오 흐름. WEB_TOKEN 이 없으면 모두 404.

GET /web/wiki        위키 그래프 화면 (app/web/wiki.html)
GET /web/flow        시나리오 흐름 화면 (app/web/flow.html)
GET /wiki/graph      그래프 JSON {nodes, links, commits, counts, source_counts}
GET /wiki/page?path= 페이지 마크다운과 이어진 페이지
GET /flow/stats      흐름 화면의 숫자와 최근 기록

브라우저는 /web/flow?token=<WEB_TOKEN> 으로 한 번 열면 쿠키를 받고, 주소에서 토큰을 지운 곳으로 옮겨 간다.
API 는 그 쿠키나 Authorization: Bearer <WEB_TOKEN> 으로 연다.
"""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.core.config import get_settings
from app.services import flow_stats, wiki_graph

PAGES = Path(__file__).resolve().parents[1] / "web"
COOKIE = "am_web"
COOKIE_DAYS = 30

DENIED = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>auto_mobile</title><body style="font-family:system-ui;background:#0b0d12;color:#c3c2b7;padding:40px">
토큰이 필요합니다. 주소 끝에 <code>?token=…</code> 을 붙여 여세요.</body>"""


def _token() -> str:
    token = get_settings().web_token
    if token is None or not token.get_secret_value():
        raise HTTPException(404)
    return token.get_secret_value()


def _given(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(COOKIE, "")


def require_web_token(request: Request) -> None:
    if not secrets.compare_digest(_given(request).encode(), _token().encode()):
        raise HTTPException(401, "invalid token")


router = APIRouter(tags=["web"])
api = APIRouter(dependencies=[Depends(require_web_token)], tags=["web"])


def _page(request: Request, name: str):
    token = _token()
    query = request.query_params.get("token")
    if query is not None:
        if not secrets.compare_digest(query.encode(), token.encode()):
            return HTMLResponse(DENIED, status_code=401)
        response = RedirectResponse(request.url.remove_query_params("token").path, status_code=303)
        response.set_cookie(COOKIE, token, max_age=COOKIE_DAYS * 86400, httponly=True, samesite="lax",
                            secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https")
        return response
    if not secrets.compare_digest(_given(request).encode(), token.encode()):
        return HTMLResponse(DENIED, status_code=401)
    return FileResponse(PAGES / name, headers={"Cache-Control": "no-store"})


@router.get("/web", include_in_schema=False)
def web_home() -> RedirectResponse:
    _token()
    return RedirectResponse("/web/flow", status_code=307)


@router.get("/web/wiki", include_in_schema=False)
def web_wiki(request: Request):
    return _page(request, "wiki.html")


@router.get("/web/flow", include_in_schema=False)
def web_flow(request: Request):
    return _page(request, "flow.html")


@api.get("/wiki/graph")
def get_graph() -> dict:
    return wiki_graph.graph()


@api.get("/wiki/page")
def get_page(path: str) -> dict:
    try:
        return wiki_graph.page(path)
    except wiki_graph.PageNotFound:
        raise HTTPException(404, "page not found") from None


@api.get("/flow/stats")
def get_flow_stats() -> dict:
    return flow_stats.stats()
