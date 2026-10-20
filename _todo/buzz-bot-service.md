# Buzz 봇을 systemd 서비스로 등록하기 (root 작업)

상태: 대기 (사용자가 커뮤니티 초대를 마친 뒤 root로 진행)

`app/jobs/buzz_bot.py`는 구현돼 있다. automan 계정은 unit 파일과 sudoers를 고칠 수 없으므로 아래는 root로 한다.

## 먼저 할 일 (root 아님)

1. Buzz 앱에서 봇 npub을 커뮤니티 멤버로 초대한다. 봇 npub은 `.env`의 `BUZZ_PRIVATE_KEY` 위 주석에 있다.
2. `.env`에 `BUZZ_RELAY_URL`, `BUZZ_OWNER_PUBKEY`(사용자 npub)를 넣는다.
3. `uv run python -m app.jobs.buzz_bot --whoami`로 relay 연결과 봇 pubkey를 확인한다.
4. `uv run python -m app.jobs.buzz_bot --once`로 DM이 열리는지 본다.

## root 작업

`/etc/systemd/system/auto-mobile-buzz.service`

```ini
[Unit]
Description=auto_mobile Buzz notification bot (single instance)
After=network-online.target
Wants=network-online.target

[Service]
User=automan
Group=automan
WorkingDirectory=/home/automan/auto_mobile
ExecStart=/home/automan/auto_mobile/.venv/bin/python -m app.jobs.buzz_bot
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now auto-mobile-buzz
```

`visudo -f /etc/sudoers.d/automan`으로 열고 `auto-mobile-buzz`의 `start|stop|restart`를 기존 규칙과 같은 형식으로 추가한다.
