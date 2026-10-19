"""음성 파일을 faster-whisper 로 전사해 output_dir 에 {파일명}.txt / {파일명}.json 으로 저장한다.

CPU 에서 실시간의 절반쯤 걸리므로(2코어, 2분 37초 통화 → 81초) 요청은 접수만 하고,
API 프로세스 안의 작업 스레드 1개가 순서대로 처리한다. 모델은 첫 작업 때 한 번 올린다.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.core.config import get_settings
from app.repositories import transcriptions
from app.services.ingest import IngestError

log = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None
_model = None


def resolve_in_data_dir(path: str) -> Path:
    """data_dir 기준 상대 경로(또는 data_dir 안의 절대 경로)만 받는다."""
    root = get_settings().data_dir.resolve()
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise IngestError(400, f"path must be inside data_dir: {path}")
    return resolved


def _relative(path: Path) -> str:
    return str(path.relative_to(get_settings().data_dir.resolve()))


def submit(input_path: str, output_dir: str, language: str | None) -> int:
    src = resolve_in_data_dir(input_path)
    if not src.is_file():
        raise IngestError(404, f"input not found: {input_path}")
    out = resolve_in_data_dir(output_dir)
    if out.exists() and not out.is_dir():
        raise IngestError(400, f"output_dir is not a directory: {output_dir}")
    job = transcriptions.insert(_relative(src), _relative(out), language)
    _enqueue(job["id"])
    return job["id"]


def resume_unfinished() -> None:
    for job_id in transcriptions.unfinished_ids():
        _enqueue(job_id)


def shutdown() -> None:
    global _executor
    if _executor:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None


def _enqueue(job_id: int) -> None:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transcribe")
    _executor.submit(_run, job_id)


def load_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        settings = get_settings()
        _model = WhisperModel(str(settings.whisper_model_dir), device="cpu",
                              compute_type=settings.whisper_compute_type,
                              cpu_threads=settings.whisper_cpu_threads)
    return _model


def _run(job_id: int) -> None:
    job = transcriptions.get(job_id)
    if job is None:
        return
    transcriptions.start(job_id)
    try:
        text_path, duration = transcribe_file(job["input_path"], job["output_dir"], job["language"])
    except Exception as exc:
        log.exception("transcription %s failed", job_id)
        transcriptions.fail(job_id, f"{type(exc).__name__}: {exc}")
    else:
        transcriptions.finish(job_id, text_path, duration)


def transcribe_file(input_path: str, output_dir: str, language: str | None) -> tuple[str, float]:
    """(data_dir 기준 .txt 경로, 음성 길이 초) 를 돌려준다."""
    src = resolve_in_data_dir(input_path)
    out = resolve_in_data_dir(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    segments, info = load_model().transcribe(str(src), language=language, vad_filter=True)
    segments = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
                for s in segments]

    txt = out / f"{src.stem}.txt"
    _write(txt, "\n".join(s["text"] for s in segments) + "\n")
    _write(out / f"{src.stem}.json", json.dumps(
        {"input": _relative(src), "language": info.language, "duration": round(info.duration, 2),
         "segments": segments}, ensure_ascii=False, indent=2))
    return _relative(txt), round(info.duration, 2)


def _write(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
