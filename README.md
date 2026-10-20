# auto_mobile

폰(통화녹음·SMS/MMS)과 메일에서 들어오는 데이터를 수집하고, Jev 로 쓸모 여부를 판별한다.

```
수신 API (app/api)  ──▶  data/raw/ 원본 저장 + events 테이블 1건 1행
메일 폴러 (app/jobs/mail_poller.py)  ──▶  IMAP 새 메일을 5분마다 가져와 같은 방식으로 저장
워커 (app/jobs/worker.py)  ──▶  pending 이벤트를 하나씩 꺼내 kept / discarded 판별
```

흐름 상세: [docs/pipeline.md](docs/pipeline.md) (시퀀스 다이어그램)

## 실행

```bash
uv sync
# 전사 모델 (약 1.6GB)
uv run python -c "from huggingface_hub import snapshot_download as d; d('mobiuslabsgmbh/faster-whisper-large-v3-turbo', local_dir='$HOME/whisper/large-v3-turbo')"
# 화자 분리 모델 (약 45MB, sherpa-onnx). 없으면 화자 없이 전사만 한다
mkdir -p ~/sherpa && cd ~/sherpa
curl -LO https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2
python3 -c "import tarfile; tarfile.open('sherpa-onnx-pyannote-segmentation-3-0.tar.bz2').extractall('.', filter='data')"
curl -LO https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx
cd -
cp .env.example .env   # TYPESAFE_API_KEY, INGEST_TOKEN 설정
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080
uv run python -m app.jobs.worker        # 반드시 1개만
uv run python -m app.jobs.mail_poller   # 메일 폴링 (반드시 1개만)
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
통화녹음(`/v1/ingest/calls`)은 새로 받으면 곧바로 `transcripts/calls` 로 전사를 접수하고 응답에 `transcription_id` 를 붙인다
(중복이면 `null`). 진행은 `GET /v1/transcriptions/{transcription_id}` 로 본다.

판별 내역(`/v1/verdicts/*`)은 최신순으로 `{"items": [...], "next_before_id"}` 를 돌려준다.
`status` 는 `pending|processing|kept|discarded|failed`, 다음 페이지는 `?before_id=<next_before_id>`.
메일 항목에는 파싱한 본문(`text`)과 첨부 목록(`attachments`)이 들어 있다.
음성 전사(`/v1/transcriptions`)는 faster-whisper large-v3-turbo(`WHISPER_MODEL_DIR`, 기본 `~/whisper/large-v3-turbo`)로
`input_path` 의 음성을 `output_dir`(기본 `transcripts`) 아래 `{파일명}.txt` 와 `{파일명}.json`(구간별 시각) 으로 저장한다.
경로는 모두 `DATA_DIR` 기준이며 그 밖은 400. CPU 에서 실시간의 절반쯤 걸리므로 접수만 하고 202 를 돌려준다.
`GET /v1/transcriptions/{id}` 의 `status` 가 `queued|running|done|failed` 이고, `done` 이면 `text` 가 채워진다.
`language` 를 생략하면 `WHISPER_LANGUAGE`(기본 `ko`), `"auto"` 면 자동 감지.

전사와 함께 sherpa-onnx 로 화자를 나눈다. `.txt` 는 줄마다 `[화자1] 여보세요` 처럼 화자가 붙고,
`.json` 은 구간마다 `speaker`(1부터, 겹치는 화자가 없으면 `null`)와 전체 `speakers` 수가 들어간다.
번호는 처음 말한 사람부터 매기므로 누가 "나"인지는 알 수 없다. 통화는 2명으로 보고 나눈다
(`DIARIZATION_NUM_SPEAKERS`, 0 이면 `DIARIZATION_THRESHOLD` 로 추정). `DIARIZATION_ENABLED=false` 거나
모델 파일(`DIARIZATION_SEGMENTATION_MODEL`, `DIARIZATION_EMBEDDING_MODEL`)이 없거나 화자 분리가 실패하면 화자 없이 저장한다.

`/v1/health` 를 뺀 모든 API 는 `Authorization: Bearer <INGEST_TOKEN>` 이 필요하다.

API 문서(`/docs`, `/redoc`, `/openapi.json`)는 VPS 안에서 `http://127.0.0.1:8080/docs` 로만 열린다.
Caddy 를 거친 요청(X-Forwarded-For 가 붙은 요청)에는 404 를 준다.

## 판별 프롬프트 (`PROMPTS_DIR`, 기본 `prompts/`)

워커는 이벤트 종류(`events.source`)마다 `prompts/{source}.toml` 의 질문을 Jev 에 보낸다.

| 파일 | 대상 |
|---|---|
| `prompts/message.toml` | SMS/MMS |
| `prompts/email.toml` | 메일 |

`questions.<keep_question>` 의 `<keep_choice>` 확률이 `threshold`(생략하면 `JEV_KEEP_THRESHOLD`) 이상이면 `kept`.
파일은 판별할 때마다 읽으므로 고친 뒤 재시작하지 않아도 된다. 파일이 없거나 형식이 틀리면 그 이벤트는 재시도 대기로 남는다.
통화녹음용 프롬프트(`call.toml`)는 전사 텍스트를 판별에 넣을 때 만든다.

## 메일 폴러

`MAIL_ACCOUNTS` 에 넣은 계정마다 받은메일함(INBOX)을 `MAIL_POLL_SECONDS`(기본 300초)마다 확인해 새 메일을 넣는다.

```
MAIL_ACCOUNTS='[{"name": "naver-main", "user": "아이디", "password": "앱 비밀번호"},
                {"name": "naver-sub", "user": "아이디2", "password": "앱 비밀번호2"}]'
```

- `name` 은 판별 결과의 `account` 로 남고 계정마다 달라야 한다(영문 소문자, 숫자, `_.-`). `host` 를 생략하면 `imap.naver.com`.
- 네이버 메일 환경설정 → POP3/IMAP 설정에서 IMAP 을 켜야 하고, 2단계 인증을 쓰면 애플리케이션 비밀번호를 넣는다.
- 같은 메일이 두 계정에 다 오면 Message-ID 로 한 번만 저장되고, 먼저 가져온 계정 이름이 남는다.
- 읽기 전용으로 열고 `BODY.PEEK` 로 가져오므로 메일이 읽음 처리되지 않는다.
- 마지막으로 가져온 UID 를 `mail_cursors` 테이블에 남겨 다음 폴링은 그 뒤부터 가져온다.
- 처음이거나 서버의 UIDVALIDITY 가 바뀌면 `MAIL_BACKFILL_DAYS`(기본 1)일 전부터 다시 가져온다. 중복은 Message-ID 로 걸러진다.
  네이버는 UIDVALIDITY 를 항상 0 으로 준다. 그래도 UID 는 계속 늘어나므로 커서는 그대로 동작한다.
- 인증 실패나 네트워크 오류는 로그만 남기고 다음 주기에 다시 시도한다. 한 계정이 실패해도 다른 계정은 계속 가져온다.

## 저장 경로 (`DATA_DIR`, 기본 `data/`)

```
data/
  events.db
  raw/
    calls/{sha256 앞 12자}_{파일명}
    messages/YYYYMMDD/{sms|mms}-{id}.json     YYYYMMDD 는 문자 자체의 date 기준 (한국 시간)
    messages/YYYYMMDD/{sms|mms}-{id}/{첨부}
    emails/{sha256 앞 16자}.eml
    emails/{sha256 앞 16자}.json    .eml 을 푼 것: 헤더, date_text, 본문 텍스트(text), 첨부 목록
  transcripts/            전사 결과 기본 위치 (output_dir 로 바꿀 수 있음)
    calls/{sha256 앞 12자}_{파일명}.txt|.json   수신한 통화녹음의 자동 전사 결과
```

날짜 표시(`date_text`), 문자 날짜 폴더, 통화녹음 기본 파일명은 `TIMEZONE`(기본 `Asia/Seoul`) 기준이다.

## 디렉토리

```
app/
  main.py               FastAPI 앱
  api/                  라우터 (ingest, verdicts, transcriptions, health, deps)
  core/config.py        Settings (.env)
  db/session.py         SQLite 연결·스키마
  repositories/events.py  events 큐
  schemas/              응답 모델
  services/             수신 저장, 판별 텍스트 변환, 프롬프트 로드, Jev 호출, 음성 전사
prompts/                판별 프롬프트 (이벤트 종류별 .toml)
  jobs/worker.py        판별 워커
  jobs/mail_poller.py   IMAP 메일 폴러
tests/
```
