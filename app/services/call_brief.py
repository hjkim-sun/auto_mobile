"""전화가 오면 그 사람과 나눈 카톡·문자를 모아 최근 주제를 3줄로 요약한다 (POST /v1/brief/call).

폰 Automate 가 전화 수신을 잡아 발신 번호와 연락처 이름을 보내고, 응답의 text 를 안드로이드 알림으로 띄운다.
벨이 울리는 동안 답해야 하므로 미리 만들어 두지 않고 그때 claude -p(BRIEF_MODEL, 도구 없음)로 요약한다.
- 문자: events 의 message 중 발신 번호(from)가 같은 것. 번호는 숫자만 남기고 +82 를 0 으로 바꿔 비교한다.
- 카톡: rooms.json 의 1:1 방 중 이름이 연락처 이름과 같은 방(이름이 없으면 같은 번호의 지난 통화녹음에 붙은 이름), 그리고 그 사람이 참여한 단톡방에서 그 사람이 쓴 말.
BRIEF_DAYS 안의 것만, 각각 최근 BRIEF_MAX_MESSAGES 건까지 넣는다. 대화가 없으면 claude 를 부르지 않는다.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from contextlib import closing
from datetime import datetime, timedelta

from app.core.config import get_settings
from app.db.session import connect
from app.services import kakao_history

logger = logging.getLogger(__name__)

GROUP_MESSAGES = 10   # 단톡방에서 그 사람이 쓴 말은 이만큼만 넣는다


class BriefError(RuntimeError):
    pass


def normalize_number(number: str | None) -> str:
    digits = re.sub(r"\D", "", number or "")
    return "0" + digits[2:] if digits.startswith("82") and len(digits) > 9 else digits


def name_for_number(number: str) -> str:
    """폰이 이름을 못 보냈을 때, 같은 번호의 지난 통화녹음에 붙은 연락처 이름."""
    target = normalize_number(number)
    if not target:
        return ""
    with closing(connect()) as conn:
        rows = conn.execute("SELECT json_extract(payload, '$.number') AS number, json_extract(payload, '$.name') AS name"
                            " FROM events WHERE source = 'call' AND json_extract(payload, '$.name') IS NOT NULL"
                            " ORDER BY id DESC LIMIT 2000").fetchall()
    return next((r["name"] for r in rows if normalize_number(r["number"]) == target), "")


def sms_lines(number: str, since: str, limit: int) -> list[str]:
    """발신 번호가 number 인 문자 (오래된 것부터). 폰은 받은 문자만 보내므로 내가 보낸 문자는 없다."""
    target = normalize_number(number)
    if not target:
        return []
    with closing(connect()) as conn:
        rows = conn.execute("SELECT payload FROM events WHERE source = 'message' ORDER BY id DESC LIMIT 5000").fetchall()
    out = []
    for row in rows:
        msg = json.loads(row["payload"])
        when = msg.get("date_text") or ""
        if normalize_number(msg.get("from")) == target and when >= since and msg.get("text"):
            out.append(f"{when[:16]} 문자: {msg['text']}")
            if len(out) >= limit:
                break
    return sorted(out)


def kakao_lines(name: str, since: str, limit: int) -> list[str]:
    """name 과의 1:1 카톡과 단톡방에서 name 이 쓴 말 (오래된 것부터)."""
    if not name:
        return []
    rooms = kakao_history.load_rooms()
    out = []
    for room in rooms.values():
        if room["kind"] == "personal" and room["name"] == name:
            rows = kakao_history.read_rows(kakao_history.kakao_dir() / room["file"])
            out += [f"{r[0][:16]} 카톡 {r[1]}: {r[2]}" for r in rows if r[0] >= since][-limit:]
    for cid in kakao_history.groups_of(name, rooms):
        room = rooms[cid]
        rows = kakao_history.read_rows(kakao_history.kakao_dir() / room["file"])
        out += [f"{r[0][:16]} 카톡 단톡 \"{room['name']}\" {r[1]}: {r[2]}"
                for r in rows if r[0] >= since and r[1] == name][-GROUP_MESSAGES:]
    return sorted(out)


def summarize(name: str, conversation: list[str]) -> str:
    s = get_settings()
    prompt = "\n".join([s.brief_prompt_path.read_text(encoding="utf-8"), "",
                        f"지금: {datetime.now(s.tz):%Y-%m-%d %H:%M}", f"전화 건 사람: {name}", "",
                        "<conversation>", *conversation, "</conversation>"])
    cmd = [str(s.wiki_claude_path), "-p", "--safe-mode", "--restricted", "--tools", "",
           "--no-session-persistence", "--output-format", "json", "--model", s.brief_model, "--effort", s.brief_effort,
           "--settings", '{"alwaysThinkingEnabled": false}']
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              timeout=s.brief_timeout_seconds, cwd=s.data_dir)
    except subprocess.TimeoutExpired as e:
        raise BriefError(f"claude timed out after {s.brief_timeout_seconds}s") from e
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise BriefError(f"claude exit {proc.returncode}: {(proc.stderr or proc.stdout)[-500:]}") from None
    if proc.returncode != 0 or out.get("is_error"):
        raise BriefError(f"claude exit {proc.returncode}: {str(out.get('result'))[-500:]}")
    return out["result"].strip()


def brief(number: str | None, name: str | None) -> dict:
    """{title, text, sms, kakao, elapsed}. text 는 알림 본문(3줄 요약)이다."""
    s = get_settings()
    started = time.monotonic()
    # 폰 Content query 는 "이름, 010-1234-5678" 처럼 번호를 붙여 보낸다
    name = re.sub(r",\s*[+\d][\d\s()-]*$", "", (name or "").strip()) or name_for_number(number or "")
    who = name or (number or "").strip() or "모르는 번호"
    since = (datetime.now(s.tz) - timedelta(days=s.brief_days)).strftime("%Y-%m-%d %H:%M:%S")
    sms = sms_lines(number or "", since, s.brief_max_messages)
    kakao = kakao_lines(name, since, s.brief_max_messages)
    if not sms and not kakao:
        text = f"최근 {s.brief_days}일 동안 나눈 카톡·문자가 없다."
    else:
        try:
            text = summarize(who, sorted(sms + kakao))
        except BriefError as e:
            logger.warning("call brief for %s failed: %s", who, e)
            last = sorted(sms + kakao)[-1]
            text = f"요약 실패. 마지막 대화: {last[:120]}"
    elapsed = round(time.monotonic() - started, 2)
    logger.info("call brief for %s: sms=%d kakao=%d %.2fs", who, len(sms), len(kakao), elapsed)
    return {"title": f"📞 {who}", "text": text, "sms": len(sms), "kakao": len(kakao), "elapsed": elapsed}
