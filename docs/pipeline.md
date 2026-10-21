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
    participant U as 사용자·다른 에이전트<br/>(Buzz 앱 DM·채널, 자체 relay :8443 경유)
    participant K as 위키 빌더<br/>(app/jobs/wiki_builder.py)
    participant L as Claude Code<br/>(claude -p, 파일은 data/wiki 안에서만)
    participant G as 네이버 캘린더<br/>(CalDAV)

    Note over C,S: 1. 수신: 저장하고 바로 응답한다
    C->>A: POST /v1/ingest/messages 또는 /calls<br/>(Authorization: Bearer 토큰)<br/>통화는 통화 기록에서 찾은 수신/발신, 번호, 이름, 시각을 쿼리로 붙임
    A->>S: 원본 파일 저장, events 행 추가<br/>(status=pending, 통화는 status=transcribing)
    A-->>C: 201 {event_id}<br/>(이미 받은 데이터면 저장 없이 200. 통화는 비어 있던 통화 정보만 채움)

    opt MMS 첨부 (2026-10-21부터 폰 플로우가 보내지 않음)
        C->>A: PUT /v1/ingest/messages/mms-{id}/attachments/{part}
        A->>S: 첨부 저장, 판별 시점(available_at)을 30초 뒤로 미룸
    end

    opt 통화녹음
        A->>T: 전사 접수 (응답에 transcription_id 포함)
        T->>T: Whisper 전사 + sherpa-onnx 화자 분리(2명)<br/>구간마다 화자1/화자2 를 붙임
        T->>S: 전사 결과 저장<br/>(data/transcripts/calls/YYYYMMDD/*.txt, *.json)
        T->>S: 통화 이벤트 status=transcribing → pending<br/>(전사가 실패해도 넘긴다)
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
        W->>S: pending이고 available_at이 지난 이벤트 1건 꺼냄 (status=processing)<br/>전사 중인 통화(transcribing)는 꺼내지 않음
        W->>W: 판별용 텍스트 만들기 (render.py)<br/>통화는 파일명 + 수신/발신·상대 + 전사문
        alt 판별할 텍스트 없음 (전사 실패한 통화, 이미지만 있는 MMS)
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
        B->>U: 브리핑: 에이전트(claude)가 위키에서 맥락을 찾아<br/>비서처럼 정리한 글 + 원문 #번호. 실패하거나 AGENT_BRIEFING=false 면<br/>원문 알림 (요약 + 본문 앞부분). BUZZ_NOTIFY_CHANNEL 채널, 없으면 DM
        B->>S: notifications 에 기록
        U->>B: DM 명령 (오늘, 메일 5, #123 ...)
        B->>S: 조회
        B->>U: DM 답장
    end
    U->>B: 명령이 아닌 말 (DM, 듣는 채널의 주인 말, 또는 채널에서 @봇이름 멘션.<br/>다른 에이전트의 멘션은 주인이 다시 말할 때까지 3번까지)
    B->>U: 👀 반응
    B->>L: 에이전트 스레드에서 claude -p --resume (채널마다 세션 하나)<br/>+ 채널 최근 대화 10건, 규칙 prompts/agent.md
    L->>S: MCP 도구 (app/jobs/agent_mcp.py)로 이벤트 조회,<br/>주인이 정했으면 캘린더 등록/안함
    L-->>B: 답 (위키와 #번호 근거)
    B->>S: agent_turns 에 질문과 답 기록 (웹 흐름 화면용)
    B->>U: DM 답장, 채널이면 그 메시지의 스레드로

    Note over S,L: 4. LLM 위키: 위키 빌더 (10분마다)
    loop WIKI_POLL_SECONDS
        K->>S: kept 인데 wiki_ingests 에 없는 이벤트 조회<br/>(통화는 전사가 끝난 것만)
        K->>L: 자료 최대 5건 + 위키 규칙(prompts/wiki.md)
        L->>L: data/wiki 의 sources/, people/, orgs/, topics/,<br/>schedule.md, index.md 를 쓰고 고침
        L-->>K: 캘린더 일정 후보 (decision=add 또는 ask)
        K->>S: wiki_ingests 에 기록, log.md 추가, data/wiki git 커밋,<br/>calendar_items 에 후보 저장
        K->>G: decision=add (확실한 약속, 예약, 마감) 는 바로 등록
    end
    B->>U: 등록한 일정 알림, decision=ask (참석 여부를 정할 초대 등) 는<br/>"넣을까요? 등록 N / 안함 N"
    U->>B: DM "등록 N"
    B->>G: 일정 등록
    Note over K,L: claude 가 실패하면 고치다 만 파일을 되돌리고<br/>다음 주기에 같은 자료를 다시 넣는다
    U->>B: DM "위키 질문"
    B->>L: 읽기 전용으로 질문
    L-->>B: 답 (근거 페이지, #번호)
    B->>U: DM 답장

    Note over C,S: 5. 조회
    C->>A: GET /v1/verdicts/messages
    A->>S: 판별 결과 조회
    A-->>C: {items, next_before_id}

    Note over A,S: 6. 웹 화면 (WEB_TOKEN 쿠키)
    U->>A: GET /web/flow, /web/wiki
    A->>S: /flow/stats: events, wiki_ingests, notifications, calendar_items, agent_turns 집계 (5초마다)
    A->>S: /wiki/graph: data/wiki 페이지와 링크, git 이력의 처음 커밋 시각 (30초마다)
    A-->>U: 흐름 애니메이션, 위키 그래프
```

| status | 뜻 |
|---|---|
| `pending` | 판별 대기 |
| `processing` | 워커가 판별 중 |
| `kept` | 쓸모 있음. Buzz 알림과 LLM 위키로 넘어간다 |
| `discarded` | 노이즈. 여기서 끝 |
| `failed` | 재시도를 다 써도 실패. 사람이 확인해야 함 |
