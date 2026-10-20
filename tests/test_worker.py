from __future__ import annotations

import json
from contextlib import closing

import httpx
import pytest

from app.core.config import get_settings
from app.db.session import connect
from app.jobs.worker import run
from app.services import jev

AD = {"kind": "mms", "id": 1, "from": "01087654321", "date": 1792247679, "text": "(광고) 분양 마감 이벤트"}
PERSONAL = {"kind": "sms", "id": 2, "from": "01012345678", "date": 1792247764000, "text": "안녕하세요 홍길동입니다"}
IMAGE_ONLY = {"kind": "mms", "id": 3, "from": "01012345678", "date": 1792247679,
              "parts": [{"_id": 1, "ct": "image/jpeg", "cl": "a.jpeg"}]}


def jev_answer(useful):
    return {"model": "jev-1.13.0",
            "answers": {"relevance": {"type": "choice", "choice": "useful" if useful >= 0.5 else "noise",
                                      "probabilities": {"useful": useful, "noise": 1 - useful}}},
            "usage": {"input_tokens": 10, "output_tokens": 1}}


def rows():
    with closing(connect()) as conn:
        return {r["dedup_key"]: r for r in conn.execute("SELECT * FROM events")}


@pytest.fixture
def fake_jev(monkeypatch):
    states = []

    def classify(state, questions):
        states.append(state)
        assert "relevance" in questions
        return jev_answer(0.0 if "광고" in state else 0.9)

    monkeypatch.setattr(jev, "classify", classify)
    return states


def test_worker_keeps_useful_and_discards_noise(client, fake_jev):
    for msg in (AD, PERSONAL, IMAGE_ONLY):
        client.post("/v1/ingest/messages", json=msg)
    run(once=True)

    result = rows()
    assert result["message:mms-1"]["status"] == "discarded"
    assert result["message:sms-2"]["status"] == "kept"
    assert json.loads(result["message:sms-2"]["verdict"])["useful"] == 0.9
    # 텍스트가 없으면 Jev 를 부르지 않고 남긴다.
    assert result["message:mms-3"]["status"] == "kept"
    assert json.loads(result["message:mms-3"]["verdict"]) == {"reason": "no_text"}
    assert len(fake_jev) == 2


def fail(state, questions):
    raise httpx.ConnectError("down")


def test_worker_retries_on_jev_error(client, monkeypatch):
    monkeypatch.setattr(jev, "classify", fail)
    client.post("/v1/ingest/messages", json=PERSONAL)
    run(once=True)

    row = rows()["message:sms-2"]
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert "ConnectError" in row["error"]


def test_worker_marks_failed_after_max_attempts(client, monkeypatch):
    monkeypatch.setenv("WORKER_MAX_ATTEMPTS", "1")
    get_settings.cache_clear()
    monkeypatch.setattr(jev, "classify", fail)
    client.post("/v1/ingest/messages", json=PERSONAL)
    run(once=True)

    assert rows()["message:sms-2"]["status"] == "failed"


def write_prompt(name, body):
    path = get_settings().prompts_dir / f"{name}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


CUSTOM = """
keep_question = "keep"
keep_choice = "yes"
threshold = 0.95

[questions.keep]
type = "choice"
instructions = "남길까?"
criteria = { yes = "남긴다", no = "버린다" }
"""


def test_prompt_is_loaded_per_source(client, monkeypatch, tmp_path):
    monkeypatch.setenv("PROMPTS_DIR", str(tmp_path / "prompts"))
    get_settings.cache_clear()
    write_prompt("message", CUSTOM)
    sent = []

    def classify(state, questions):
        sent.append(questions)
        return {"answers": {"keep": {"probabilities": {"yes": 0.9, "no": 0.1}}}}

    monkeypatch.setattr(jev, "classify", classify)
    client.post("/v1/ingest/messages", json=PERSONAL)
    run(once=True)

    row = rows()["message:sms-2"]
    assert sent == [{"keep": {"type": "choice", "instructions": "남길까?",
                              "criteria": {"yes": "남긴다", "no": "버린다"}}}]
    # 0.9 < 파일의 threshold 0.95 → JEV_KEEP_THRESHOLD(0.3) 대신 파일 값을 쓴다
    assert row["status"] == "discarded"
    assert json.loads(row["verdict"])["prompt"] == "message"


@pytest.mark.parametrize("body", [None, "keep_question = 'x'\nkeep_choice = 'y'\n", CUSTOM.replace('"yes"', '"maybe"', 1)])
def test_bad_or_missing_prompt_is_retried(client, monkeypatch, tmp_path, body):
    monkeypatch.setenv("PROMPTS_DIR", str(tmp_path / "prompts"))
    get_settings.cache_clear()
    if body is not None:
        write_prompt("message", body)
    monkeypatch.setattr(jev, "classify", lambda *a: pytest.fail("jev must not be called"))
    client.post("/v1/ingest/messages", json=PERSONAL)
    run(once=True)

    row = rows()["message:sms-2"]
    assert row["status"] == "pending" and "PromptError" in row["error"]


def test_bundled_prompts_are_valid():
    from app.services import prompts

    for name in ("message", "email", "call"):
        assert prompts.load(name).keep_question == "relevance"



CALL_FILE = "통화 홍길동_261020_195821.m4a"
CALL_RAW = f"raw/calls/20261020/abc_{CALL_FILE}"


def add_call():
    from app.repositories import events, transcriptions

    events.insert_event("call", "call:abc", {"filename": CALL_FILE}, CALL_RAW, status="transcribing")
    return transcriptions.insert(CALL_RAW, "transcripts/calls/20261020", None)["id"]


def test_call_is_classified_from_transcript_after_it_is_done(client, fake_jev):
    from app.repositories import events, transcriptions

    job_id = add_call()
    run(once=True)
    # 전사 중인 통화는 워커가 꺼내지 않는다
    assert rows()["call:abc"]["status"] == "transcribing"
    assert fake_jev == []

    out = get_settings().data_dir / "transcripts/calls/20261020/abc.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("[화자1] 내일 3시에 뵙겠습니다", encoding="utf-8")
    transcriptions.finish(job_id, "transcripts/calls/20261020/abc.txt", 7.0)
    events.release_transcribed(CALL_RAW)
    run(once=True)

    row = rows()["call:abc"]
    assert row["status"] == "kept" and row["attempts"] == 1
    assert json.loads(row["verdict"])["prompt"] == "call"
    assert CALL_FILE in fake_jev[0] and "내일 3시에" in fake_jev[0]


def test_failed_transcription_releases_call_without_text(client, fake_jev, monkeypatch):
    from app.services import transcribe

    job_id = add_call()
    # conftest 의 no_whisper 때문에 전사는 실패한다
    monkeypatch.setattr(transcribe, "resolve_in_data_dir", lambda p: get_settings().data_dir / p)
    transcribe._run(job_id)
    assert rows()["call:abc"]["status"] == "pending"
    run(once=True)

    row = rows()["call:abc"]
    assert row["status"] == "kept" and json.loads(row["verdict"]) == {"reason": "no_text"}
    assert fake_jev == []


def test_call_without_transcription_job_is_released_on_api_start(client, fake_jev):
    from app.repositories import events
    from app.services import transcribe

    events.insert_event("call", "call:abc", {"filename": CALL_FILE}, CALL_RAW, status="transcribing")
    transcribe.resume_unfinished()
    assert rows()["call:abc"]["status"] == "pending"


def test_ingested_call_waits_for_transcription_thread(client, fake_jev):
    import time

    from app.repositories import events

    event_id = client.post("/v1/ingest/calls?filename=call_261020_195821.m4a", content=b"audio").json()["event_id"]
    assert events.get(event_id)["status"] in ("transcribing", "pending")
    for _ in range(50):   # 테스트의 전사는 곧바로 실패하고, 실패해도 대기열에 넣는다
        if events.get(event_id)["status"] == "pending":
            break
        time.sleep(0.1)
    assert events.get(event_id)["status"] == "pending"
