#!/bin/sh
# 현재 화면의 UI 요소(text/resource-id/bounds) 덤프
adb shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1
adb exec-out cat /sdcard/ui.xml | python3 -c '
import sys,re
x=sys.stdin.read()
for m in re.finditer(r"<node [^>]*>",x):
    n=m.group(0)
    g=lambda k:(re.search(k+r"=\"([^\"]*)\"",n) or [None,""])[1]
    t,i,d,b=g("text"),g("resource-id").split("/")[-1],g("content-desc"),g("bounds")
    if t or d or (i and g("clickable")=="true"): print(repr(t),i,repr(d),b)
'
