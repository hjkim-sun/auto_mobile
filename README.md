# auto_mobile

폰(통화녹음·SMS/MMS·카카오톡)과 메일에서 들어오는 데이터를 수집하고, Jev 로 쓸모 여부를 판별한다.

```
수신 API (app/api)  ──▶  data/raw/ 원본 저장 + events 테이블 1건 1행
메일 폴러 (app/jobs/mail_poller.py)  ──▶  IMAP 받은편지함·보낸메일함 새 메일을 5분마다 가져와 같은 방식으로 저장
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
| POST | `/v1/ingest/calls?filename=&direction=&number=&name=&call_date=` | raw 오디오 |
| POST | `/v1/ingest/messages` | `{"kind": "sms"\|"mms", "id", "from", "date", "text"?, "parts"?}` |
| PUT | `/v1/ingest/messages/{sms\|mms}-{id}/attachments/{part_id}?filename=` | raw 첨부 |
| POST | `/v1/ingest/emails?account=naver\|gmail` | raw .eml |
| POST | `/v1/ingest/kakao` | `{"title", "message", "ticker", "when", "channel", "extras"}` (Automate 알림 출력) |
| POST | `/v1/ingest/kakao/history` | `{"rooms": [{"chat_id", "kind": "personal"\|"group", "name", "members"}], "messages": [{"chat_id", "log_id", "sent_at", "author", "text"}]}` |
| POST | `/v1/brief/call?number=&name=` | 없음. 전화 수신 브리핑 (아래) |
| GET | `/v1/verdicts/messages?status=&limit=&before_id=` | |
| GET | `/v1/verdicts/emails?status=&limit=&before_id=` | |
| POST | `/v1/transcriptions` | `{"input_path", "output_dir"?, "language"?}` |
| GET | `/v1/transcriptions/{id}` | |
| GET | `/v1/health` | |
| GET | `/web/flow`, `/web/wiki` | 웹 화면 (아래 "웹 화면") |
| GET | `/wiki/graph`, `/wiki/page?path=`, `/flow/stats` | 웹 화면용 JSON |

수신(`/v1/ingest/*`)은 새로 받으면 201, 이미 받은 것이면 200. 응답 body 는 `{"event_id", "duplicate"}`.
통화녹음(`/v1/ingest/calls`)은 새로 받으면 곧바로 `transcripts/calls/YYYYMMDD`(원본과 같은 날짜 폴더) 로 전사를 접수하고 응답에 `transcription_id` 를 붙인다
(중복이면 `null`). 진행은 `GET /v1/transcriptions/{transcription_id}` 로 본다.
통화 정보 쿼리는 모두 선택이고 빈 값이나 형식이 틀린 값은 버린다(녹음은 받는다). 폰이 통화 기록(`content://call_log/calls`)에서 찾아 붙인다.
`direction`(`incoming`\|`outgoing`\|`unknown`), `number`(상대 번호), `name`(연락처 이름), `call_date`(통화 시작, epoch ms).
payload 에 그대로 저장하고(`call_date_text` 추가), 판별·알림·위키에 `통화: 수신 홍길동 (01012345678)` 처럼 붙인다.
같은 녹음을 다시 보내면 저장은 건너뛰고 payload 에 비어 있던 통화 정보만 채운다.

카카오톡(`/v1/ingest/kakao`)은 폰 Automate 의 `Notification posted?` 출력을 그대로 받는다. 알림 하나가 메시지 하나이고 `extras.chatLogId` 로 중복을 거른다.
카카오톡 알림의 `title` 은 방 이름이 아니라 마지막 메시지를 보낸 사람이고, 단톡방 이름과 오픈채팅 여부는 알림에 없다.
그래서 알림 채널로 거른다. `KAKAO_CHANNELS`(기본 `["new_message_v6"]`, 알림을 켠 방)에 없는 채널과 묶음 요약 알림은 200 `{"ignored": true}`.
오픈채팅방은 카톡에서 알림을 끄면 `quiet_new_message` 로 빠져 받지 않는다. 카톡 자체 방해금지 시간대에 온 메시지도 같은 채널이라 놓친다.

카카오톡 대화 기록(`/v1/ingest/kakao/history`)은 맥에서 `tools/kakao_sync.py` 가 보낸다. 맥 카카오톡 DB 를
[kakaocli](https://github.com/silver-flight-group/kakaocli)로 읽어 1:1 방과 단톡방(오픈채팅·채널·나와의 채팅 제외)의
마지막으로 보낸 메시지 이후 메시지를 보낸다. 서버는 방마다 CSV(`일시,사용자명,톡 내용,log_id,jev 판단,claude 판단`)에 log_id 로
중복을 걸러 쌓고, 응답은 `{"rooms", "added", "duplicates", "unknown_room"}` 이다. 새 메시지가 들어간 방은 방 단위 판단 대기가 된다.
`log_id` 는 폰 알림의 `chatLogId` 와 같은 값이다(Automate 를 거치면 double 이라 끝자리가 ±256 안에서 달라진다).

폰 알림으로 받은 카톡도 워커가 판별 전에 같은 CSV 에 넣는다(`kakao_history.file_notification`).
1:1 은 보낸 사람 이름의 personal 방에, 단톡은 `rooms.json` 에서 보낸 사람이 참여한 단톡방에 넣는다.
참여한 단톡방이 여럿이면 Jev 가 후보 방들의 최근 대화를 보고 고르고, 하나도 없으면 `group/unassigned.csv` 에 둔다.
고른 방은 이벤트 verdict 의 `kakao_room` 에 남는다. 나중에 맥에서 같은 메시지(log_id ±512)가 오면 맥의 정확한 log_id 로 바꾼다.
방을 찾은 알림 이벤트는 `filed` 로 끝내고 판단은 방 단위로 한다. 방을 모르는 알림(unassigned)만 메시지 하나로 판별한다.

카톡 판단은 메시지마다 하지 않고 방 단위로 한다(`app/services/kakao_judge.py`, 워커의 스레드).
메시지마다 최근 대화를 묶어 던지면 창이 거의 겹쳐 같은 판단이 연달아 나오기 때문이다.
1. 판단 대기 방 중 대화가 `KAKAO_JUDGE_SETTLE_SECONDS`(70초, 폰 알림이 들어온 방은 `KAKAO_JUDGE_PHONE_SETTLE_SECONDS` 20초) 멈췄거나 첫 새 메시지 뒤 `KAKAO_JUDGE_MAX_WAIT_SECONDS`(5분)가 지난 방을 고른다.
2. 방 CSV 의 최근 `KAKAO_JUDGE_WINDOW`(20)건을 Jev 에 준다. 판단 열이 빈 메시지에 ▶ 를 붙여 그것만 판단하게 한다(`prompts/kakao.toml`).
   `KAKAO_JUDGE_MAX_AGE_HOURS`(12시간)보다 오래된 메시지는 맥락으로만 쓴다. noise 면 `jev 판단` 열에 `noise 0.12`.
3. useful 이면 Claude(`claude -p`, 위키 읽기 전용, `prompts/kakao_judge.md`)가 다시 보고 캘린더에 넣을 일정을 고른다.
   고르면 `source=kakao` 이벤트(kept)와 `calendar_items` 를 만들고 add 는 바로 네이버 캘린더에 넣는다. Buzz 봇이 일정 알림을 보낸다.
   `claude 판단` 열은 `claude-add #이벤트번호`(ask 만 있으면 `claude-ask`), 고를 일정이 없으면 오탐으로 보고 `claude-deny`.
`KAKAO_JUDGE_ENABLED=false` 면 방 단위 판단을 끄고 예전처럼 알림 하나씩 판별한다.

```bash
KAKAOCLI_USER_ID=<내 카카오 userId> INGEST_URL=https://<서버> python3 tools/kakao_sync.py [--days N] [--dry-run]
tools/install_kakao_sync.sh <내 카카오 userId> https://<서버> [interval초=60]   # launchd 1분 주기 등록
```
주기 실행은 launchd(`com.auto_mobile.kakao_sync`)가 1분마다 돌린다. 로그는 `~/Library/Logs/kakao_sync.log`.
맥 카톡(`KakaoTalk` 프로세스)이 떠 있지 않으면 DB 를 읽지 않고 바로 끝낸다.
보낼 때마다 보낸 메시지 중 가장 늦은 `sentAt` 을 `~/Library/Application Support/auto_mobile/kakao_sync.json` 에 적고, 다음에는 그 5분 전 이후에
보낸 메시지만 보낸다(맥 DB 에 늦게 들어오는 메시지를 덮는 여유. 중복은 서버가 거른다). 실행 시각이 아니라 `sentAt` 을 적으므로
맥이나 카톡을 꺼 뒀다 켜서 그동안의 메시지가 원래 시각으로 DB 에 들어와도 빠지지 않는다.
상태 파일이 없으면 최근 2일치, `--days N` 이면 상태와 관계없이 N일치를 보낸다.
처음 등록하면 macOS 가 python 의 다른 앱 데이터 접근을 물으므로 허용한다. 다시 묻는 일을 없애려면 kakaocli 와 python 에 전체 디스크 접근 권한을 준다.
맥 카톡이 로그인돼 있는 동안 폰 카톡 알림은 `quiet_new_message` 로 와서 폰 플로우가 보내지 않는다. 맥이 꺼져 있을 때만 폰이 보낸다.
payload 는 `sender`(ticker 의 "보낸사람 : 본문" 앞부분), `group`(단톡 여부), `text`, `time`, `chat_log_id`, `context`(같은 보낸 사람의 앞 메시지 5건)다.

판별 내역(`/v1/verdicts/*`)은 최신순으로 `{"items": [...], "next_before_id"}` 를 돌려준다.
`status` 는 `pending|processing|kept|discarded|filed|failed`, 다음 페이지는 `?before_id=<next_before_id>`.
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

전화 수신 브리핑(`/v1/brief/call`)은 폰 Automate 가 전화가 올 때 부른다(`app/services/call_brief.py`).
요약은 사람별로 미리 만들어 `call_briefs` 테이블에 둔다. 전화가 오면 저장된 요약을 바로 돌려준다(서버에서 0.1초 안쪽).
- 문자·카톡·통화(전사가 끝난 때)가 들어오면 그 사람의 `is_updated` 를 1 로 둔다. 사람은 이름으로 묶고, 이름을 모르는 번호는 번호로 두었다가
  같은 번호의 통화녹음으로 이름을 알게 되면 이름 쪽에 합친다. 단톡방 말은 이미 브리핑이 있는 사람만 표시한다.
- 워커의 brief 스레드가 `BRIEF_REFRESH_SECONDS`(30분)마다 `is_updated=1` 인 사람만 다시 요약하고 0 으로 돌린다. 실패하면 1 로 남겨 다음에 다시 한다.
  테이블이 비어 있으면(처음 띄울 때) `BRIEF_DAYS` 안에 대화가 있는 사람을 모두 넣고 시작한다.
- 요약에 넣는 것: 같은 번호의 문자, 이름이 같은 1:1 카톡과 그 사람이 단톡방에서 쓴 말(`BRIEF_DAYS` 30일 안, 각각 최근 `BRIEF_MAX_MESSAGES` 40건),
  같은 번호의 통화 전사문(최근 `BRIEF_CALLS` 3건, 건마다 600자). `claude -p`(`BRIEF_MODEL` sonnet, `BRIEF_EFFORT` low, 도구 없음,
  `prompts/call_brief.md`)로 최근 주제를 3줄 요약한다. 대화가 없으면 claude 를 부르지 않는다.
- 응답은 `{"title": "📞 홍길동", "text": 3줄 요약, "sms", "kakao", "calls", "elapsed", "summarized_at", "html"}` 이다.
  폰은 `html`(카드. 틀은 `app/web/call_card.html`)을 Dialog web 으로 전화 화면 위에 띄우고, 닫으면 `text` 를 알림으로 남긴다.
  창 크기와 위치, CANCEL 버튼은 Automate 가 정해 바꿀 수 없다. 카드를 누르면 닫힌다.
  아직 요약이 없는 사람이면 그때 요약하고(4초 안팎) `summarized_at` 은 null 이다. 이때 실패하면 "요약 실패." 를 돌려준다.
문자는 폰이 받은 문자만 보내므로 내가 보낸 문자는 요약에 없다.

`/v1/health` 를 뺀 모든 API 는 `Authorization: Bearer <INGEST_TOKEN>` 이 필요하다.

API 문서(`/docs`, `/redoc`, `/openapi.json`)는 VPS 안에서 `http://127.0.0.1:8080/docs` 로만 열린다.
Caddy 를 거친 요청(X-Forwarded-For 가 붙은 요청)에는 404 를 준다.

## 판별 프롬프트 (`PROMPTS_DIR`, 기본 `prompts/`)

워커는 이벤트 종류(`events.source`)마다 `prompts/{source}.toml` 의 질문을 Jev 에 보낸다.

| 파일 | 대상 |
|---|---|
| `prompts/message.toml` | SMS/MMS |
| `prompts/kakao.toml` | 카카오톡 (방 단위 판단의 Jev 질문. Claude 단계는 `prompts/kakao_judge.md`) |
| `prompts/email.toml` | 메일 |
| `prompts/call.toml` | 통화녹음 전사 |

`questions.<keep_question>` 의 `<keep_choice>` 확률이 `threshold`(생략하면 `JEV_KEEP_THRESHOLD`) 이상이면 `kept`.
파일은 판별할 때마다 읽으므로 고친 뒤 재시작하지 않아도 된다. 파일이 없거나 형식이 틀리면 그 이벤트는 재시도 대기로 남는다.
통화녹음은 `transcribing` 상태로 들어와 워커가 꺼내지 않는다. 전사 스레드가 끝나면 `pending` 으로 바뀌고, 워커가 파일명과 전사문으로 판별한다.
전사가 실패해도 `pending` 으로 바꾸며, 이때는 Jev 없이 `kept`(`no_text`)로 남긴다.

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
- 받은편지함(INBOX)과 보낸메일함을 가져온다. 보낸메일함은 IMAP LIST 의 `\Sent` 표시로 찾는다(네이버 `Sent Messages`, Gmail `[Gmail]/Sent Mail`). 보낸메일함에서 온 메일은 `payload.sent` 가 true 이고, 판별·위키에는 "보낸메일함 (사용자가 보낸 메일)"로 표시되며, kept 여도 Buzz 로 알리지 않는다.
- 마지막으로 가져온 UID 를 메일함별로 `mail_cursors` 테이블에 남겨 다음 폴링은 그 뒤부터 가져온다.
- 처음이거나 서버의 UIDVALIDITY 가 바뀌면 `MAIL_BACKFILL_DAYS`(기본 1)일 전부터 다시 가져온다. 중복은 Message-ID 로 걸러진다.
  네이버는 UIDVALIDITY 를 항상 0 으로 준다. 그래도 UID 는 계속 늘어나므로 커서는 그대로 동작한다.
- 인증 실패나 네트워크 오류는 로그만 남기고 다음 주기에 다시 시도한다. 한 계정이 실패해도 다른 계정은 계속 가져온다.

## Buzz 봇

[Buzz](https://github.com/block/buzz)(Block 의 Nostr 기반 메신저)로 kept 판별된 문자·메일·통화를 DM 으로 알리고, DM 명령에 답한다.
`app/jobs/buzz_bot.py` 가 `BUZZ_POLL_SECONDS`(기본 5초)마다 알릴 것을 보내고 사용자의 새 DM 을 읽는다.

```
BUZZ_RELAY_URL=https://srv2036064.hstgr.cloud:8443   # 자체 운영 relay (deploy/buzz-relay)
BUZZ_PRIVATE_KEY=<봇 키, hex 또는 nsec>
BUZZ_OWNER_PUBKEY=<사용자 npub>
BUZZ_BOT_NAME=auto_mobile        # 앱 DM 목록에 보이는 봇 이름. 봇이 시작할 때 프로필에 넣는다
BUZZ_NOTIFY_CHANNEL=<채널 UUID>    # 알림을 보낼 채널. 비우면 DM 으로 보낸다
```

- relay 와는 공식 `buzz` CLI 로 주고받는다. CLI 는 데스크톱 릴리스 `.deb` 에 들어 있다:
  `dpkg-deb -x Buzz_<버전>_amd64.deb x && cp x/usr/bin/buzz ~/.local/bin/` (`BUZZ_CLI_PATH`, 기본 `~/.local/bin/buzz`)
- relay 는 이 서버에서 docker 로 운영한다(`deploy/buzz-relay/README.md`). 봇이 커뮤니티 owner 다.
- `python -m app.jobs.buzz_bot --whoami` 로 연결을 확인하고, `--invite` 로 사용자용 초대 링크(7일, 1회용)를 만든다. 앱에는 출력의 `app_url`(`buzz://join?…`)을 넣는다.
  폰 Buzz 앱 첫 화면의 "Enter a community link" 에 그 링크를 넣는다.
- 알림(kept 이벤트, 캘린더 등록·질문)은 `BUZZ_NOTIFY_CHANNEL` 이 있으면 그 채널로, 없으면 주인과의 DM 으로 보낸다. 캘린더 질문에 채널에서 답하려면 그 채널을 `BUZZ_LISTEN_CHANNELS` 에도 넣는다.
- kept 알림은 원문을 그대로 옮기지 않고 에이전트가 브리핑한다. 한 주기에 들어온 것을 묶어, 위키에서 보낸 사람·진행 중인 일·일정을 찾아 무엇인지, 할 일과 기한이 있는지 말한다(`prompts/agent.md` 의 '새 자료 브리핑'). 끝에 `원문: #번호` 를 붙인다. 에이전트가 실패하거나 `AGENT_BRIEFING=false` 면 원문 알림을 보낸다.
- 봇을 처음 띄울 때 이미 있던 이벤트는 알리지 않는다. 그 뒤 kept 된 이벤트를 한 번씩 알린다(`notifications` 테이블). 내가 보낸 메일은 알리지 않는다.
- `BUZZ_OWNER_PUBKEY` 와의 DM 에서는 그 사람의 글에만 답한다. 명령: `도움말`, `오늘`, `문자|카톡|메일|통화 [n]`, `버린 [n]`, `상태`, `#번호`, `위키 <질문>`, `일정`, `등록 N`, `안함 N`, `새대화`.

### 에이전트

봇은 명령만 받는 프로그램이 아니라 Buzz 에 들어와 있는 에이전트로도 동작한다. 명령이 아닌 말은 이 서버의 Claude Code 가 답하므로 맥이 꺼져 있어도 24시간 답한다.

- DM 에서 명령이 아닌 말, 그리고 봇이 멤버인 채널에서 `@봇이름`(`BUZZ_BOT_NAME`)으로 멘션한 말에 답한다. 채널에서는 그 메시지의 스레드로 답하고, 채널의 최근 대화 10건을 맥락으로 함께 넘긴다.
- 답을 만드는 동안 받은 메시지에 👀 반응을 단다. 에이전트는 별도 스레드에서 돌아 알림이 밀리지 않는다.
- `app/services/agent.py` 가 `claude -p --restricted` 를 위키 디렉토리에서 띄운다. Bash 가 없고 파일 도구는 위키 안만 다룬다. 원본 이벤트와 캘린더는 MCP 서버 `app/jobs/agent_mcp.py` 의 도구(`search_events`, `get_event`, `upcoming_calendar`, `pending_calendar`, `status_counts`)로만 다룬다.
- 주인과의 대화에서는 개인 비서로 일한다. 웹 검색·조회(`WebSearch`, `WebFetch`)로 조사하고, 위키를 쓰고(`Write`, `Edit`, 조사 결과는 `research/`), 캘린더에 일정을 넣고 지운다(`add_calendar`, `decide_calendar`, `remove_calendar`). 위키를 고쳤으면 위키 git 에 `agent: 주인 요청` 으로 남긴다.
- 다른 에이전트와의 대화와 새 자료 브리핑에서는 이 쓰기·웹 도구를 주지 않는다(MCP 서버도 `AUTO_MOBILE_AGENT_OWNER=0` 이면 캘린더 쓰기 도구를 숨긴다). 자료나 남의 말에 적힌 지시로 쓰기나 바깥 요청을 하지 않게 하려는 것이다. 규칙은 `prompts/agent.md`.
- 대화 맥락은 채널마다 claude 세션 하나로 이어 간다. `AGENT_SESSION_HOURS`(기본 6) 동안 말이 없거나 `새대화` 라고 하면 새 세션을 연다.
- 다른 에이전트(Claude, Codex 등)도 멘션하면 답한다. 에이전트끼리 끝없이 주고받지 않게, 주인이 다시 말할 때까지 `AGENT_PEER_REPLIES`(기본 3)번까지만 답한다. relay 가 닫힌 커뮤니티라 멤버만 말을 걸 수 있다.
- 듣는 채널(`BUZZ_LISTEN_CHANNELS`, 채널 UUID 의 JSON 목록)에서는 주인의 말에 멘션 없이도 답한다. 채널에서 한 말은 채널로, 스레드 안에서 한 말은 그 스레드로 답한다. 주인이 다른 사람이나 에이전트를 `@이름`으로 콕 집어 부른 말에는 끼어들지 않는다. 다른 에이전트의 말에는 여기서도 멘션했을 때만 답한다.
- 다른 채널에서 쓰려면 그 채널에 봇을 멤버로 넣는다. 에이전트가 자기 이름으로 아는 것도 `BUZZ_BOT_NAME` 이다(`prompts/agent.md` 의 `{bot_name}`).

```
BUZZ_LISTEN_CHANNELS='["<채널 UUID>"]'   # buzz channels list 로 찾는다
AGENT_ENABLED=true          # false 면 명령이 아닌 말에 도움말만 보낸다
AGENT_MODEL=sonnet
AGENT_TIMEOUT_SECONDS=300
AGENT_SESSION_HOURS=6
AGENT_PEER_REPLIES=3
AGENT_BRIEFING=true         # false 면 kept 알림을 원문 그대로 보낸다
```

## LLM 위키

kept 판별된 문자·메일·통화를 Claude Code 가 `data/wiki/` 의 마크다운 위키로 정리한다.
원본은 그대로 두고, LLM 이 자료 요약과 사람·기관·주제 페이지, 일정, 목록을 쓰고 고친다.
`app/jobs/wiki_builder.py` 가 `WIKI_POLL_SECONDS`(기본 600초)마다 아직 넣지 않은 kept 이벤트를 `WIKI_BATCH_SIZE`(기본 5)건씩 넘긴다.

```bash
uv run python -m app.jobs.wiki_builder           # 계속 돌며 새 kept 를 넣는다 (반드시 1개만, data/wiki.lock)
uv run python -m app.jobs.wiki_builder --once    # 쌓인 것만 넣고 종료. 처음엔 기존 kept 를 모두 넣는다
uv run python -m app.jobs.wiki_builder --ask "홍길동과 약속 언제야?"
uv run python -m app.jobs.wiki_builder --lint    # 깨진 링크, 중복 페이지, 모순, 지난 일정 정리
```

- 위키를 쓰는 규칙은 `prompts/wiki.md`(`WIKI_SCHEMA_PATH`)에 있다. 페이지 구조나 쓰는 방식을 바꾸려면 이 파일을 고친다.
- `claude -p --restricted --safe-mode` 로 띄운다. Bash 가 없고 파일 도구(`Read,Write,Edit,Glob,Grep`)는 `data/wiki` 안에서만 쓴다.
  문자·메일 본문은 `<source>` 태그로 감싸 데이터로만 다루게 한다.
- `claude` 는 automan 계정에 로그인돼 있어야 한다(`WIKI_CLAUDE_PATH`, 기본 `~/.local/bin/claude`). 모델은 `WIKI_MODEL`(기본 `sonnet`).
- `data/wiki` 는 따로 git 저장소다(공개 저장소에 올라가지 않는다). 넣을 때마다 커밋하므로 `git -C data/wiki log -p` 로 LLM 이 고친 내용을 보고 되돌릴 수 있다.
  claude 가 실패하면 고치다 만 파일을 마지막 커밋으로 되돌리고, 다음 주기에 같은 자료를 다시 넣는다.
- 통화는 전사가 끝난 뒤에 넣는다. 넣은 이벤트는 `wiki_ingests` 테이블에 남는다.
- Buzz DM 으로 `위키 <질문>` 을 보내면 위키를 읽고 답한다.

### 네이버 캘린더

자료를 넣을 때 Claude 가 캘린더에 넣을 일정도 고른다(`prompts/wiki.md` 의 "캘린더 일정 판단").

| decision | 예 | 처리 |
|---|---|---|
| `add` | 참석하겠다고 한 약속, 확정된 예약, 직접 해야 하는 일의 마감 | 위키 빌더가 바로 등록하고, Buzz 봇이 넣었다고 알린다 |
| `ask` | 참석 여부를 정할 초대·행사, 답하지 않은 제안 | Buzz 봇이 묻는다. `등록 N` 이면 넣고 `안함 N` 이면 넣지 않는다 |
| (없음) | 카드 결제 예정일, 약관 시행일처럼 할 일이 없는 안내 | 넣지 않는다 |

```
CALENDAR_ACCOUNT=naver-main   # MAIL_ACCOUNTS 의 name. 비우면 첫 번째 네이버 계정
CALENDAR_NAME=내 캘린더        # 일정을 넣을 캘린더 이름
CALENDAR_ENABLED=true
```

- CalDAV(`caldav.calendar.naver.com`)에 메일과 같은 애플리케이션 비밀번호로 로그인한다.
- 넣은 일정은 UID 가 `auto-mobile-` 로 시작하고 설명 끝에 `auto_mobile #자료번호` 가 붙는다.
- 후보와 처리 상태는 `calendar_items` 테이블에 남는다. 등록이 실패하면 `pending` 으로 남아 다음 주기에 다시 넣는다.
- Buzz 봇 명령: `일정`(답을 기다리는 일정), `등록 N`, `안함 N`. Buzz 없이 `python -m app.jobs.buzz_bot --cmd "등록 2"` 로도 처리한다.
- 캘린더 연동 전에 위키에 넣은 자료는 `python -m app.jobs.wiki_builder --plan-calendar` 로 `schedule.md` 에서 후보를 고른다.

```
data/wiki/
  index.md        모든 페이지 목록
  log.md          작업 기록 (프로그램이 적는다)
  schedule.md     일정, 약속, 마감, 할 일
  sources/{id}.md 자료 1건 요약 (id 는 events.id)
  people/  orgs/  topics/
```

## 웹 화면

영상이나 데모에서 시나리오를 보여 주는 화면이다. 위키에 개인 정보가 있어서 `WEB_TOKEN` 을 비우면 화면과 그 API 가 모두 404 다.
처음에는 `https://<도메인>/web/flow?token=<WEB_TOKEN>` 으로 연다. 토큰은 30일짜리 쿠키(HttpOnly)로 남고, 주소에서 토큰을 뺀 곳으로 옮겨 간다.
JSON API 는 그 쿠키나 `Authorization: Bearer <WEB_TOKEN>` 으로 연다. `INGEST_TOKEN` 과는 따로 둔다(폰 토큰을 브라우저에 넣지 않게).

- `/web/flow` 흐름: 시나리오 두 개를 그린 화면.
  ① 문자·메일·통화(·카카오톡) → Jev 판별 → 위키·캘린더 → Buzz 알림. ② Buzz 질문 → 에이전트 → 위키·이벤트·캘린더 → 답.
  상자마다 실제 건수를, 선마다 단계별로 걸린 시간의 중앙값(최근 kept 30건)을 붙인다. 5초마다 `/flow/stats` 를 다시 읽고,
  새 이벤트나 상태가 바뀐 이벤트, 새 대화가 보이면 그 이벤트가 실제로 거친 길을 따라 점이 흐른다.
  "흐름 재생" 은 최근 이벤트 10건(대화 3건)을 차례로 다시 흘린다. 아래에는 최근 이벤트의 단계(남김, 위키, 알림, 일정)와 Buzz 대화가 나온다.
  대화는 봇이 에이전트로 답할 때마다 `agent_turns` 테이블에 남긴 것이다.
- `/web/wiki` 위키 그래프: `data/wiki` 를 3D 은하로 그린다(3d-force-graph, three.js 블룸). `index.md`, `log.md` 는 뺀다.
  사람·기관·주제·일정 페이지는 행성, 자료(`sources/`)는 작은 별이다. 색은 자료가 들어온 채널(통화·문자·메일·카카오톡)만 나타낸다.
  행성 색은 이어진 자료 중 가장 많은 채널을 따르고, 이어진 자료가 없으면 흰색이다. 연결이 5개 이상인 행성에는 고리가 있다.
  행성들은 납작한 원반으로 모이고 카메라가 천천히 돈다("회전" 버튼으로 끈다). 행성 하나에만 이어진 자료는 그 행성 둘레를 공전하고,
  어디에도 이어지지 않은 자료는 바깥 소행성대를 돈다. 여러 행성을 잇는 자료는 그 사이에 머문다.
  노드를 누르면 카메라가 날아가고 페이지 본문과 이어진 페이지가 나온다. 본문 링크를 누르면 그 노드로 옮겨 간다.
  재생 버튼은 위키 git 이력에서 파일이 처음 커밋된 순서대로, 빈 우주에서 은하가 자라는 과정을 25초 안에 보여 준다.
  30초마다 다시 읽어 새로 생긴 별은 부풀며 나타난다.
  자료 노드가 1500개를 넘으면(카카오톡 대화를 넣은 뒤 등) 처음에는 자료를 숨기고 행성만 그린다. 왼쪽 필터에서 켤 수 있다.
  이름은 연결이 많은 행성 60개(좁은 화면은 15개)에만 늘 띄우고, 나머지는 가리키거나 고른 노드의 이웃일 때 띄운다.
  그래프는 파일 수와 마지막 수정 시각, 위키 HEAD 가 같으면 다시 만들지 않는다.
- 새 이벤트 종류(예: `kakao`)는 `events.source` 값 그대로 색과 이름이 붙는다. 이름은 화면의 `SOURCES` 에, 색은 `--kakao` 처럼 CSS 변수에 있다.

## 저장 경로 (`DATA_DIR`, 기본 `data/`)

```
data/
  events.db
  wiki/                   LLM 위키 (아래 "LLM 위키")
  raw/
    calls/YYYYMMDD/{sha256 앞 12자}_{파일명}      YYYYMMDD 는 파일명 속 녹음 시각(…_yyMMdd_HHmmss) 기준, 없으면 받은 날
    messages/YYYYMMDD/{sms|mms}-{id}.json     YYYYMMDD 는 문자 자체의 date 기준 (한국 시간)
    messages/YYYYMMDD/{sms|mms}-{id}/{첨부}
    emails/YYYYMMDD/{sha256 앞 16자}.eml        YYYYMMDD 는 Date 헤더 기준 (한국 시간), 없으면 받은 날
    emails/YYYYMMDD/{sha256 앞 16자}.json    .eml 을 푼 것: 헤더, date_text, 본문 텍스트(text), 첨부 목록
    kakao/YYYYMMDD/{chatLogId}.json          폰 알림으로 받은 카톡 1건
    kakao/rooms.json                         맥에서 받은 방 목록: chat_id → kind, name, file, members(단톡 참여자)
    kakao/personal/{상대 이름}.csv            1:1 방 대화 기록. 이름이 겹치면 {상대 이름}_{chat_id}.csv
                                             열: 일시, 사용자명, 톡 내용, log_id, jev 판단, claude 판단
    kakao/group/{chat_id}.csv                단톡방 대화 기록
    kakao/group/unassigned.csv               폰 알림 단톡 중 보낸 사람이 참여한 방을 모르는 것
  transcripts/            전사 결과 기본 위치 (output_dir 로 바꿀 수 있음)
    calls/YYYYMMDD/{sha256 앞 12자}_{파일명}.txt|.json   수신한 통화녹음의 자동 전사 결과. 날짜 폴더는 원본과 같다
```

날짜 표시(`date_text`), 문자·통화·메일 날짜 폴더, 통화녹음 기본 파일명은 `TIMEZONE`(기본 `Asia/Seoul`) 기준이다.

## 디렉토리

```
app/
  main.py               FastAPI 앱
  api/                  라우터 (ingest, verdicts, transcriptions, health, web, deps)
  web/                  웹 화면 HTML (flow.html, wiki.html)
  core/config.py        Settings (.env)
  db/session.py         SQLite 연결·스키마
  repositories/events.py  events 큐
  schemas/              응답 모델
  services/             수신 저장, 판별 텍스트 변환, 프롬프트 로드, Jev 호출, 음성 전사, 위키 그래프·흐름 통계
prompts/                판별 프롬프트 (이벤트 종류별 .toml), 위키 규칙(wiki.md), 에이전트 규칙(agent.md)
  jobs/worker.py        판별 워커
  jobs/mail_poller.py   IMAP 메일 폴러
  jobs/buzz_bot.py      Buzz 알림·명령 봇, 에이전트 대화
  jobs/agent_mcp.py     에이전트용 MCP 서버 (이벤트·캘린더 도구, 캘린더 쓰기는 주인 대화만)
  jobs/wiki_builder.py  LLM 위키 빌더
tests/
tools/kakao_sync.py     맥 카카오톡 대화 기록을 서버로 보낸다 (맥에서만 실행)
tools/install_kakao_sync.sh  kakao_sync.py 를 맥 launchd 에 1분 주기로 등록
```
