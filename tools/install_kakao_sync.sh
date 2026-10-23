#!/bin/sh
# 맥 launchd 에 tools/kakao_sync.py 를 1분마다 돌도록 등록한다. 다시 실행하면 설정을 바꿔 다시 등록한다.
# 사용: tools/install_kakao_sync.sh <내 카카오 userId> <서버 주소> [days=2] [interval초=60]
# 지우기: launchctl bootout gui/$(id -u)/com.auto_mobile.kakao_sync && rm ~/Library/LaunchAgents/com.auto_mobile.kakao_sync.plist
# 로그: ~/Library/Logs/kakao_sync.log
set -eu
[ $# -ge 2 ] || { sed -n 3p "$0"; exit 1; }
LABEL=com.auto_mobile.kakao_sync
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$(command -v python3)"
KAKAOCLI="${KAKAOCLI:-$HOME/.local/bin/kakaocli}"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string><string>$REPO/tools/kakao_sync.py</string><string>--days</string><string>${3:-2}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>KAKAOCLI_USER_ID</key><string>$1</string>
    <key>INGEST_URL</key><string>$2</string>
    <key>KAKAOCLI</key><string>$KAKAOCLI</string>
  </dict>
  <key>StartInterval</key><integer>${4:-60}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/kakao_sync.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/kakao_sync.log</string>
</dict>
</plist>
PLIST
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "registered $LABEL (every ${4:-60}s, --days ${3:-2}); log: ~/Library/Logs/kakao_sync.log"
