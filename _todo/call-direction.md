# 통화녹음에 수신/발신 여부, 상대 번호, 연락처 이름 붙이기

상태: 완료. 2026-10-21 실제 수신/발신 녹음으로 확인했다(정보 붙는 경로, 연락처에 없는 번호는 name 없음)

## 폰에 넣은 것 (2. Call recording upload 플로우)

`4 Monitor file → 6 Delay 5s → 5 Content query → 3 HTTP request → 4`

- 블록 6: 녹음 파일이 닫힌 직후에는 통화 기록 행이 아직 없을 수 있어 5초 기다린다.
- 블록 5: `content://call_log/calls`, 컬럼 `["type", "number", "name", "date", "duration"]`,
  Where `type = 1 OR type = 2`, Order `date DESC`, Limit 1, Rows as dictionaries, Column result types `Text`, 출력 `calls`.
  Where 에 SQL 함수(`julianday`, `strftime`)를 쓰면 일반 앱 권한에서는 `IllegalArgumentException: Invalid token`
  으로 실패한다(adb shell 에서는 통과해서 시험으로 걸러지지 않는다). 2026-10-21 이 때문에 플로우가 멈췄다.
- 블록 3 URL: 가장 최근 통화가 3분 안에 끝났을 때만 정보를 붙인다(이전 통화 정보를 잘못 붙이지 않게).
  `... ++ urlEncode(path) ++ (#calls && calls[0]["date"] + calls[0]["duration"] * 1000 >= now * 1000 - 180000 ? "&direction=" ++ {"1": "incoming", "2": "outgoing"}[calls[0]["type"]] ++ "&number=" ++ urlEncode(calls[0]["number"]) ++ "&name=" ++ urlEncode(calls[0]["name"]) ++ "&call_date=" ++ calls[0]["date"] : "")`
- Automate 에 `READ_CALL_LOG` 를 `adb shell pm grant` 로 허용했다.

## 배경

- 삼성 통화녹음 파일(`.m4a`, 모노 AAC 48kHz)에는 수신/발신 정보가 없다.
  - 메타데이터에는 `creation_time`(녹음 종료 무렵), `com.android.version`, `com.samsung.android.utc_offset`만 들어 있다.
  - 파일명 `통화 {상대 이름 또는 번호}_{YYMMDD}_{HHMMSS}.m4a`의 시각은 녹음 시작 시각이다.
- Android 통화 기록 `content://call_log/calls`에는 통화마다 `type`, `number`, `name`(연락처에 저장된 이름, 없으면 NULL), `date`(통화 시작, epoch ms), `duration`(초)이 있다.
  폰에서 이 정보를 찾아 업로드 요청에 붙여 보낸다.

`type` 값: 1 INCOMING, 2 OUTGOING, 3 MISSED, 4 VOICEMAIL, 5 REJECTED, 6 BLOCKED, 7 ANSWERED_EXTERNALLY.
녹음이 생기는 통화는 사실상 1과 2뿐이다.

## 요청 형식 (변경 후)

```
POST /v1/ingest/calls?filename=<파일명>&direction=<incoming|outgoing|unknown>&number=<번호>&name=<연락처 이름>&call_date=<epoch ms>
```

- `direction`, `number`, `name`, `call_date`는 모두 **선택**이다. 빠지면 지금처럼 동작한다.
- FastAPI는 모르는 쿼리를 무시한다. 그래서 서버와 폰 중 어느 쪽을 먼저 바꿔도 깨지지 않는다.
  서버를 먼저 배포하는 쪽이 안전하다(먼저 보낸 값이 버려지지 않는다).

## 서버 작업 (VPS의 Claude)

1. `app/api/ingest.py` `ingest_call`에 선택 쿼리를 추가한다.
   - `direction: Literal["incoming", "outgoing", "unknown"] | None`
   - `number: str | None` (숫자, `+`, `-`만 허용하는 pattern)
   - `name: str | None` (연락처 이름, 길이 제한)
   - `call_date: int | None` (epoch ms)
2. `app/services/ingest.py` `ingest_call`이 이 값을 payload에 넣는다.
   - payload 키: `direction`, `number`, `name`, `call_date`, `call_date_text`(`format_date` 재사용).
   - 중복 수신(같은 sha256)인데 기존 payload에 `direction`이 없고 새 요청에는 있으면, 기존 payload를 갱신한다.
     값을 붙이기 전에 받은 녹음을 폰에서 다시 보내 보강할 수 있게 하려는 것이다. `events.update_payload` 같은 함수를 추가한다.
3. 전사 결과 `.json`에 `direction`과 `number`를 함께 적을지 정한다. 판별 단계에서 쓰기 편한 쪽을 고른다.
4. 통화 판별(`app/services/render.py`의 call 분기, 현재 `None`)을 만들 때 state 맨 위에 넣는다.
   예: `[CALL] 수신 / 상대: 홍길동 (01012345678) / 시각: ...`
   (전사 텍스트를 판별에 넣는 작업과 함께 하거나 그 뒤에 한다.)
5. 테스트(`tests/test_ingest.py`)
   - 파라미터가 있으면 payload에 저장된다.
   - 없으면 기존과 같다(하위 호환).
   - 잘못된 `direction`은 422가 난다.
   - 중복 수신으로 보강된다.
   - 가짜 번호 `01012345678`만 쓴다.
6. README의 API 표와 설명을 갱신한다.
7. 배포한 뒤 커밋한다.

## 폰 작업 (맥의 Claude에게 요청. VPS에서는 불가)

대상: Automate `2. Call recording upload` 플로우, 블록 3(`POST /v1/ingest/calls?filename=`).

1. Automate 앱에 통화 기록 읽기 권한(`READ_CALL_LOG`)이 있는지 확인한다. 없으면 허용한다.
2. 블록 3 앞에 **Content query** 블록을 넣는다.
   - Content URI: `content://call_log/calls`
   - Projection: `type`, `number`, `name`, `date`, `duration`
   - Selection: 녹음 시작 시각(파일명의 `_YYMMDD_HHMMSS`를 파싱하거나 파일 수정 시각을 쓴다)과 겹치는 통화를 고른다.
     `date <= 녹음시각 + 60000 AND date + duration*1000 >= 녹음시각 - 60000`
   - Order: `date DESC`, 첫 행만 쓴다.
3. `type`을 변환한다: 1이면 `incoming`, 2면 `outgoing`, 나머지나 결과 없음이면 `unknown`.
4. 블록 3 URL에 `&direction=...&number=...&name=...&call_date=...`를 붙인다(URL 인코딩).
   기존 `Authorization` 헤더는 그대로 둔다.
5. 편집하면 플로우가 멈추므로 START로 다시 시작하고 `running fibers`로 확인한다.
6. 확인: 테스트 통화를 수신 1건, 발신 1건 한 뒤 VPS에서 확인한다.
   `journalctl -u auto-mobile-api` 로그와 `data/events.db`의 call payload에 `direction`이 맞게 들어왔는지 본다.

## 폰 확인 결과 (2026-10-20, 맥에서 adb로 확인)

- Automate 권한: `READ_CONTACTS`는 허용돼 있고 `READ_CALL_LOG`는 **허용 안 됨**. 폰 작업 1단계에서 허용해야 한다.
- 통화 기록 `name` 컬럼은 최근 30건 중 27건에 값이 있었다. 연락처에 없는 번호만 NULL이다.
  따로 `content://com.android.contacts/phone_lookup`을 조회하지 않아도 된다.
- 실제 녹음 1건 비교: 수신 통화 기록 `date`가 파일명 시각보다 6초 빨랐다(벨 울린 시각 vs 받은 시각). ±60초 창으로 충분하다.

## 열린 질문

- 녹음 시각과 통화 기록이 어긋날 때 허용 폭 ±60초가 충분한가. 실제 녹음 몇 건으로 확인한다.
- 같은 번호와 짧은 간격으로 통화가 이어지면 엉뚱한 통화와 짝지어질 수 있다. 녹음 길이와 `duration`을 비교해 보정할지 정한다.
