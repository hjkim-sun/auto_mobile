# 메일 폴러를 systemd 서비스로 등록하기 (root 작업)

상태: 대기 (사용자가 root로 진행)

`app/jobs/mail_poller.py`는 구현돼 있지만 아직 서비스로 등록하지 않았다.
automan 계정은 unit 파일과 sudoers를 고칠 수 없으므로 아래 작업은 root로 한다.

## 먼저 할 일 (root 아님)

1. 네이버 메일 환경설정 → POP3/IMAP 설정에서 IMAP을 켠다. 2단계 인증을 쓰면 애플리케이션 비밀번호를 발급한다.
2. `.env`에 `NAVER_IMAP_USER`, `NAVER_IMAP_PASSWORD`를 넣는다.
3. `uv run python -m app.jobs.mail_poller --once`로 한 번 돌려 로그인과 수신이 되는지 확인한다.

## root 작업

### 1. unit 파일 만들기

`/etc/systemd/system/auto-mobile-mail-poller.service`

```ini
[Unit]
Description=auto_mobile IMAP mail poller (single instance)
After=network-online.target
Wants=network-online.target

[Service]
User=automan
Group=automan
WorkingDirectory=/home/automan/auto_mobile
ExecStart=/home/automan/auto_mobile/.venv/bin/python -m app.jobs.mail_poller
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 2. 등록하고 시작하기

```bash
systemctl daemon-reload
systemctl enable --now auto-mobile-mail-poller
systemctl status auto-mobile-mail-poller
```

### 3. sudoers에 추가하기

`visudo -f /etc/sudoers.d/automan`으로 열고, 기존 `auto-mobile-api`, `auto-mobile-worker` 규칙과 같은 형식으로
`auto-mobile-mail-poller`의 `start|stop|restart`를 추가한다. 예:

```
automan ALL=(root) NOPASSWD: /usr/bin/systemctl start auto-mobile-mail-poller, /usr/bin/systemctl stop auto-mobile-mail-poller, /usr/bin/systemctl restart auto-mobile-mail-poller
```

## 끝나면 (VPS의 Claude)

1. `journalctl -u auto-mobile-mail-poller`에서 폴링 로그를 확인한다.
2. CLAUDE.md의 systemd 서비스 목록과 배포 명령에 `auto-mobile-mail-poller`를 추가한다.
3. 이 파일을 지우고 커밋한다.
