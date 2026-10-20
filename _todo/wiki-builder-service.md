# LLM 위키 빌더를 systemd 서비스로 등록하기 (root 작업)

상태: 대기. `_todo/root-setup.sh` 를 root 로 실행하면 된다

`app/jobs/wiki_builder.py`는 구현돼 있고 `--once`로 기존 kept를 넣어 봤다. 계속 돌리려면 서비스로 등록한다.
claude가 automan의 `~/.claude` 로그인 정보를 쓰므로 `User=automan`으로 돌린다.

`/etc/systemd/system/auto-mobile-wiki.service`

```ini
[Unit]
Description=auto_mobile LLM wiki builder (single instance)
After=network-online.target
Wants=network-online.target

[Service]
User=automan
Group=automan
WorkingDirectory=/home/automan/auto_mobile
Environment=PATH=/home/automan/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/home/automan/auto_mobile/.venv/bin/python -m app.jobs.wiki_builder
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now auto-mobile-wiki
```

`visudo -f /etc/sudoers.d/automan`으로 열고 `auto-mobile-wiki`의 `start|stop|restart`를 기존 규칙과 같은 형식으로 추가한다.
배포 명령에도 `auto-mobile-wiki`를 넣는다: `sudo -n systemctl restart auto-mobile-api auto-mobile-worker auto-mobile-wiki`
