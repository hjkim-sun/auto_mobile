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
from app.services.render import render

LABELS = {"message": "문자", "email": "메일", "call": "통화"}
WRITE_TOOLS = "Read,Write,Edit,Glob,Grep"
READ_TOOLS = "Read,Glob,Grep"
INDEX = "# 위키 목록\n\n아직 페이지가 없다.\n"
LOG = "# 작업 기록\n"


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


def ingest_prompt(items: list[tuple[sqlite3.Row, str]]) -> str:
    parts = [f"아래 자료 {len(items)}건을 위키에 넣어라. 규칙의 '자료를 받으면 (ingest)' 절차를 따른다.", ""]
    for event, text in items:
        parts += [f'<source id="{event["id"]}" kind="{LABELS.get(event["source"], event["source"])}"'
                  f' received="{_local(event["created_at"])}">', text, "</source>", ""]
    parts.append("다 넣었으면 만들거나 고친 페이지 경로만 한 줄씩 적고 끝낸다.")
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


def run_claude(prompt: str, tools: str) -> str:
    """위키 디렉토리에서 claude -p 를 한 번 돌리고 마지막 답을 돌려준다."""
    s = get_settings()
    cmd = [str(s.wiki_claude_path), "-p", "--restricted", "--safe-mode",
           "--tools", tools, "--permission-mode", "acceptEdits", "--permission-prompts", "none",
           "--no-session-persistence", "--output-format", "json", "--model", s.wiki_model,
           "--append-system-prompt", s.wiki_schema_path.read_text(encoding="utf-8")]
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
    return out.get("result") or ""


def _edit(prompt: str) -> str:
    """위키를 고치는 호출. 실패하면 LLM 이 고치다 만 파일을 마지막 커밋으로 되돌린다."""
    ensure_wiki()
    try:
        return run_claude(prompt, WRITE_TOOLS)
    except Exception:
        _git("reset", "-q", "--hard")
        _git("clean", "-qfd")
        raise


def ingest(items: list[tuple[sqlite3.Row, str]]) -> str:
    """자료를 위키에 넣고 log.md 와 git 에 남긴다."""
    ids = " ".join(f"#{event['id']}" for event, _ in items)
    result = _edit(ingest_prompt(items))
    append_log("ingest", ids)
    commit(f"ingest {ids}")
    return result


def ask(question: str) -> str:
    ensure_wiki()
    return run_claude(f"질문에 답해라. 규칙의 '질문을 받으면 (ask)' 절차를 따른다.\n\n질문: {question}", READ_TOOLS)


def lint() -> str:
    result = _edit("위키를 점검하고 고쳐라. 규칙의 '점검 (lint)' 절차를 따른다. 끝나면 고친 것을 짧게 적는다.")
    lines = result.strip().splitlines()
    append_log("lint", lines[0][:100] if lines else "")
    commit("lint")
    return result
