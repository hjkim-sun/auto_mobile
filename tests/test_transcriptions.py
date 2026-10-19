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


def test_unknown_job_and_token(client, monkeypatch):
    assert client.get("/v1/transcriptions/999").status_code == 404
    monkeypatch.setenv("INGEST_TOKEN", "secret")
    get_settings.cache_clear()
    assert client.post("/v1/transcriptions", json={"input_path": CALL}).status_code == 401
