#!/bin/sh
# 폰에 텍스트 입력 (공백/따옴표 이스케이프 처리)
python3 - "$1" <<'PY'
import subprocess, sys
s = sys.argv[1].replace(" ", "%s")
q = "'" + s.replace("'", "'\\''") + "'"
subprocess.run(["adb", "shell", "input text " + q], check=True)
PY
