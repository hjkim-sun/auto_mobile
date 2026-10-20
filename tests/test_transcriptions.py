from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.services import transcribe

CALL = "raw/calls/a1b2c3d4e5f6_통화 녹음 홍길동.m4a"


class FakeModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, path, language=None, vad_filter=False):
        self.calls.append((path, language))
        segs = [SimpleNamespace(start=0.0, end=1.5, text=" 여보세요"),
                SimpleNamespace(start=1.5, end=3.0, text=" 홍길동입니다 ")]
        return iter(segs), SimpleNamespace(language=language or "ko", duration=3.0)


@pytest.fixture
def fake_model(monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(transcribe, "_model", model)
    return model


@pytest.fixture
def call_file():
    path = get_settings().data_dir / CALL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake audio")
    return path


def wait_done(client, job_id):
    for _ in range(100):
        job = client.get(f"/v1/transcriptions/{job_id}").json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError("transcription did not finish")


def test_transcribe_writes_txt_and_json(client, fake_model, call_file):
    res = client.post("/v1/transcriptions", json={"input_path": CALL, "output_dir": "transcripts/calls"})
    assert res.status_code == 202
    assert res.json()["status"] in ("queued", "running", "done")

    job = wait_done(client, res.json()["id"])
    assert job["status"] == "done"
    assert job["text_path"] == "transcripts/calls/a1b2c3d4e5f6_통화 녹음 홍길동.txt"
    assert job["text"] == "여보세요\n홍길동입니다\n"
    assert job["duration"] == 3.0
    assert fake_model.calls == [(str(call_file.resolve()), "ko")]

    data_dir = get_settings().data_dir
    meta = json.loads((data_dir / "transcripts/calls/a1b2c3d4e5f6_통화 녹음 홍길동.json").read_text())
    assert meta["segments"][1] == {"start": 1.5, "end": 3.0, "text": "홍길동입니다"}


def test_speakers_are_labeled_by_first_appearance(client, fake_model, call_file, monkeypatch):
    # 화자 분리 모델의 번호(7, 3)와 상관없이 먼저 말한 사람이 화자1
    monkeypatch.setattr(transcribe, "diarize", lambda src: [(0.0, 1.4, 7), (1.3, 3.0, 3)])
    res = client.post("/v1/transcriptions", json={"input_path": CALL, "output_dir": "transcripts/calls"})
    job = wait_done(client, res.json()["id"])
    assert job["text"] == "[화자1] 여보세요\n[화자2] 홍길동입니다\n"

    meta = json.loads((get_settings().data_dir / "transcripts/calls/a1b2c3d4e5f6_통화 녹음 홍길동.json").read_text())
    assert meta["speakers"] == 2
    assert [s["speaker"] for s in meta["segments"]] == [1, 2]


def test_assign_speakers_without_overlap():
    segs = [{"start": 0.0, "end": 1.0, "text": "a"}, {"start": 5.0, "end": 6.0, "text": "b"}]
    transcribe.assign_speakers(segs, [(0.0, 2.0, 0)])
    assert [s["speaker"] for s in segs] == [1, None]
    assert transcribe._line(segs[1]) == "[화자?] b"


def test_default_output_dir_and_auto_language(client, fake_model, call_file):
    res = client.post("/v1/transcriptions", json={"input_path": CALL, "language": "auto"})
    job = wait_done(client, res.json()["id"])
    assert job["output_dir"] == "transcripts" and job["language"] is None
    assert fake_model.calls[0][1] is None


@pytest.mark.parametrize("body,status", [
    ({"input_path": "../.env"}, 400),
    ({"input_path": "/etc/passwd"}, 400),
    ({"input_path": "raw/calls/none.m4a"}, 404),
    ({"input_path": CALL, "output_dir": "../outside"}, 400),
    ({"input_path": CALL, "language": "korean"}, 422),
])
def test_rejects_bad_paths(client, fake_model, call_file, body, status):
    assert client.post("/v1/transcriptions", json=body).status_code == status
    assert fake_model.calls == []


def test_failure_is_recorded(client, monkeypatch, call_file):
    class Broken:
        def transcribe(self, *a, **kw):
            raise RuntimeError("bad audio")

    monkeypatch.setattr(transcribe, "_model", Broken())
    job = wait_done(client, client.post("/v1/transcriptions", json={"input_path": CALL}).json()["id"])
    assert job["status"] == "failed" and job["error"] == "RuntimeError: bad audio"


def test_ingested_call_is_transcribed(client, fake_model):
    res = client.post("/v1/ingest/calls?filename=통화 홍길동_261019_121733.m4a", content=b"fake audio")
    assert res.status_code == 201
    job = wait_done(client, res.json()["transcription_id"])
    assert job["status"] == "done" and job["text"] == "여보세요\n홍길동입니다\n"
    assert job["input_path"].startswith("raw/calls/20261019/") and job["output_dir"] == "transcripts/calls/20261019"
    assert job["text_path"].startswith("transcripts/calls/20261019/")

    # 같은 녹음이 다시 오면 전사를 다시 접수하지 않는다
    again = client.post("/v1/ingest/calls?filename=other.m4a", content=b"fake audio")
    assert again.status_code == 200 and again.json()["transcription_id"] is None
    assert len(fake_model.calls) == 1


def test_unknown_job_and_token(client, monkeypatch):
    assert client.get("/v1/transcriptions/999").status_code == 404
    monkeypatch.setenv("INGEST_TOKEN", "secret")
    get_settings.cache_clear()
    assert client.post("/v1/transcriptions", json={"input_path": CALL}).status_code == 401
