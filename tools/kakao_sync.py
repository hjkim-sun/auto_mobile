#!/usr/bin/env python3
"""맥 카카오톡 DB 의 대화 기록을 서버 POST /v1/ingest/kakao/history 로 보낸다. 맥에서만 돈다.

- kakaocli(https://github.com/silver-flight-group/kakaocli)로 DB 를 읽기 전용으로 조회한다.
- 1:1 방(NTChatRoom.type 0)과 단톡방(type 1)만 보낸다. 오픈채팅(4), 카카오톡 채널(2), 나와의 채팅(5)은 뺀다.
- 서버가 log_id 로 중복을 거르므로 같은 기간을 다시 보내도 된다.

환경 변수 (저장소에 넣지 않는다)
  KAKAOCLI_USER_ID   내 카카오 userId. DB 키를 만들 때와 내가 보낸 메시지를 가릴 때 쓴다
  INGEST_URL         서버 주소 (예: https://example.com)
  INGEST_TOKEN       없으면 저장소 .env 의 INGEST_TOKEN
  KAKAOCLI           kakaocli 경로 (기본 kakaocli)

실행: python3 tools/kakao_sync.py [--days 30] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOM_KINDS = {0: "personal", 1: "group"}
SKIP_TYPES = (0, 1999)   # 0: 입장·퇴장 같은 시스템 메시지, 1999: 보낸 사람도 본문도 없는 메시지
PLACEHOLDER = {2: "(사진)", 3: "(동영상)", 12: "(이모티콘)", 18: "(파일)", 20: "(이모티콘)", 27: "(사진)"}
ME = "나"
NAME_SQL = "COALESCE(NULLIF(u.friendNickName,''), NULLIF(u.nickName,''), NULLIF(u.displayName,''))"


def query(sql: str) -> list[list]:
    out = subprocess.run([os.environ.get("KAKAOCLI", "kakaocli"), "query", sql],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def token() -> str:
    if value := os.environ.get("INGEST_TOKEN"):
        return value
    for line in (Path(__file__).resolve().parent.parent / ".env").read_text().splitlines():
        if line.startswith("INGEST_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"\'')
    sys.exit("INGEST_TOKEN 이 없다")


def collect(days: int, me: int) -> dict:
    names = {uid: name for uid, name in query(
        f"SELECT u.userId, {NAME_SQL} FROM NTUser u WHERE u.linkId = 0") if name}
    members: dict[int, set[int]] = {}
    for cid, author in query("SELECT DISTINCT m.chatId, m.authorId FROM NTChatMessage m "
                             "JOIN NTChatRoom r USING(chatId) WHERE r.type = 1 AND r.linkId = 0"):
        members.setdefault(cid, set()).add(author)

    rooms = []
    for cid, rtype, chat_name, partner, display in query(
            "SELECT chatId, type, chatName, directChatMemberUserId, hex(displayMemberIds) "
            "FROM NTChatRoom WHERE type IN (0, 1) AND linkId = 0"):
        if rtype == 0:
            rooms.append({"chat_id": cid, "kind": "personal", "name": names.get(partner) or str(partner)})
            continue
        # displayMemberIds 는 방 목록에 보이는 참여자 몇 명(bplist)뿐이라 이 방에 글을 쓴 사람을 더한다
        ids = set(plistlib.loads(bytes.fromhex(display))) if display else set()
        people = sorted({names[i] for i in ids | members.get(cid, set()) if i != me and i in names})
        rooms.append({"chat_id": cid, "kind": "group", "name": chat_name or ", ".join(people[:5]),
                      "members": people})

    since = int(time.time()) - days * 86400
    messages = []
    for cid, log_id, sent_at, author, mtype, text in query(
            "SELECT m.chatId, m.logId, m.sentAt, m.authorId, m.type, m.message FROM NTChatMessage m "
            f"JOIN NTChatRoom r USING(chatId) WHERE r.type IN (0, 1) AND r.linkId = 0 AND m.sentAt >= {since} "
            f"AND m.type NOT IN {SKIP_TYPES} ORDER BY m.logId"):
        messages.append({"chat_id": cid, "log_id": log_id, "sent_at": sent_at,
                         "author": ME if author == me else names.get(author) or str(author),
                         "text": text or PLACEHOLDER.get(mtype, f"(type {mtype})")})
    return {"rooms": rooms, "messages": messages}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true", help="보내지 않고 건수만 본다")
    args = ap.parse_args()
    me = int(os.environ["KAKAOCLI_USER_ID"])
    history = collect(args.days, me)
    kinds = [r["kind"] for r in history["rooms"]]
    print(f"rooms personal={kinds.count('personal')} group={kinds.count('group')} "
          f"messages={len(history['messages'])}")
    if args.dry_run:
        return
    req = urllib.request.Request(
        os.environ["INGEST_URL"].rstrip("/") + "/v1/ingest/kakao/history",
        data=json.dumps(history, ensure_ascii=False).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token()}"})
    with urllib.request.urlopen(req, timeout=60) as res:
        print(res.read().decode())


if __name__ == "__main__":
    main()
