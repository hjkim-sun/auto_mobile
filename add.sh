#!/bin/sh
# 블록 추가: $1=검색어, $2=블록 이름(목록의 정확한 텍스트). 직전 블록 OK → 새 블록 IN 연결 후 설정 화면 열기
cd "$(dirname "$0")"
sleep 1.5; ./ui.py tap Add || exit 1; sleep 1
./ui.py tap 'Clear query' 2 2>/dev/null
./ui.py tap 'Search blocks…' 4 2>/dev/null
./t.sh "$1"; sleep 1
./ui.py tap "$2" || exit 1; sleep 1.5
case "$3" in noconnect) ;; do) adb shell input swipe 728 892 540 1143 800; sleep 0.8 ;; *) adb shell input swipe 540 1017 540 1143 800; sleep 0.8 ;; esac
adb shell input tap 540 1270; sleep 1.5
