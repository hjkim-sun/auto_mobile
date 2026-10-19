# auto_mobile

## 목적

폰에서 생기는 데이터(통화녹음, SMS/MMS)와 메일(naver, gmail)을 서버 한 곳에 모으고, Jev(typesafe.ai)로 쓸모 있는 것(`kept`)과 노이즈(`discarded`)를 가려낸다. 걸러낸 데이터를 다음 단계(요약, 알림 등)에 넘기는 것이 최종 목표다.

```
폰 Automate 앱 ──HTTPS + Bearer──▶ Caddy :443 ──▶ uvicorn 127.0.0.1:8080 (app/)
                                                    └ data/raw/ 원본 저장 + events 테이블 1행
                                   워커 (app/jobs/worker.py) ── pending 이벤트 → Jev 판별
```

API, 저장 경로, 실행 방법은 README.md에 정리돼 있다.

## 운영 환경 (VPS)
- 개발 환경은 별도로 없다. 이 환경에서 개발을 수행한다. 
- 코드 `/home/automan/auto_mobile`, 설정 `/home/automan/auto_mobile/.env`(git 제외, 600), 데이터 `/home/automan/auto_mobile/data/`
- systemd 서비스
  - `auto-mobile-api`: uvicorn, `127.0.0.1:8080`에서만 받는다
  - `auto-mobile-worker`: 판별 워커. 반드시 1개만 띄운다
  - `caddy`: HTTPS 종단. Let's Encrypt 인증서를 자동으로 발급하고 갱신한다. 설정은 `/etc/caddy/Caddyfile`
- `automan` 계정으로 운영한다. 서비스도 `User=automan`으로 돈다. root에서는 `claude --dangerously-skip-permissions`를 쓸 수 없어서 일반 계정으로 옮겼다.
- automan은 `sudo -n systemctl start|stop|restart auto-mobile-api|auto-mobile-worker`, `sudo -n systemctl reload|restart caddy`만 비밀번호 없이 쓸 수 있다(`/etc/sudoers.d/automan`). 그 밖의 root 작업(Caddyfile·systemd unit 수정, apt 등)은 사용자에게 요청한다.
- 배포: `git pull && uv sync --frozen && sudo -n systemctl restart auto-mobile-api auto-mobile-worker`
- `.env`의 `INGEST_TOKEN`은 폰 Automate 블록 헤더에 같은 값이 들어 있다. 바꾸면 폰도 함께 바꿔야 한다(아래 참고).
- 저장소가 public이다. 토큰, 실제 전화번호, 이름, 문자 내용을 커밋하지 않는다. 테스트 데이터는 `01012345678`, `홍길동` 같은 가짜 값을 쓴다.

## 폰 Automate 연동

수신 데이터는 모두 폰의 Automate 앱(LlamaLab) 플로우가 보낸다. 폰에서 보내는 요청 형식이 바뀌면 서버만 고쳐서는 끝나지 않는다.

| 플로우 | 블록 | 요청 |
|---|---|---|
| SMS MMS forward | 10 (SMS 본문) | `POST /v1/ingest/messages` |
| SMS MMS forward | 16 (MMS 본문) | `POST /v1/ingest/messages` |
| SMS MMS forward | 19 (MMS 첨부) | `PUT /v1/ingest/messages/mms-{id}/attachments/{part}?filename=` |
| 통화녹음전송 | 3 | `POST /v1/ingest/calls?filename=` |

4개 블록 모두 Request headers에 `{"Authorization": "Bearer <INGEST_TOKEN>"}`이 들어 있다.

### 폰 조작은 VPS의 Claude가 할 수 없다

폰은 사용자의 맥에 USB/adb(scrcpy)로 연결돼 있다. 폰을 조작하는 도구(`ui.py`, `ui.sh`, `goto.sh`, `add.sh`, `shot.sh`, `t.sh`)는 **맥에서 실행 중인 Claude Code 세션만** 쓸 수 있다. VPS에는 adb로 연결된 기기가 없다.

VPS의 Claude가 지켜야 할 것:
- adb나 위 스크립트를 실행하지 않는다. 실행해도 동작하지 않는다.
- 엔드포인트 경로, 메서드, 요청 body 형식, 인증 방식, 토큰, 도메인을 바꾸면 폰 Automate 블록도 바꿔야 한다. 이런 변경은 하위 호환을 유지하거나, 작업을 끝낸 뒤 사용자에게 "맥의 Claude에게 폰 Automate 블록 수정을 요청해야 한다"고 알리고 바꿀 블록과 값을 정리해 준다.
- 폰 쪽 동작은 서버 로그(`journalctl -u auto-mobile-api`)와 `data/events.db`로만 확인할 수 있다.

### 맥의 Claude가 폰을 조작할 때 참고

- `./shot.sh`로 화면을 캡처한다(`shots/cur.png`, git 제외). `./ui.sh`로 UI 요소를 덤프한다.
- 블록 편집 화면에서는 `./ui.py dump`로 행을 보고 `./ui.py set <행> <값> [fx]`로 입력한 뒤 `./ui.py tap SAVE`로 저장한다.
- `./goto.sh <블록번호>`로 플로우차트에서 블록으로 이동한다. 이동한 블록은 대개 (540, 796) 근처에 오지만, 차트 끝 블록은 가운데로 오지 않으므로 캡처해서 위치를 확인한다.
- 실행 중인 플로우를 편집하면 플로우가 멈춘다("Stopped by user"). 편집을 마치면 플로우 화면에서 START로 다시 시작하고, 목록에서 `running fibers` 아이콘으로 확인한다.
- 편집기 밖에서 `goto.sh`를 실행하면 플로우 메뉴(Duplicate/Delete)가 열린다. Delete를 누르지 않게 주의한다.

## git 원칙
작업 후에는 항상 commit 을 수행한다. 

