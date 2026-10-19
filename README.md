# auto_mobile

폰(통화녹음·SMS/MMS)과 메일에서 들어오는 데이터를 수집하고, Jev 로 쓸모 여부를 판별한다.

```
수신 API (app/api)  ──▶  data/raw/ 원본 저장 + events 테이블 1건 1행
워커 (app/jobs/worker.py)  ──▶  pending 이벤트를 하나씩 꺼내 kept / discarded 판별
```

## 실행

```bash
uv sync
# 전사 모델 (약 1.6GB)
uv run python -c "from huggingface_hub import snapshot_download as d; d('mobiuslabsgmbh/faster-whisper-large-v3-turbo', local_dir='$HOME/whisper/large-v3-turbo')"
cp .env.example .env   # TYPESAFE_API_KEY, INGEST_TOKEN 설정
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080
uv run python -m app.jobs.worker        # 반드시 1개만
uv run pytest
```

## API

| 메서드 | 경로 | body |
|---|---|---|
| POST | `/v1/ingest/calls?filename=` | raw 오디오 |
| POST | `/v1/ingest/messages` | `{"kind": "sms"\|"mms", "id", "from", "date", "text"?, "parts"?}` |
| PUT | `/v1/ingest/messages/{sms\|mms}-{id}/attachments/{part_id}?filename=` | raw 첨부 |
| POST | `/v1/ingest/emails?account=naver\|gmail` | raw .eml |
| GET | `/v1/verdicts/messages?status=&limit=&before_id=` | |
| GET | `/v1/verdicts/emails?status=&limit=&before_id=` | |
| POST | `/v1/transcriptions` | `{"input_path", "output_dir"?, "language"?}` |
| GET | `/v1/transcriptions/{id}` | |
| GET | `/v1/health` | |

수신(`/v1/ingest/*`)은 새로 받으면 201, 이미 받은 것이면 200. 응답 body 는 `{"event_id", "duplicate"}`.

판별 내역(`/v1/verdicts/*`)은 최신순으로 `{"items": [...], "next_before_id"}` 를 돌려준다.
`status` 는 `pending|processing|kept|discarded|failed`, 다음 페이지는 `?before_id=<next_before_id>`.
음성 전사(`/v1/transcriptions`)는 faster-whisper large-v3-turbo(`WHISPER_MODEL_DIR`, 기본 `~/whisper/large-v3-turbo`)로
`input_path` 의 음성을 `output_dir`(기본 `transcripts`) 아래 `{파일명}.txt` 와 `{파일명}.json`(구간별 시각) 으로 저장한다.
경로는 모두 `DATA_DIR` 기준이며 그 밖은 400. CPU 에서 실시간의 절반쯤 걸리므로 접수만 하고 202 를 돌려준다.
`GET /v1/transcriptions/{id}` 의 `status` 가 `queued|running|done|failed` 이고, `done` 이면 `text` 가 채워진다.
`language` 를 생략하면 `WHISPER_LANGUAGE`(기본 `ko`), `"auto"` 면 자동 감지.

`/v1/health` 를 뺀 모든 API 는 `Authorization: Bearer <INGEST_TOKEN>` 이 필요하다.

API 문서(`/docs`, `/redoc`, `/openapi.json`)는 VPS 안에서 `http://127.0.0.1:8080/docs` 로만 열린다.
Caddy 를 거친 요청(X-Forwarded-For 가 붙은 요청)에는 404 를 준다.

## 저장 경로 (`DATA_DIR`, 기본 `data/`)

```
data/
  events.db
  raw/
    calls/{sha256 앞 12자}_{파일명}
    messages/YYYYMMDD/{sms|mms}-{id}.json     YYYYMMDD 는 문자 자체의 date 기준
    messages/YYYYMMDD/{sms|mms}-{id}/{첨부}
    emails/{sha256 앞 16자}.eml
  transcripts/            전사 결과 기본 위치 (output_dir 로 바꿀 수 있음)
```

## 디렉토리

```
app/
  main.py               FastAPI 앱
  api/                  라우터 (ingest, verdicts, transcriptions, health, deps)
  core/config.py        Settings (.env)
  db/session.py         SQLite 연결·스키마
  repositories/events.py  events 큐
  schemas/              응답 모델
  services/             수신 저장, 판별 텍스트 변환, Jev 호출, 음성 전사
  jobs/worker.py        판별 워커
tests/
```
