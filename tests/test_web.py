from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.db.session import init_db
from app.repositories import agent_turns, buzz_state, calendar_items, events, wiki_ingests
from app.services import wiki

SMS = {"kind": "sms", "id": 1, "from": "01012345678", "date_text": "2026-10-20 09:00:00",
       "text": "홍길동입니다. 내일 3시 회의 가능하세요?", "attachments": []}
AUTH = {"Authorization": "Bearer web-secret"}


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("WEB_TOKEN", "web-secret")
    get_settings.cache_clear()
    init_db()


def write(path: str, text: str) -> None:
    target = get_settings().wiki_dir / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


@pytest.fixture
def wiki_pages(web):
    event_id, _ = events.insert_event("message", "sms-1", SMS)
    events.finish(event_id, "kept", {"useful": 0.9})
    wiki.ensure_wiki()
    write(f"sources/{event_id}.md", f"# #{event_id} 문자 홍길동\n\n회의 요청.\n\n## 관련\n- [홍길동](../people/홍길동.md)\n")
    write("people/홍길동.md", "# 홍길동\n\n거래처 담당자.\n\n## 주고받은 일\n"
          f"- 회의 요청 ([#{event_id}](../sources/{event_id}.md)), [회의 준비](../topics/회의-준비.md)\n")
    wiki.commit("ingest #1")
    write("topics/회의-준비.md", "# 회의 준비\n\n[홍길동](../people/%ED%99%8D%EA%B8%B8%EB%8F%99.md) 과 회의. [밖](https://example.com/a.md)\n")
    wiki.commit("ingest #2")
    return event_id


def test_web_is_closed_without_token(client, monkeypatch):
    monkeypatch.setenv("WEB_TOKEN", "")
    get_settings.cache_clear()
    assert client.get("/web/flow").status_code == 404
    assert client.get("/wiki/graph", headers=AUTH).status_code == 404


def test_token_in_url_becomes_cookie(client, web):
    assert client.get("/wiki/graph").status_code == 401
    assert client.get("/web/wiki").status_code == 401
    assert client.get("/web/wiki?token=wrong", follow_redirects=False).status_code == 401
    r = client.get("/web/wiki?token=web-secret", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/web/wiki"
    assert "httponly" in r.headers["set-cookie"].lower()
    assert client.get("/web/wiki").status_code == 200
    assert client.get("/wiki/graph").status_code == 200


def test_graph_links_pages_and_tracks_when_they_appeared(client, wiki_pages):
    g = client.get("/wiki/graph", headers=AUTH).json()
    nodes = {n["id"]: n for n in g["nodes"]}
    # ensure_wiki 가 만든 index.md, log.md 는 빼고 schedule.md 는 넣는다
    assert set(nodes) == {f"sources/{wiki_pages}.md", "people/홍길동.md", "topics/회의-준비.md", "schedule.md"}
    assert nodes[f"sources/{wiki_pages}.md"]["source"] == "message"
    assert nodes["people/홍길동.md"]["type"] == "person" and nodes["people/홍길동.md"]["degree"] == 2
    assert nodes["people/홍길동.md"]["summary"] == "거래처 담당자."
    pairs = {tuple(sorted((link["source"], link["target"]))) for link in g["links"]}
    assert pairs == {("people/홍길동.md", f"sources/{wiki_pages}.md"), ("people/홍길동.md", "topics/회의-준비.md")}
    assert [c["subject"] for c in g["commits"]][-2:] == ["ingest #1", "ingest #2"]
    assert all(n["added_at"] for n in g["nodes"])
    assert g["counts"] == {"source": 1, "person": 1, "topic": 1, "schedule": 1} and g["source_counts"] == {"message": 1}


def test_page_returns_markdown_and_rejects_paths_outside_wiki(client, wiki_pages):
    page = client.get("/wiki/page", params={"path": "people/홍길동.md"}, headers=AUTH).json()
    assert page["title"] == "홍길동" and page["markdown"].startswith("# 홍길동")
    assert {p["id"] for p in page["linked"]} == {f"sources/{wiki_pages}.md", "topics/회의-준비.md"}
    for path in ("../events.db", "/etc/passwd", ".git/config", "people/없음.md"):
        assert client.get("/wiki/page", params={"path": path}, headers=AUTH).status_code == 404


def test_mask_file_replaces_names_on_screen_only(client, wiki_pages):
    write("people/홍길동.md", "# 홍길동\n\n연락처 01012345678. 결제 11,000원, 1,000원.\n")
    mask = get_settings().wiki_mask_path
    mask.write_text('{"pairs": [["홍길동", "김철수"], ["01012345678", "01000000000"], ["1,000원", "○원"]]}',
                    encoding="utf-8")
    g = client.get("/wiki/graph", headers=AUTH).json()
    assert "people/김철수.md" in {n["id"] for n in g["nodes"]}
    assert f"#{wiki_pages} 문자 김철수" in {n["title"] for n in g["nodes"]}
    page = client.get("/wiki/page", params={"path": "people/김철수.md"}, headers=AUTH).json()
    assert page["path"] == "people/김철수.md"
    assert page["markdown"] == "# 김철수\n\n연락처 01000000000. 결제 11,000원, ○원.\n"
    # 파일은 그대로 두고, 매핑 파일을 치우면 원래대로 보인다
    assert (get_settings().wiki_dir / "people/홍길동.md").read_text(encoding="utf-8").startswith("# 홍길동")
    mask.unlink()
    assert "people/홍길동.md" in {n["id"] for n in client.get("/wiki/graph", headers=AUTH).json()["nodes"]}


def test_flow_stats_follow_an_event_through_each_step(client, wiki_pages):
    noise, _ = events.insert_event("message", "sms-2", {**SMS, "id": 2, "text": "광고"})
    events.finish(noise, "discarded", {"useful": 0.05})
    wiki_ingests.mark([wiki_pages])
    buzz_state.mark_notified(wiki_pages)
    calendar_items.insert({"event_id": wiki_pages, "title": "홍길동 회의", "start": "2026-10-21T15:00",
                           "decision": "ask", "reason": "참석 여부 미정"})
    agent_turns.record("dm-1", True, "내일 일정 뭐야?", "15시 홍길동 회의 (#1)", True, events.now())

    s = client.get("/flow/stats", headers=AUTH).json()
    c = s["counts"]
    assert c["sources"] == {"message": {"kept": 1, "discarded": 1}}
    assert (c["kept"], c["discarded"], c["wiki_ingested"], c["notified"], c["agent_turns"]) == (1, 1, 1, 1, 1)
    assert c["calendar"] == {"asking": 1} and c["calendar_asked"] == 1 and c["wiki_links"] == 2
    kept, dropped = s["recent"][1], s["recent"][0]
    assert kept["title"] == f"#{wiki_pages} 문자 홍길동" and kept["useful"] == 0.9   # 위키 자료 페이지 제목을 쓴다
    assert kept["wiki_at"] and kept["notified_at"] and kept["calendar"][0]["status"] == "asking"
    assert dropped["status"] == "discarded" and dropped["title"] == "01012345678"
    assert s["turns"][0]["question"] == "내일 일정 뭐야?" and s["turns"][0]["ok"] is True
