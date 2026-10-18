#!/bin/sh
# 편집기에서 블록 번호로 이동 (화면 중앙 정렬)
cd "$(dirname "$0")"
./ui.py tap '옵션 더보기'; sleep 0.8
./ui.py tap 'Go to'; sleep 0.8
adb shell input text "$1"; ./ui.py tap 'GO TO'; sleep 1.2
