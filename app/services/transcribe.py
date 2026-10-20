"""음성 파일을 faster-whisper 로 전사해 output_dir 에 {파일명}.txt / {파일명}.json 으로 저장한다.

CPU 에서 실시간의 절반쯤 걸리므로(2코어, 2분 37초 통화 → 81초) 요청은 접수만 하고,
API 프로세스 안의 작업 스레드 1개가 순서대로 처리한다. 모델은 첫 작업 때 한 번 올린다.

sherpa-onnx 로 화자를 나눠(2분 통화에 몇 초) 구간마다 화자 번호를 붙인다. 화자 분리가 꺼져 있거나
실패하면 화자 없이 저장한다. 누가 "나"인지는 알 수 없어서 처음 말한 사람부터 화자1, 화자2 로 붙인다.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.core.config import get_settings
from app.repositories import events, transcriptions
from app.services.ingest import IngestError

log = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None
_model = None
_diarizer = None


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
    events.release_orphaned_transcribing()
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


def load_diarizer():
    global _diarizer
    if _diarizer is None:
        import sherpa_onnx

        settings = get_settings()
        n, threads = settings.diarization_num_speakers, settings.whisper_cpu_threads
        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(settings.diarization_segmentation_model)),
                num_threads=threads),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(settings.diarization_embedding_model), num_threads=threads),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=n if n > 0 else -1, threshold=settings.diarization_threshold),
            min_duration_on=0.3, min_duration_off=0.5)
        if not config.validate():
            raise RuntimeError("invalid diarization config (model files missing?)")
        _diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
    return _diarizer


def diarize(src: Path) -> list[tuple[float, float, int]] | None:
    """[(시작, 끝, 화자 번호)] 를 돌려준다. 꺼져 있거나 실패하면 None."""
    if not get_settings().diarization_enabled:
        return None
    try:
        from faster_whisper.audio import decode_audio

        diarizer = load_diarizer()
        result = diarizer.process(decode_audio(str(src), sampling_rate=diarizer.sample_rate))
        return [(t.start, t.end, t.speaker) for t in result.sort_by_start_time()]
    except Exception:
        log.exception("diarization failed for %s", src)
        return None


def assign_speakers(segments: list[dict], turns: list[tuple[float, float, int]]) -> None:
    """구간마다 가장 많이 겹치는 화자를 붙인다. 번호는 처음 등장한 순서대로 1부터 매긴다."""
    numbers: dict[int, int] = {}
    for seg in segments:
        overlap: dict[int, float] = {}
        for start, end, speaker in turns:
            o = min(seg["end"], end) - max(seg["start"], start)
            if o > 0:
                overlap[speaker] = overlap.get(speaker, 0.0) + o
        if overlap:
            speaker = max(overlap, key=overlap.get)
            seg["speaker"] = numbers.setdefault(speaker, len(numbers) + 1)
        else:
            seg["speaker"] = None


def _line(seg: dict) -> str:
    if "speaker" not in seg:
        return seg["text"]
    return f"[화자{seg['speaker'] or '?'}] {seg['text']}"


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
    # 통화 이벤트였다면 이제 워커가 전사문으로 판별한다 (실패했으면 전사 없이)
    events.release_transcribed(job["input_path"])


def transcribe_file(input_path: str, output_dir: str, language: str | None) -> tuple[str, float]:
    """(data_dir 기준 .txt 경로, 음성 길이 초) 를 돌려준다."""
    src = resolve_in_data_dir(input_path)
    out = resolve_in_data_dir(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    segments, info = load_model().transcribe(str(src), language=language, vad_filter=True)
    segments = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
                for s in segments]
    meta = {"input": _relative(src), "language": info.language, "duration": round(info.duration, 2)}
    turns = diarize(src)
    if turns is not None:
        assign_speakers(segments, turns)
        meta["speakers"] = len({s["speaker"] for s in segments} - {None})

    txt = out / f"{src.stem}.txt"
    _write(txt, "\n".join(_line(s) for s in segments) + "\n")
    _write(out / f"{src.stem}.json", json.dumps({**meta, "segments": segments}, ensure_ascii=False, indent=2))
    return _relative(txt), round(info.duration, 2)


def _write(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
