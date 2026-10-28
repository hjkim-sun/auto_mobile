"""전화가 오면 그 사람과 나눈 카톡·문자·통화의 최근 주제 3줄 요약을 돌려준다 (POST /v1/brief/call).

폰 Automate 가 전화 수신을 잡아 발신 번호와 연락처 이름을 보내고, 응답의 text 를 안드로이드 알림으로 띄운다.
요약은 미리 만들어 둔다(call_briefs 테이블). 카톡·문자·통화가 들어오면 그 사람의 is_updated 를 켜고,
워커의 brief 스레드가 BRIEF_REFRESH_SECONDS 마다 is_updated 인 사람만 claude -p(BRIEF_MODEL, 도구 없음)로 다시 요약한다.
전화가 오면 저장된 요약을 바로 돌려주고, 아직 요약이 없는 사람만 그때 요약한다.
- 문자: events 의 message 중 발신 번호(from)가 같은 것. 번호는 숫자만 남기고 +82 를 0 으로 바꿔 비교한다.
- 카톡: rooms.json 의 1:1 방 중 이름이 그 사람 이름과 같은 방, 그리고 그 사람이 참여한 단톡방에서 그 사람이 쓴 말.
- 통화: events 의 call 중 번호가 같은 것의 전사문 (최근 BRIEF_CALLS 건, 건마다 앞부분만).
BRIEF_DAYS 안의 것만, 문자·카톡은 각각 최근 BRIEF_MAX_MESSAGES 건까지 넣는다. 대화가 없으면 claude 를 부르지 않는다.
"""
from __future__ import annotations

import base64
import html
import json
import logging
import re
import subprocess
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from string import Template

from app.core.config import get_settings
from app.db.session import connect
from app.repositories import call_briefs
from app.repositories.call_briefs import clean_name, normalize_number
from app.services import kakao_history
from app.services.render import call_transcript

logger = logging.getLogger(__name__)

GROUP_MESSAGES = 10   # 단톡방에서 그 사람이 쓴 말은 이만큼만 넣는다
CALL_CHARS = 600      # 통화 전사문은 건마다 이만큼만 넣는다


class BriefError(RuntimeError):
    pass


def since() -> str:
    s = get_settings()
    return (datetime.now(s.tz) - timedelta(days=s.brief_days)).strftime("%Y-%m-%d %H:%M:%S")


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


def call_lines(number: str, since: str, limit: int) -> list[str]:
    """번호가 number 인 통화녹음의 전사문 (오래된 것부터). 전사가 끝나지 않은 통화는 뺀다."""
    target = normalize_number(number)
    if not target:
        return []
    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM events WHERE source = 'call' ORDER BY id DESC LIMIT 2000").fetchall()
    out = []
    for row in rows:
        call = json.loads(row["payload"])
        when = call.get("call_date_text") or ""
        if normalize_number(call.get("number")) != target or when < since:
            continue
        if text := " / ".join((call_transcript(row) or "").split("\n")).strip(" /"):
            out.append(f"{when[:16]} 통화: {text[:CALL_CHARS]}")
            if len(out) >= limit:
                break
    return sorted(out)


def summarize(name: str, conversation: list[str]) -> str:
    s = get_settings()
    prompt = "\n".join([s.brief_prompt_path.read_text(encoding="utf-8"), "",
                        f"지금: {datetime.now(s.tz):%Y-%m-%d %H:%M}", f"전화 건 사람: {name}", "",
                        "<conversation>", *conversation, "</conversation>"])
    cmd = [str(s.wiki_claude_path), "-p", "--safe-mode", "--restricted", "--tools", "",
           "--no-session-persistence", "--output-format", "json", "--model", s.brief_model, "--effort", s.brief_effort]
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


def make_brief(name: str, number: str, who: str) -> dict:
    """{text, sms, kakao, calls}. 요약이 실패하면 BriefError."""
    s = get_settings()
    start = since()
    sms = sms_lines(number, start, s.brief_max_messages)
    kakao = kakao_lines(name, start, s.brief_max_messages)
    calls = call_lines(number, start, s.brief_calls)
    if not sms and not kakao and not calls:
        text = f"최근 {s.brief_days}일 동안 나눈 카톡·문자·통화가 없다."
    else:
        text = summarize(who, sorted(sms + kakao + calls))
    return {"text": text, "sms": len(sms), "kakao": len(kakao), "calls": len(calls)}


def refresh() -> int:
    """is_updated 인 사람의 요약을 다시 만든다. 다시 만든 사람 수."""
    done = 0
    for row in call_briefs.claim_updated():
        name = "" if row["person"] == row["number"] else row["person"]
        try:
            b = make_brief(name, row["number"] or "", row["person"])
        except Exception:
            logger.exception("call brief for %s failed", row["person"])
            call_briefs.unclaim(row["person"])
            continue
        call_briefs.save(row["person"], b["text"], b["sms"], b["kakao"], b["calls"])
        done += 1
    return done


def seed() -> int:
    """BRIEF_DAYS 안에 대화가 있는 사람을 모두 is_updated 로 둔다. 브리핑이 하나도 없을 때(처음 띄울 때) 부른다."""
    start = since()
    people = set()
    for room in kakao_history.load_rooms().values():
        rows = kakao_history.read_rows(kakao_history.kakao_dir() / room["file"])
        if room["kind"] == "personal" and rows and max(r[0] for r in rows) >= start:
            people.add(call_briefs.mark(name=room["name"]))
    with closing(connect()) as conn:
        rows = conn.execute("SELECT source, payload FROM events WHERE source IN ('call', 'message')"
                            " ORDER BY id").fetchall()
    for row in rows:
        p = json.loads(row["payload"])
        if row["source"] == "call" and (p.get("call_date_text") or "") >= start:
            people.add(call_briefs.mark(p.get("name"), p.get("number")))
        elif row["source"] == "message" and (p.get("date_text") or "") >= start:
            people.add(call_briefs.mark(number=p.get("from")))
    return len(people - {None})


WEB = Path(__file__).resolve().parents[1] / "web"
CARD = WEB / "call_card.html"
SOURCE = re.compile(r"^\[(카톡|통화|문자)\]\s*")   # 요약 줄 앞의 출처 (prompts/call_brief.md)
ICONS = {"카톡": "kakao", "통화": "call", "문자": "sms"}   # app/web/icons/<이름>.png


def icon(source: str) -> str:
    data = base64.b64encode((WEB / "icons" / f"{ICONS[source]}.png").read_bytes()).decode()
    return f'<img src="data:image/png;base64,{data}" alt="{source}">'


def plain(text: str) -> str:
    """알림 본문용. 요약 줄 앞의 [카톡] 같은 출처를 뗀다."""
    return re.sub(r"^(• ?)?\[(?:카톡|통화|문자)\][ \t]*", r"\1", text, flags=re.M)


def card(who: str, b: dict) -> str:
    """전화 화면 위에 띄우는 카드(폰 Automate Dialog web 블록의 HTML page). 틀은 app/web/call_card.html.
    줄 앞에 출처가 있으면 카톡·통화·문자 아이콘을, 없으면(출처를 붙이기 전에 만든 요약) 번호를 띄운다."""
    if b["summarized_at"]:
        when = datetime.fromisoformat(b["summarized_at"]).astimezone(get_settings().tz)
        when = f"{when:%m.%d %H:%M} 기준"
    else:
        when = "방금 요약"
    lines = filter(None, (line.strip().removeprefix("•").strip() for line in b["text"].splitlines()))
    items = []
    for i, line in enumerate(lines, 1):
        if m := SOURCE.match(line):
            mark, line = f'<i class="src">{icon(m[1])}</i>', line[m.end():]
        else:
            mark = f"<i>{i}</i>"
        items.append(f"<li>{mark}<span>{html.escape(line)}</span></li>")
    return Template(CARD.read_text(encoding="utf-8")).substitute(
        initial=html.escape(who[:1]), name=html.escape(who), items="".join(items), when=when,
        kakao=b["kakao"] or 0, sms=b["sms"] or 0, calls=b["calls"] or 0)


def brief(number: str | None, name: str | None) -> dict:
    """{title, text, sms, kakao, calls, elapsed, summarized_at, html}. text 는 알림 본문(3줄 요약), html 은 큰 카드다."""
    started = time.monotonic()
    name = clean_name(name) or call_briefs.name_for_number(number or "")
    who = name or (number or "").strip() or "모르는 번호"
    row = call_briefs.find(name, number)
    if row is not None and row["summary"]:
        b = {"text": row["summary"], "sms": row["sms"], "kakao": row["kakao"], "calls": row["calls"]}
        summarized_at = row["summarized_at"]
    else:   # 아직 요약이 없는 사람 (처음 연락한 뒤 요약 배치가 아직 안 돌았거나 대화가 없음)
        summarized_at = None
        try:
            b = make_brief(name, number or "", who)
        except BriefError as e:
            logger.warning("call brief for %s failed: %s", who, e)
            b = {"text": "요약 실패.", "sms": 0, "kakao": 0, "calls": 0}
    elapsed = round(time.monotonic() - started, 2)
    logger.info("call brief for %s: cached=%s %.2fs", who, summarized_at is not None, elapsed)
    result = {"title": f"📞 {who}", **b, "elapsed": elapsed, "summarized_at": summarized_at}
    return result | {"text": plain(b["text"]), "html": card(who, result)}
