#!/usr/bin/env python3
"""Automate 블록 편집 화면 조작 도우미.

ui.py dump                 폼 행(row) 목록: 행번호, 입력칸 종류, 현재 값
ui.py tap <텍스트>          해당 텍스트(또는 content-desc) 요소 탭
ui.py set <행> <값> [fx]   해당 행 입력칸에 값 입력. fx 면 먼저 수식 모드로 전환
ui.py fx <행>              해당 행의 수식 모드 토글
ui.py click <행>           해당 행 입력칸 탭(스피너/버튼 열기용)
"""
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

INPUT_IDS = ("edit_text", "edit_expression", "spinner", "button", "edit_variable", "checkbox")
SCREEN_TOP, SCREEN_BOTTOM = 240, 2220


def sh(*args):
    return subprocess.run(["adb", "shell", *args], capture_output=True, text=True).stdout


def rid(n):
    return n.get("resource-id", "").split("/")[-1]


def bounds(n):
    x1, y1, x2, y2 = map(int, re.findall(r"\d+", n.get("bounds")))
    return x1, y1, x2, y2


def center(n):
    x1, y1, x2, y2 = bounds(n)
    return (x1 + x2) // 2, (y1 + y2) // 2


def root():
    sh("uiautomator dump /sdcard/ui.xml")
    xml = subprocess.run(["adb", "exec-out", "cat /sdcard/ui.xml"], capture_output=True, text=True).stdout
    return ET.fromstring(xml)


def rows(r):
    """폼(스크롤 컨테이너 안 LinearLayout)의 행들 → {행번호: (행노드, 입력칸, 수식토글)}"""
    form = next(n for n in r.iter("node") if rid(n) == "fields")
    res = {}
    for row in form:
        i = int(row.get("index"))
        field = next((n for n in row.iter("node") if rid(n) in INPUT_IDS), None)
        fx = next((n for n in row.iter("node") if rid(n) == "expression_mode"), None)
        res[i] = (row, field, fx)
    return res


def tap(x, y, wait=0.6):
    sh(f"input tap {x} {y}")
    time.sleep(wait)


def keyboard_shown():
    return "mInputShown=true" in sh("dumpsys input_method")


def hide_keyboard():
    if keyboard_shown():
        sh("input keyevent KEYCODE_BACK")
        time.sleep(0.5)


def type_text(s):
    s = s.replace(" ", "%s")
    sh("input text '" + s.replace("'", "'\\''") + "'")


def visible_row(i):
    """행 i 가 화면 안에 충분히 보이도록 스크롤한 뒤 (행, 입력칸, 수식토글) 반환."""
    for _ in range(8):
        rs = rows(root())
        if i not in rs:   # 화면 밖 행은 덤프에 없음: 보이는 행 번호로 방향 결정
            sh("input swipe 540 1500 540 900 300" if i > max(rs) else "input swipe 540 900 540 1500 300")
            time.sleep(0.6)
            continue
        row, field, fx = rs[i]
        x1, y1, x2, y2 = bounds(row)
        if y2 - y1 > 40 and y1 >= SCREEN_TOP and y2 <= SCREEN_BOTTOM:
            return row, field, fx
        if y2 - y1 > 40 and y1 < SCREEN_TOP or y2 <= SCREEN_TOP + 10:
            sh("input swipe 540 900 540 1500 300")   # 위쪽 행 → 아래로 끌기
        else:
            sh("input swipe 540 1500 540 900 300")
        time.sleep(0.6)
    raise SystemExit(f"row {i} not visible")


def main():
    cmd = sys.argv[1]
    if cmd == "dump":
        hide_keyboard()
        for i, (row, field, fx) in rows(root()).items():
            label = next((n.get("text") for n in row.iter("node") if n.get("text")), "")
            kind = rid(field) if field is not None else "-"
            print(i, kind, repr(label), bounds(row), "fx" if fx is not None else "")
        return
    if cmd == "tap":
        for _ in range(int(sys.argv[3]) if len(sys.argv) > 3 else 10):
            n = next((n for n in root().iter("node")
                      if sys.argv[2] in (n.get("text"), n.get("content-desc"))), None)
            if n is not None:
                tap(*center(n))
                return
            time.sleep(0.5)
        raise SystemExit(f"not found: {sys.argv[2]}")
        return

    i = int(sys.argv[2])
    hide_keyboard()
    row, field, fx = visible_row(i)
    if cmd == "fx":
        tap(*center(fx))
    elif cmd == "click":
        tap(*center(field))
    elif cmd == "set":
        if len(sys.argv) > 4 and sys.argv[4] == "fx" and rid(field) != "edit_expression":
            tap(*center(fx))
            row, field, fx = visible_row(i)
        x, y = center(field)
        tap(x - 100, y)
        sh("input keycombination KEYCODE_CTRL_LEFT KEYCODE_A")
        sh("input keyevent KEYCODE_DEL")
        type_text(sys.argv[3])
        time.sleep(0.4)
        hide_keyboard()
        tap(540, 150)   # 자동완성 팝업 닫기 (제목 영역 탭)


if __name__ == "__main__":
    main()
