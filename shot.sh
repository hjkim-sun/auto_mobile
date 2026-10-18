#!/bin/sh
# 폰 화면을 캡처해 절반 크기로 줄여 shots/cur.png 로 저장
cd "$(dirname "$0")"
adb exec-out screencap -p > shots/full.png && sips -Z 1170 shots/full.png --out shots/cur.png >/dev/null
