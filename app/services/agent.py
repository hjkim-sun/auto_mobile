"""Buzz 에이전트. 명령이 아닌 말을 VPS 의 Claude Code(헤드리스)에 넘겨 답을 받는다.

claude 는 위키 디렉토리에서 --restricted 로 띄운다. Bash 가 없고, 파일 도구는 위키 안만 읽는다.
원본 이벤트와 캘린더는 MCP 서버(app/jobs/agent_mcp.py)의 도구로만 다룬다. 규칙은 prompts/agent.md.
대화 맥락은 채널마다 claude 세션 하나로 이어 가고, AGENT_SESSION_HOURS 동안 말이 없으면 새 세션을 연다.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from app.core.config import get_settings
from app.repositories import buzz_state
from app.services import wiki

REPO = Path(__file__).resolve().parents[2]
TOOLS = "Read,Glob,Grep"


class AgentError(RuntimeError):
    pass


def mcp_config() -> str:
    # claude 는 MCP 서버를 위키 디렉토리에서 띄우므로, .env 와 data/ 를 찾게 저장소로 옮겨 실행한다.
    script = f"cd {json.dumps(str(REPO))} && exec {json.dumps(sys.executable)} -m app.jobs.agent_mcp"
    return json.dumps({"mcpServers": {"auto_mobile": {"command": "/bin/sh", "args": ["-c", script]}}})


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


def _run(session_id: str, resume: bool, text: str) -> dict:
    s = get_settings()
    rules = (REPO / s.agent_prompt_path).read_text(encoding="utf-8")
    cmd = [str(s.wiki_claude_path), "-p", "--restricted", "--strict-mcp-config", "--mcp-config", mcp_config(),
           "--tools", TOOLS, "--allowedTools", "mcp__auto_mobile", "--permission-prompts", "none",
           "--output-format", "json", "--model", s.agent_model, "--append-system-prompt", rules]
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
    if proc.returncode != 0 or out.get("is_error"):
        raise AgentError(f"claude exit {proc.returncode}: {str(out.get('result'))[-500:]}")
    return out


def reply(channel: str, text: str, sender: str, is_owner: bool, history: list[str] | None = None) -> str:
    session_id, resume = _session(channel)
    message = prompt(text, sender, is_owner, history or [])
    try:
        out = _run(session_id, resume, message)
    except AgentError:
        if not resume:
            raise
        # 세션 파일이 지워졌거나 깨졌으면 새 세션으로 한 번 더 한다.
        session_id = str(uuid.uuid4())
        out = _run(session_id, False, message)
    buzz_state.put(f"agent_session:{channel}", {"id": out.get("session_id") or session_id, "at": time.time()})
    return (out.get("result") or "").strip() or "(답이 비어 있음)"


def reset(channel: str) -> None:
    buzz_state.put(f"agent_session:{channel}", None)
