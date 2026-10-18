# auto_mobile

폰(통화녹음·SMS/MMS)과 메일에서 들어오는 데이터를 수집하고, Jev 로 쓸모 여부를 판별한다.

```
수신 API (app/api)  ──▶  data/raw/ 원본 저장 + events 테이블 1건 1행
워커 (app/jobs/worker.py)  ──▶  pending 이벤트를 하나씩 꺼내 kept / discarded 판별
```

## 실행

```bash
uv sync
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
| GET | `/v1/health` | |

새로 받으면 201, 이미 받은 것이면 200. 응답 body 는 `{"event_id", "duplicate"}`.

## 저장 경로 (`DATA_DIR`, 기본 `data/`)

```
data/
  events.db
  raw/
    calls/{sha256 앞 12자}_{파일명}
    messages/YYYYMMDD/{sms|mms}-{id}.json     YYYYMMDD 는 문자 자체의 date 기준
    messages/YYYYMMDD/{sms|mms}-{id}/{첨부}
    emails/{sha256 앞 16자}.eml
```

## 디렉토리

```
app/
  main.py               FastAPI 앱
  api/                  라우터 (ingest, health, deps)
  core/config.py        Settings (.env)
  db/session.py         SQLite 연결·스키마
  repositories/events.py  events 큐
  schemas/              응답 모델
  services/             수신 저장, 판별 텍스트 변환, Jev 호출
  jobs/worker.py        판별 워커
tests/
```
