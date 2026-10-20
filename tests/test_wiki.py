from __future__ import annotations

import json
import subprocess
import sys

import pytest

from app.core.config import get_settings
from app.db.session import init_db
from app.jobs import wiki_builder
from app.repositories import events, transcriptions, wiki_ingests
from app.services import bot, wiki

SMS = {"kind": "sms", "id": 1, "from": "01012345678", "date_text": "2026-10-20 09:00:00",
       "text": "홍길동입니다. 내일 3시 회의 가능하세요?", "attachments": []}

# 진짜 claude 대신 쓰는 스크립트: 받은 프롬프트를 위키 디렉토리에 남기고 claude -p --output-format json 처럼 답한다.
FAKE_CLAUDE = """#!{python}
import json, pathlib, sys
prompt = sys.stdin.read()
pathlib.Path("prompt.txt").write_text(prompt, encoding="utf-8")
pathlib.Path("args.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
if "FAIL" in prompt:
    pathlib.Path("people/half-written.md").write_text("x", encoding="utf-8")
    print(json.dumps({{"is_error": True, "result": "boom"}}))
    sys.exit(1)
pathlib.Path("sources/written.md").write_text("ok", encoding="utf-8")
print(json.dumps({{"is_error": False, "result": "sources/written.md"}}))
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable))
    script.chmod(0o755)
    monkeypatch.setenv("WIKI_CLAUDE_PATH", str(script))
    get_settings.cache_clear()
    init_db()
    return get_settings().wiki_dir


def kept(source, key, payload, raw_path=None):
    event_id, _ = events.insert_event(source, key, payload, raw_path)
    events.finish(event_id, "kept", {"useful": 0.9})
    return event_id


def git_log(root):
    return subprocess.run(["git", "log", "--format=%s"], cwd=root, capture_output=True, text=True).stdout.split("\n")


def test_ingest_writes_wiki_marks_events_and_commits(fake_claude):
    sms = kept("message", "message:sms-1", SMS)
    dropped, _ = events.insert_event("message", "message:sms-2", {**SMS, "id": 2})
    events.finish(dropped, "discarded", {"useful": 0.1})

    wiki_builder.run(once=True)

    root = fake_claude
    prompt = (root / "prompt.txt").read_text(encoding="utf-8")
    assert f'<source id="{sms}" kind="문자"' in prompt and "내일 3시 회의" in prompt
    args = json.loads((root / "args.json").read_text())
    assert "--restricted" in args and args[args.index("--tools") + 1] == "Read,Write,Edit,Glob,Grep"
    assert (root / "sources" / "written.md").exists()
    assert f"ingest | #{sms}" in (root / "log.md").read_text(encoding="utf-8")
    assert git_log(root)[0] == f"ingest #{sms}"
    assert wiki_ingests.count() == 1

    (root / "prompt.txt").unlink()
    wiki_builder.run(once=True)   # 이미 넣은 것은 다시 넣지 않는다
    assert not (root / "prompt.txt").exists()


def test_call_waits_for_transcript(fake_claude):
    raw = "raw/calls/20261020/abc_통화 홍길동_261020_090000.m4a"
    call = kept("call", "call:abc", {"filename": "통화 홍길동_261020_090000.m4a"}, raw)
    job = transcriptions.insert(raw, "transcripts/calls/20261020", "ko")

    wiki_builder.run(once=True)
    assert wiki_ingests.count() == 0

    text_path = get_settings().data_dir / "transcripts" / "t.txt"
    text_path.parent.mkdir(parents=True)
    text_path.write_text("[화자1] 여보세요 홍길동입니다", encoding="utf-8")
    transcriptions.finish(job["id"], "transcripts/t.txt", 3.0)

    wiki_builder.run(once=True)
    prompt = (fake_claude / "prompt.txt").read_text(encoding="utf-8")
    assert f'<source id="{call}" kind="통화"' in prompt and "[화자1] 여보세요 홍길동입니다" in prompt
    assert wiki_ingests.count() == 1


def test_failed_ingest_rolls_back_and_retries(fake_claude):
    kept("message", "message:sms-1", {**SMS, "text": "FAIL"})

    wiki_builder.run(once=True)

    root = fake_claude
    assert wiki_ingests.count() == 0
    assert not (root / "people" / "half-written.md").exists()
    assert git_log(root)[0] == "init"


def test_bot_wiki_command(fake_claude, monkeypatch):
    asked = []
    monkeypatch.setattr(wiki, "ask", lambda q: asked.append(q) or "답: 내일 3시 (#1)")
    assert bot.handle("위키 홍길동 약속 언제야?") == "답: 내일 3시 (#1)"
    assert asked == ["홍길동 약속 언제야?"]
    assert "위키" in bot.handle("위키")   # 질문이 없으면 도움말
