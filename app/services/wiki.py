"""kept 이벤트를 Claude Code(헤드리스)에 넘겨 data/wiki 의 마크다운 위키로 정리한다.

LLM Wiki 방식: 원본(data/raw)은 그대로 두고, LLM 이 자료 요약과 사람·기관·주제 페이지, index.md 를 쓰고 고친다.
관리 규칙은 prompts/wiki.md (WIKI_SCHEMA_PATH). log.md 는 이 모듈이 적는다.
claude 는 --restricted 로 띄운다. Bash 가 없고 파일 도구는 위키 디렉토리 안만 다룬다.
위키 디렉토리는 git 저장소라 LLM 이 고친 내역을 보고 되돌릴 수 있다.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime

from app.core.config import get_settings
from app.db.session import connect
from app.repositories import calendar_items
from app.services.render import render

LABELS = {"message": "문자", "email": "메일", "call": "통화"}
WRITE_TOOLS = "Read,Write,Edit,Glob,Grep"
READ_TOOLS = "Read,Glob,Grep"
INDEX = "# 위키 목록\n\n아직 페이지가 없다.\n"
LOG = "# 작업 기록\n"
# 자료를 넣으면서 고른 캘린더 일정 후보. 규칙은 prompts/wiki.md 의 "캘린더 일정 판단"
CALENDAR_SCHEMA = {
    "type": "object",
    "properties": {"calendar": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "source_id": {"type": "integer"},
            "title": {"type": "string"},
            "start": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$"},
            "end": {"type": ["string", "null"], "pattern": r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$"},
            "location": {"type": ["string", "null"]},
            "note": {"type": ["string", "null"]},
            "decision": {"enum": ["add", "ask"]},
            "reason": {"type": "string"},
        },
        "required": ["source_id", "title", "start", "decision", "reason"],
    }}},
    "required": ["calendar"],
}


class WikiError(RuntimeError):
    pass


def _local(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(get_settings().tz).strftime("%Y-%m-%d %H:%M")


def source_text(event: sqlite3.Row) -> str | None:
    """위키에 넘길 자료 본문. 통화 전사가 아직 끝나지 않았으면 None (다음에 다시 본다)."""
    if event["source"] != "call":
        return render(event) or "(본문 없음. 이미지만 있는 MMS 등)"
    with closing(connect()) as conn:
        row = conn.execute("SELECT status, text_path FROM transcriptions WHERE input_path = ?"
                           " ORDER BY id DESC LIMIT 1", (event["raw_path"],)).fetchone()
    if row is not None and row["status"] in ("queued", "running"):
        return None
    if row is not None and row["status"] == "done":
        text = (get_settings().data_dir / row["text_path"]).read_text(encoding="utf-8")
    else:
        text = "(전사 없음)"
    filename = json.loads(event["payload"]).get("filename", "")
    # 화자 번호는 처음 말한 사람부터 매기므로 누가 "나"인지 모른다.
    return "\n".join([f"[CALL] 녹음 파일: {filename}",
                      "화자1/화자2 중 누가 위키 주인인지는 표시돼 있지 않다. 내용으로 판단한다.",
                      "", text])


def _calendar_context() -> list[str]:
    today = datetime.now(get_settings().tz).date().isoformat()
    lines = [f"오늘은 {today} 이다. 이미 잡힌 캘린더 후보 (다시 고르지 않는다):"]
    lines += [f"- {r['start']} {r['title']} ({r['status']})" for r in calendar_items.upcoming(today)] or ["- 없음"]
    return [*lines, ""]


def ingest_prompt(items: list[tuple[sqlite3.Row, str]]) -> str:
    parts = [f"아래 자료 {len(items)}건을 위키에 넣어라. 규칙의 '자료를 받으면 (ingest)' 절차를 따르고,"
             " '캘린더 일정 판단'에 따라 고른 일정을 calendar 로 돌려준다.", "", *_calendar_context()]
    for event, text in items:
        parts += [f'<source id="{event["id"]}" kind="{LABELS.get(event["source"], event["source"])}"'
                  f' received="{_local(event["created_at"])}">', text, "</source>", ""]
    return "\n".join(parts)


def ensure_wiki() -> None:
    root = get_settings().wiki_dir
    for sub in ("sources", "people", "orgs", "topics"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    for name, text in (("index.md", INDEX), ("log.md", LOG), ("schedule.md", "# 일정과 할 일\n")):
        if not (root / name).exists():
            (root / name).write_text(text, encoding="utf-8")
    if not (root / ".git").exists():
        _git("init", "-q")
        _git("config", "user.name", "auto_mobile wiki")
        _git("config", "user.email", "wiki@localhost")
        commit("init")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=get_settings().wiki_dir, capture_output=True, text=True,
                          check=True)


def commit(message: str) -> None:
    _git("add", "-A")
    if _git("status", "--porcelain").stdout.strip():
        _git("commit", "-q", "-m", message)


def append_log(op: str, detail: str) -> None:
    now = datetime.now(get_settings().tz).strftime("%Y-%m-%d %H:%M")
    with open(get_settings().wiki_dir / "log.md", "a", encoding="utf-8") as f:
        f.write(f"\n## [{now}] {op} | {detail}\n")


def run_claude(prompt: str, tools: str, schema: dict | None = None) -> dict:
    """위키 디렉토리에서 claude -p 를 한 번 돌린다. result 는 마지막 답, schema 를 주면 structured_output 도 있다."""
    s = get_settings()
    cmd = [str(s.wiki_claude_path), "-p", "--restricted", "--safe-mode",
           "--tools", tools, "--permission-mode", "acceptEdits", "--permission-prompts", "none",
           "--no-session-persistence", "--output-format", "json", "--model", s.wiki_model,
           "--append-system-prompt", s.wiki_schema_path.read_text(encoding="utf-8")]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    try:
        proc = subprocess.run(cmd, input=prompt, cwd=s.wiki_dir, capture_output=True, text=True,
                              timeout=s.wiki_timeout_seconds)
    except subprocess.TimeoutExpired as e:
        raise WikiError(f"claude timed out after {s.wiki_timeout_seconds}s") from e
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise WikiError(f"claude exit {proc.returncode}: {(proc.stderr or proc.stdout)[-500:]}") from None
    if proc.returncode != 0 or out.get("is_error"):
        raise WikiError(f"claude exit {proc.returncode}: {str(out.get('result'))[-500:]}")
    if schema and not isinstance(out.get("structured_output"), dict):
        raise WikiError("claude returned no structured output")
    return out


def _edit(prompt: str, schema: dict | None = None) -> dict:
    """위키를 고치는 호출. 실패하면 LLM 이 고치다 만 파일을 마지막 커밋으로 되돌린다."""
    ensure_wiki()
    try:
        return run_claude(prompt, WRITE_TOOLS, schema)
    except Exception:
        _git("reset", "-q", "--hard")
        _git("clean", "-qfd")
        raise


def ingest(items: list[tuple[sqlite3.Row, str]]) -> list[dict]:
    """자료를 위키에 넣고 log.md 와 git 에 남긴다. 고른 캘린더 일정 후보를 돌려준다."""
    ids = " ".join(f"#{event['id']}" for event, _ in items)
    out = _edit(ingest_prompt(items), CALENDAR_SCHEMA)
    append_log("ingest", ids)
    commit(f"ingest {ids}")
    return out["structured_output"]["calendar"]


def plan_calendar() -> list[dict]:
    """이미 위키에 있는 일정(schedule.md)에서 캘린더 후보를 고른다. 캘린더 연동 전에 넣은 자료용."""
    ensure_wiki()
    prompt = "\n".join([
        "schedule.md 의 '예정' 일정과 관련 sources/ 페이지를 읽고, 규칙의 '캘린더 일정 판단'에 따라"
        " 캘린더에 넣을 일정을 calendar 로 돌려준다. source_id 는 일정에 붙은 자료 번호다. 파일은 고치지 않는다.",
        "", *_calendar_context()])
    return run_claude(prompt, READ_TOOLS, CALENDAR_SCHEMA)["structured_output"]["calendar"]


def ask(question: str) -> str:
    ensure_wiki()
    return run_claude(f"질문에 답해라. 규칙의 '질문을 받으면 (ask)' 절차를 따른다.\n\n질문: {question}",
                      READ_TOOLS).get("result") or ""


def lint() -> str:
    result = _edit("위키를 점검하고 고쳐라. 규칙의 '점검 (lint)' 절차를 따른다. 끝나면 고친 것을 짧게 적는다.").get("result") or ""
    lines = result.strip().splitlines()
    append_log("lint", lines[0][:100] if lines else "")
    commit("lint")
    return result
