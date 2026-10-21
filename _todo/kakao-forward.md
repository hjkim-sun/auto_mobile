# 카카오톡 메시지 전송

상태: 진행 중(2026-10-21). 서버 `POST /v1/ingest/kakao`, 폰 `KakaoTalk forward` 플로우

## 조사 결과 (맥에서 adb로 확인)

- 폰은 루팅돼 있지 않다(`su` 없음). 카카오톡 DB(`/data/data/com.kakao.talk`)는 읽을 수 없다.
- 카카오톡(26.8.3)에 알림 채널 `quiet_new_message`가 있다. 이름은 "알림 받지 않는 메시지"이고 설명은
  "알림을 꺼둔 채팅방이나 방해금지 시간대 설정 시 받은 메시지에 대한 알림"이다. 중요도는 LOW(2)로 소리와 팝업이 없다.
  알림을 꺼 둔 단톡방 메시지도 이 채널로 알림이 올라온다. 그래서 알림 리스너로 받을 수 있다.
  일반 방은 `new_message_v6` 채널을 쓴다.
- 알림은 MessagingStyle이다. 방마다 알림 하나를 갱신한다(`tag` = 채팅방 id).
  - `android.title`: 방 이름
  - `android.isGroupConversation`: 단톡 여부
  - `android.messages[]`: `sender`, `text`(링크까지 전체), `time`(epoch ms), `extras.chatLogId`(메시지 고유 id)
  - `chatLogId`가 있어 서버에서 중복을 거를 수 있다.

## Automate 로 받아 보니 (2026-10-21)

- `Notification posted?` 의 Extras 에 `android.messages` 가 없다(Bundle 배열은 넘기지 않는다). 마지막 메시지의 `chatLogId` 가 extras 최상위에 온다. 알림 갱신 한 번이 메시지 하나다.
- `android.title` 은 방 이름이 아니라 마지막 메시지를 보낸 사람이다. ticker 는 `보낸사람 : 본문`. 단톡방 이름은 알림 어디에도 없다(shortcut 라벨에만 있다).
- `isGroupConversation` 은 1:1 방도 true 다. 알림 id 는 모든 방이 2 이고 tag(방 id)는 Automate 가 주지 않는다.
- 그래서 오픈채팅은 채널로 거른다: 카톡에서 오픈채팅방 알림을 끄고 `new_message_v6` 만 받는다.
- Automate 숫자는 double 이라 19자리 chatLogId 의 끝자리가 뭉개진다(512 단위). 메시지마다 id 가 크게 벌어져 중복 판정에는 문제없다.
- `when` 은 초 단위(소수)다.
- 방마다 알림을 묶는 요약 알림(GROUP_SUMMARY)도 함께 올라온다. 제목과 본문이 비어 있어 서버가 무시한다.

## 주의

- 채널 `quiet_new_message`를 Android 설정에서 끄면 알림이 아예 올라오지 않아 받을 수 없다. 무음이나 최소화는 괜찮다.
- 폰에서 해당 방을 열어 보고 있으면 카카오톡이 알림을 올리지 않는다.
- PC/맥 카카오톡이 켜져 있을 때 모바일 알림을 끄는 설정이 켜져 있으면 놓칠 수 있다. 확인해야 한다.
- 사진, 이모티콘, 파일은 알림에 대체 문구만 온다.

## 대안 (알림 방식이 부족할 때)

- 맥 카카오톡 로컬 DB를 복호화해 읽는다. 오픈소스 키 유도 방식이 있지만 버전이 바뀌면 깨지기 쉽다.
- 비공식 LOCO 클라이언트는 계정 정지 위험이 있어 쓰지 않는다.
