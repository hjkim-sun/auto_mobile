from __future__ import annotations

import json
import subprocess

import pytest

from app.core.config import get_settings
from app.db.session import init_db
from app.jobs import agent_mcp
from app.repositories import buzz_state, calendar_items, events
from app.services import agent, agent_tools, calendar

SMS = {"kind": "sms", "id": 1, "from": "01012345678", "date_text": "2026-10-20 09:00:00",
       "text": "홍길동입니다. 내일 3시 회의 가능하세요?", "attachments": []}


@pytest.fixture(autouse=True)
def db():
    init_db()


def test_mcp_lists_and_calls_tools():
    event_id, _ = events.insert_event("message", "message:sms-1", SMS)
    events.finish(event_id, "kept", {"useful": 0.9})
    init = agent_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["capabilities"] == {"tools": {}}
    assert agent_mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = [t["name"] for t in agent_mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
    assert names == [n for n in agent_tools.TOOLS if n not in agent_tools.WRITE]

    def call(name, **arguments):
        return agent_mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                 "params": {"name": name, "arguments": arguments}})["result"]
    assert "회의" in call("search_events", query="회의")["content"][0]["text"]
    assert call("search_events", query="없는말")["content"][0]["text"] == "없음"
    assert "홍길동입니다" in call("get_event", event_id=event_id)["content"][0]["text"]
    assert call("get_event", event_id=999)["isError"]
    assert "없는 도구" in call("decide_calendar", item_id=1, decision="add")["content"][0]["text"]
    assert call("search_events", source="sns")["isError"]
    assert call("status_counts")["content"][0]["text"] == "kept: 1"


def test_session_continues_until_idle(monkeypatch):
    runs = []

    def fake_run(session_id, resume, text, owner=False, research=False):
        runs.append((session_id, resume, owner))
        return {"result": "답", "session_id": session_id}
    monkeypatch.setattr(agent, "_run", fake_run)
    assert agent.reply("room", "안녕", "주인", True) == "답"
    agent.reply("room", "또", "주인", True)
    assert runs[0][2] is True and runs[1][:2] == (runs[0][0], True)
    saved = buzz_state.get("agent_session:room")
    buzz_state.put("agent_session:room", {**saved, "at": saved["at"] - 7 * 3600})
    agent.reply("room", "오랜만", "주인", True)
    assert runs[2][1] is False and runs[2][0] != runs[0][0]


def test_prompt_marks_owner_and_history():
    text = agent.prompt("근거는?", "codex", False, ["- 주인: 약속 정리해 줘"])
    assert "보낸 사람: codex (주인이 아님)" in text and "- 주인: 약속 정리해 줘" in text


def test_mcp_config_runs_server_from_repo(tmp_path):
    server = json.loads(agent.mcp_config())["mcpServers"]["auto_mobile"]
    proc = subprocess.run([server["command"], *server["args"]], cwd=tmp_path, capture_output=True, text=True,
                          input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n', timeout=60)
    assert "search_events" in proc.stdout


def test_brief_runs_once_without_session(monkeypatch):
    event_id, _ = events.insert_event("message", "message:sms-1", SMS)
    events.finish(event_id, "kept", {"useful": 0.9})
    calls = []

    def fake_run(session_id, resume, text, owner=False, research=False):
        assert owner is False and research is True
        calls.append((session_id, text))
        return {"result": " 브리핑 "}
    monkeypatch.setattr(agent, "_run", fake_run)
    assert agent.brief([events.get(event_id)]) == "브리핑"
    session_id, text = calls[0]
    assert session_id is None and f"#{event_id} [문자]" in text and "내일 3시 회의" in text
    assert buzz_state.get("agent_session:room") is None


def call_tool(name, **arguments):
    return agent_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": name, "arguments": arguments}})["result"]


def test_owner_adds_lists_and_removes_calendar(monkeypatch):
    monkeypatch.setenv(agent_tools.AGENT_OWNER_ENV, "1")
    added, removed = [], []
    monkeypatch.setattr(calendar, "add", lambda item: added.append(item["title"]) or "uid-1")
    monkeypatch.setattr(calendar, "remove", removed.append)
    names = [t["name"] for t in agent_mcp.tool_list()]
    assert names == list(agent_tools.TOOLS)
    assert call_tool("decide_calendar", item_id=1, decision="maybe")["isError"]
    assert call_tool("add_calendar", title="치과", start="내일 3시")["isError"]

    out = call_tool("add_calendar", title="치과", start="2099-01-02T15:00", location="강남")["content"][0]["text"]
    assert "캘린더에 넣었습니다" in out and added == ["치과"]
    item = calendar_items.with_status("added")[0]
    assert item["notified"] and item["event_id"] == 0
    assert "치과" in call_tool("upcoming_calendar", days=36500)["content"][0]["text"]
    assert call_tool("upcoming_calendar")["content"][0]["text"] == "일정 없음"

    out = call_tool("remove_calendar", item_id=item["id"])["content"][0]["text"]
    assert "지웠습니다" in out and removed == ["uid-1"]
    assert calendar_items.get(item["id"])["status"] == "removed"
    assert call_tool("remove_calendar", item_id=999)["isError"]


def test_owner_gets_web_and_wiki_write(monkeypatch):
    cmds = []

    def fake_subprocess_run(cmd, **kwargs):
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "답"}), "")
    monkeypatch.setattr(agent.subprocess, "run", fake_subprocess_run)
    # subprocess.run 은 전역이라 위키의 git 호출도 막는다
    monkeypatch.setattr(agent.wiki, "ensure_wiki", lambda: None)
    monkeypatch.setattr(agent.wiki, "commit", lambda message: cmds.append(["commit"]))
    agent._run(None, False, "조사해 줘", owner=True)
    agent._run(None, False, "조사해 줘")
    owner, other = cmds[0], cmds[2]
    assert cmds[1] == ["commit"] and len(cmds) == 3
    assert "WebSearch" in owner[owner.index("--tools") + 1] and "Write" in owner[owner.index("--tools") + 1]
    assert owner[owner.index("--permission-mode") + 1] == "acceptEdits"
    assert other[other.index("--tools") + 1] == "Read,Glob,Grep" and "--permission-mode" not in other
    assert json.loads(other[other.index("--mcp-config") + 1])["mcpServers"]["auto_mobile"]["env"] == {
        agent_tools.AGENT_OWNER_ENV: "0"}
    # 메일 첨부 폴더는 주인 여부와 상관없이 읽게 한다
    attachments = str(get_settings().attachments_dir.resolve())
    assert owner[owner.index("--add-dir") + 1] == attachments == other[other.index("--add-dir") + 1]
