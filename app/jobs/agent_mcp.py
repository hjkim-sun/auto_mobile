"""Buzz 에이전트용 MCP 서버(stdio). app/services/agent_tools.py 의 도구를 Claude Code 에 내놓는다.

claude 가 --mcp-config 로 띄운다. MCP 의 tools 부분만 구현한다: 줄마다 JSON-RPC 메시지 하나.
실행: python -m app.jobs.agent_mcp
"""
from __future__ import annotations

import json
import logging
import sys

from app.db.session import init_db
from app.services import agent_tools

PROTOCOL_VERSION = "2025-06-18"
logger = logging.getLogger(__name__)


def tool_list() -> list[dict]:
    return [{"name": name, "description": spec["description"],
             "inputSchema": {"type": "object", "properties": spec["properties"],
                             "required": spec.get("required", [])}}
            for name, (_, spec) in agent_tools.TOOLS.items()]


def call_tool(name: str, arguments: dict) -> dict:
    if name not in agent_tools.TOOLS:
        return {"content": [{"type": "text", "text": f"없는 도구: {name}"}], "isError": True}
    func, _ = agent_tools.TOOLS[name]
    try:
        text = func(**arguments)
    except (agent_tools.ToolError, TypeError) as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except Exception as e:
        logger.exception("tool %s failed", name)
        return {"content": [{"type": "text", "text": f"도구 실패: {e!r}"}], "isError": True}
    return {"content": [{"type": "text", "text": text}]}


def handle(msg: dict) -> dict | None:
    """요청 하나에 대한 응답. 알림(id 없음)이면 None."""
    method, params = msg.get("method"), msg.get("params") or {}
    if "id" not in msg:
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "auto_mobile", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": tool_list()}
    elif method == "tools/call":
        result = call_tool(params.get("name", ""), params.get("arguments") or {})
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": msg["id"], "result": result}


def main() -> int:
    # stdout 은 프로토콜 전용이라 로그는 stderr 로 보낸다.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    init_db()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            reply = handle(json.loads(line))
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
