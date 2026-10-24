#!/usr/bin/env python3
"""맥 카카오톡 DB 의 대화 기록을 서버 POST /v1/ingest/kakao/history 로 보낸다. 맥에서만 돈다.

- kakaocli(https://github.com/silver-flight-group/kakaocli)로 DB 를 읽기 전용으로 조회한다.
- 1:1 방(NTChatRoom.type 0)과 단톡방(type 1)만 보낸다. 오픈채팅(4), 카카오톡 채널(2), 나와의 채팅(5)은 뺀다.
- 맥 카톡(KakaoTalk 프로세스)이 떠 있지 않으면 DB 를 읽지 않고 끝낸다. 꺼져 있으면 DB 에 새 메시지가 들어오지 않는다.
- 보낼 때마다 보낸 메시지 중 가장 늦은 sentAt 을 맥의 상태 파일에 적고, 다음에는 그 이후에 보낸 메시지만 보낸다.
  실행 시각이 아니라 sentAt 을 적으므로, 카톡을 꺼 뒀다 켜서 그동안의 메시지가 원래 보낸 시각으로 DB 에 들어와도 빠지지 않는다.
  상태 파일이 없으면(처음) 최근 2일치를 보낸다.
- 맥 카톡은 메시지를 보낸 시각(sentAt)보다 조금 늦게 DB 에 넣으므로, 상태 파일의 시각보다 OVERLAP 초 앞부터 읽는다.
  서버가 log_id 로 중복을 거르므로 겹쳐 보내도 된다.

환경 변수 (저장소에 넣지 않는다)
  KAKAOCLI_USER_ID   내 카카오 userId. DB 키를 만들 때와 내가 보낸 메시지를 가릴 때 쓴다
  INGEST_URL         서버 주소 (예: https://example.com)
  INGEST_TOKEN       없으면 저장소 .env 의 INGEST_TOKEN
  KAKAOCLI           kakaocli 경로 (기본 kakaocli)
  KAKAO_SYNC_STATE   상태 파일 (기본 ~/Library/Application Support/auto_mobile/kakao_sync.json)

실행: python3 tools/kakao_sync.py [--days N] [--dry-run]
주기 실행(1분)은 tools/install_kakao_sync.sh 로 launchd 에 등록한다.
--days 를 주면 상태 파일과 관계없이 최근 N일치를 다시 보낸다(빠진 것을 확실히 채우고 싶을 때). 카톡이 꺼져 있어도 읽는다.
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
FIRST_DAYS = 2       # 상태 파일이 없을 때 보낼 기간
OVERLAP = 300        # 상태 파일의 sentAt 보다 이만큼 앞부터 다시 읽는다 (맥 DB 에 늦게 들어오는 메시지)
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


def state_path() -> Path:
    default = Path.home() / "Library" / "Application Support" / "auto_mobile" / "kakao_sync.json"
    return Path(os.environ.get("KAKAO_SYNC_STATE") or default)


def last_sent() -> int | None:
    try:
        return int(json.loads(state_path().read_text())["last_sent_at"])
    except (FileNotFoundError, KeyError, ValueError):
        return None


def save_last_sent(at: int) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    # last_sent_at: 서버가 받은 메시지 중 가장 늦은 sentAt
    tmp.write_text(json.dumps({"last_sent_at": at, "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at))}))
    tmp.replace(path)


def collect(since: int, me: int) -> dict:
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
    ap.add_argument("--days", type=int, help="마지막 전송 시각 대신 최근 며칠치를 보낸다")
    ap.add_argument("--dry-run", action="store_true", help="보내지 않고 건수만 본다. 상태 파일도 그대로 둔다")
    args = ap.parse_args()
    if args.days is None and subprocess.run(["pgrep", "-xq", "KakaoTalk"]).returncode != 0:
        return
    me = int(os.environ["KAKAOCLI_USER_ID"])
    started = int(time.time())
    last = last_sent()
    if args.days is not None:
        since = started - args.days * 86400
    elif last is not None:
        since = last - OVERLAP
    else:
        since = started - FIRST_DAYS * 86400
    history = collect(since, me)
    kinds = [r["kind"] for r in history["rooms"]]
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} since {time.strftime('%m-%d %H:%M:%S', time.localtime(since))} "
          f"rooms personal={kinds.count('personal')} group={kinds.count('group')} "
          f"messages={len(history['messages'])}")
    if args.dry_run:
        return
    req = urllib.request.Request(
        os.environ["INGEST_URL"].rstrip("/") + "/v1/ingest/kakao/history",
        data=json.dumps(history, ensure_ascii=False).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token()}"})
    with urllib.request.urlopen(req, timeout=60) as res:
        print(res.read().decode())
    # 서버가 받은 뒤에만 적는다. 실패하면 다음 실행이 같은 시각부터 다시 보낸다. --days 로 옛것을 보내도 뒤로 돌리지 않는다
    newest = max((m["sent_at"] for m in history["messages"]), default=None)
    if newest is not None and (last is None or newest > last):
        save_last_sent(newest)


if __name__ == "__main__":
    main()
