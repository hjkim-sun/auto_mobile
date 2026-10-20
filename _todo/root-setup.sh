#!/usr/bin/env bash
# root 로 한 번 실행한다: bash /home/automan/auto_mobile/_todo/root-setup.sh
# Buzz relay 준비(docker 권한, Caddy :8443, 방화벽), 메일 폴러·위키 빌더 서비스, Buzz 봇 서비스 unit, sudoers.
# 여러 번 실행해도 같은 결과가 나온다.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "root 로 실행하세요"; exit 1; }

echo "== 1. automan 을 docker 그룹에 추가"
usermod -aG docker automan

echo "== 2. Caddy 에 :8443 (Buzz relay) 추가"
if ! grep -q ':8443' /etc/caddy/Caddyfile; then
  cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak.$(date +%Y%m%d%H%M%S)
  cat >> /etc/caddy/Caddyfile <<'EOF'

srv2036064.hstgr.cloud:8443 {
	encode zstd gzip
	reverse_proxy 127.0.0.1:3000
}
EOF
fi
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl reload caddy

echo "== 3. 방화벽 8443/tcp"
if ufw status | grep -q 'Status: active'; then
  ufw allow 8443/tcp
else
  echo "ufw 비활성: 건너뜀"
fi

echo "== 4. systemd 서비스"
cat > /etc/systemd/system/auto-mobile-mail-poller.service <<'EOF'
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
EOF

cat > /etc/systemd/system/auto-mobile-wiki.service <<'EOF'
[Unit]
Description=auto_mobile LLM wiki builder and calendar (single instance)
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
EOF

cat > /etc/systemd/system/auto-mobile-buzz.service <<'EOF'
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
EOF

systemctl daemon-reload
systemctl enable --now auto-mobile-mail-poller auto-mobile-wiki
# Buzz 봇은 BUZZ_OWNER_PUBKEY 를 넣은 뒤 켠다 (relay → 초대 → pubkey 순서). 여기서는 부팅 시 자동 시작만 걸어 둔다.
systemctl enable auto-mobile-buzz

echo "== 5. sudoers: automan 이 새 서비스를 start/stop/restart"
cat > /etc/sudoers.d/automan-services <<'EOF'
automan ALL=(root) NOPASSWD: /usr/bin/systemctl start auto-mobile-mail-poller, /usr/bin/systemctl stop auto-mobile-mail-poller, /usr/bin/systemctl restart auto-mobile-mail-poller
automan ALL=(root) NOPASSWD: /usr/bin/systemctl start auto-mobile-wiki, /usr/bin/systemctl stop auto-mobile-wiki, /usr/bin/systemctl restart auto-mobile-wiki
automan ALL=(root) NOPASSWD: /usr/bin/systemctl start auto-mobile-buzz, /usr/bin/systemctl stop auto-mobile-buzz, /usr/bin/systemctl restart auto-mobile-buzz
EOF
chmod 440 /etc/sudoers.d/automan-services
visudo -cf /etc/sudoers.d/automan-services || { rm -f /etc/sudoers.d/automan-services; echo "sudoers 문법 오류: 파일을 지웠습니다"; exit 1; }

echo
echo "완료. 확인:"
systemctl is-active auto-mobile-mail-poller auto-mobile-wiki
echo
echo "남은 일: Hostinger hPanel → VPS → 방화벽을 쓰고 있다면 8443/tcp 허용 규칙 추가"
echo "그다음 Claude Code(automan)를 다시 시작해서 docker 그룹을 적용하고 Claude 에게 알린다."
