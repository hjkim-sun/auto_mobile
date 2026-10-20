from __future__ import annotations

import json
import subprocess

import pytest

from app.db.session import init_db
from app.jobs import agent_mcp
from app.repositories import buzz_state, events
from app.services import agent, agent_tools

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
    assert names == list(agent_tools.TOOLS)

    def call(name, **arguments):
        return agent_mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                 "params": {"name": name, "arguments": arguments}})["result"]
    assert "회의" in call("search_events", query="회의")["content"][0]["text"]
    assert call("search_events", query="없는말")["content"][0]["text"] == "없음"
    assert "홍길동입니다" in call("get_event", event_id=event_id)["content"][0]["text"]
    assert call("get_event", event_id=999)["isError"]
    assert call("decide_calendar", item_id=1, decision="maybe")["isError"]
    assert call("search_events", source="sns")["isError"]
    assert call("status_counts")["content"][0]["text"] == "kept: 1"


def test_session_continues_until_idle(monkeypatch):
    runs = []

    def fake_run(session_id, resume, text):
        runs.append((session_id, resume))
        return {"result": "답", "session_id": session_id}
    monkeypatch.setattr(agent, "_run", fake_run)
    assert agent.reply("room", "안녕", "주인", True) == "답"
    agent.reply("room", "또", "주인", True)
    assert runs[1] == (runs[0][0], True)
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
