"""Buzz 에이전트. 명령이 아닌 말을 VPS 의 Claude Code(헤드리스)에 넘겨 답을 받는다.

claude 는 위키 디렉토리에서 --restricted 로 띄운다. Bash 가 없고, 파일 도구는 위키 안만 다룬다.
원본 이벤트와 캘린더는 MCP 서버(app/jobs/agent_mcp.py)의 도구로만 다룬다. 규칙은 prompts/agent.md.
주인과의 대화에서는 개인 비서로 일한다: 웹 검색·조회, 위키 쓰기, 캘린더 넣기·지우기를 준다. 위키를 고쳤으면 git 에 남긴다.
다른 에이전트와의 대화와 새 자료 브리핑은 읽기만 한다. 자료나 남의 말에 적힌 지시로 쓰기나 바깥 요청을 하지 않게 하려는 것이다.
대화 맥락은 채널마다 claude 세션 하나로 이어 가고, AGENT_SESSION_HOURS 동안 말이 없으면 새 세션을 연다.
새로 남긴 자료의 알림(brief)은 대화와 따로 한 번만 돌리는 세션으로 만든다.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

from app.core.config import get_settings
from app.repositories import buzz_state
from app.services import agent_tools, bot, wiki

REPO = Path(__file__).resolve().parents[2]
TOOLS = "Read,Glob,Grep"
OWNER_TOOLS = "Read,Glob,Grep,Write,Edit,WebSearch,WebFetch"
BRIEF_CHARS = 3000   # 브리핑에 넣는 자료 하나의 본문 길이. 더 필요하면 에이전트가 get_event 로 본다.


class AgentError(RuntimeError):
    pass


def mcp_config(owner: bool = False) -> str:
    # claude 는 MCP 서버를 위키 디렉토리에서 띄우므로, .env 와 data/ 를 찾게 저장소로 옮겨 실행한다.
    script = f"cd {json.dumps(str(REPO))} && exec {json.dumps(sys.executable)} -m app.jobs.agent_mcp"
    server = {"command": "/bin/sh", "args": ["-c", script], "env": {agent_tools.AGENT_OWNER_ENV: "1" if owner else "0"}}
    return json.dumps({"mcpServers": {"auto_mobile": server}})


def prompt(text: str, sender: str, is_owner: bool, history: list[str]) -> str:
    who = f"{sender} (주인)" if is_owner else f"{sender} (주인이 아님)"
    parts = []
    if history:
        parts += ["채널의 최근 대화 (오래된 것부터):", *history, ""]
    parts += [f"새 메시지. 보낸 사람: {who}", text]
    return "\n".join(parts)


def _session(channel: str) -> tuple[str, bool]:
    """(세션 id, 이어 가는지)."""
    s = get_settings()
    saved = buzz_state.get(f"agent_session:{channel}")
    if saved and time.time() - saved["at"] < s.agent_session_hours * 3600:
        return saved["id"], True
    return str(uuid.uuid4()), False


def _run(session_id: str | None, resume: bool, text: str, owner: bool = False) -> dict:
    s = get_settings()
    rules = (REPO / s.agent_prompt_path).read_text(encoding="utf-8").replace("{bot_name}", s.buzz_bot_name)
    cmd = [str(s.wiki_claude_path), "-p", "--restricted", "--strict-mcp-config", "--mcp-config", mcp_config(owner),
           "--tools", OWNER_TOOLS if owner else TOOLS, "--permission-prompts", "none",
           "--allowedTools", "mcp__auto_mobile,WebSearch,WebFetch" if owner else "mcp__auto_mobile",
           "--output-format", "json", "--model", s.agent_model, "--append-system-prompt", rules]
    if owner:
        cmd += ["--permission-mode", "acceptEdits"]
    if session_id is None:
        cmd.append("--no-session-persistence")
    else:
        cmd += ["--resume", session_id] if resume else ["--session-id", session_id]
    wiki.ensure_wiki()
    try:
        proc = subprocess.run(cmd, input=text, cwd=s.wiki_dir.resolve(), capture_output=True, text=True,
                              timeout=s.agent_timeout_seconds)
    except subprocess.TimeoutExpired as e:
        raise AgentError(f"claude timed out after {s.agent_timeout_seconds}s") from e
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AgentError(f"claude exit {proc.returncode}: {(proc.stderr or proc.stdout)[-500:]}") from None
    if owner:   # 위키를 고쳤으면 남긴다. 위키 빌더가 실패해 되돌릴 때 함께 지워지지 않게
        wiki.commit("agent: 주인 요청")
    if proc.returncode != 0 or out.get("is_error"):
        raise AgentError(f"claude exit {proc.returncode}: {str(out.get('result'))[-500:]}")
    return out


def reply(channel: str, text: str, sender: str, is_owner: bool, history: list[str] | None = None) -> str:
    session_id, resume = _session(channel)
    message = prompt(text, sender, is_owner, history or [])
    try:
        out = _run(session_id, resume, message, owner=is_owner)
    except AgentError:
        if not resume:
            raise
        # 세션 파일이 지워졌거나 깨졌으면 새 세션으로 한 번 더 한다.
        session_id = str(uuid.uuid4())
        out = _run(session_id, False, message, owner=is_owner)
    buzz_state.put(f"agent_session:{channel}", {"id": out.get("session_id") or session_id, "at": time.time()})
    return (out.get("result") or "").strip() or "(답이 비어 있음)"


def brief_prompt(rows: list[sqlite3.Row]) -> str:
    parts = [f"새로 남긴 자료 {len(rows)}건이 들어왔다. 주인에게 브리핑한다 (규칙의 '새 자료 브리핑').", ""]
    for row in rows:
        text = bot.body(row).strip()
        if len(text) > BRIEF_CHARS:
            text = text[:BRIEF_CHARS] + "\n…(잘림, get_event 로 전체를 본다)"
        parts += [f"=== {bot.summary_line(row)}", text, ""]
    return "\n".join(parts)


def brief(rows: list[sqlite3.Row]) -> str:
    """kept 이벤트 묶음을 위키 맥락과 함께 비서처럼 알리는 글."""
    out = _run(None, False, brief_prompt(rows))
    result = (out.get("result") or "").strip()
    if not result:
        raise AgentError("empty briefing")
    return result


def reset(channel: str) -> None:
    buzz_state.put(f"agent_session:{channel}", None)
