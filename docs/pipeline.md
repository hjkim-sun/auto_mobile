# 파이프라인 흐름

API와 메일 폴러는 받은 데이터를 저장만 한다. 판별은 별도 프로세스인 워커가 `events` 테이블을 큐처럼 꺼내 쓰며 나중에 처리한다.

```mermaid
sequenceDiagram
    autonumber
    participant C as Client<br/>(폰 Automate)
    participant M as 메일 폴러<br/>(app/jobs/mail_poller.py)
    participant N as IMAP<br/>(imap.naver.com)
    box API 프로세스 (uvicorn)
        participant A as FastAPI
        participant T as 전사 스레드
    end
    participant S as 저장소<br/>(data/raw, events.db)
    participant W as 워커<br/>(app/jobs/worker.py)
    participant J as Jev<br/>(typesafe.ai)
    participant B as Buzz 봇<br/>(app/jobs/buzz_bot.py)
    participant U as 사용자<br/>(Buzz 앱 DM)

    Note over C,S: 1. 수신: 저장하고 바로 응답한다
    C->>A: POST /v1/ingest/messages 또는 /calls<br/>(Authorization: Bearer 토큰)
    A->>S: 원본 파일 저장, events 행 추가 (status=pending)
    A-->>C: 201 {event_id}<br/>(이미 받은 데이터면 저장 없이 200)

    opt MMS 첨부
        C->>A: PUT /v1/ingest/messages/mms-{id}/attachments/{part}
        A->>S: 첨부 저장, 판별 시점(available_at)을 30초 뒤로 미룸
    end

    opt 통화녹음
        A->>T: 전사 접수 (응답에 transcription_id 포함)
        T->>T: Whisper 전사 + sherpa-onnx 화자 분리(2명)<br/>구간마다 화자1/화자2 를 붙임
        T->>S: 전사 결과 저장<br/>(data/transcripts/calls/YYYYMMDD/*.txt, *.json)
    end

    Note over M,S: 1-1. 메일 수신: 폴러가 IMAP 에서 가져온다
    loop 5분마다 (MAIL_POLL_SECONDS)
        M->>N: 읽기 전용 SELECT INBOX, 커서 이후 UID 검색
        N-->>M: 새 메일 원본 (BODY.PEEK, 읽음 처리 안 함)
        M->>S: data/raw/emails/YYYYMMDD/*.eml 원본과 파싱한 *.json 저장,<br/>events 행 추가 (source=email). Message-ID 가 이미 있으면 건너뜀
        M->>S: mail_cursors 에 마지막 UID 기록
    end

    Note over S,J: 2. 판별: 워커가 큐에서 하나씩 꺼낸다
    loop 3초마다
        W->>S: pending이고 available_at이 지난 이벤트 1건 꺼냄 (status=processing)
        W->>W: 판별용 텍스트 만들기 (render.py)
        alt 판별할 텍스트 없음 (통화녹음, 이미지만 있는 MMS)
            W->>S: status=kept (놓치지 않게 일단 남김)
        else 텍스트 있음
            W->>W: 프롬프트 읽기 (prompts/{source}.toml)
            W->>J: POST /v1/systemone {state, questions}
            J-->>W: useful 확률
            W->>S: threshold 이상이면 kept, 미만이면 discarded
        end
    end
    Note over W,J: Jev 호출이 실패하면 pending으로 되돌려 재시도하고<br/>5회를 넘기면 failed로 둔다

    Note over S,U: 3. 알림과 명령: Buzz 봇 (5초마다)
    loop BUZZ_POLL_SECONDS
        B->>S: kept 인데 notifications 에 없는 이벤트 조회
        B->>U: DM 알림 (요약 + 본문 앞부분, 통화는 전사)
        B->>S: notifications 에 기록
        U->>B: DM 명령 (오늘, 메일 5, #123 ...)
        B->>S: 조회
        B->>U: DM 답장
    end

    Note over C,S: 4. 조회
    C->>A: GET /v1/verdicts/messages
    A->>S: 판별 결과 조회
    A-->>C: {items, next_before_id}
```

| status | 뜻 |
|---|---|
| `pending` | 판별 대기 |
| `processing` | 워커가 판별 중 |
| `kept` | 쓸모 있음. 다음 단계(요약, 알림)로 넘길 대상 |
| `discarded` | 노이즈. 여기서 끝 |
| `failed` | 재시도를 다 써도 실패. 사람이 확인해야 함 |
