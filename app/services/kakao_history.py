"""맥 카카오톡 DB(kakaocli)에서 읽은 대화 기록을 방마다 CSV 로 쌓는다.

data/raw/kakao/
  rooms.json                 chat_id → {kind, name, file, members}. 방 목록과 단톡 참여자
  personal/{상대 이름}.csv     1:1 방
  group/{chat_id}.csv         단톡방

CSV 열은 일시, 사용자명, 톡 내용, log_id 이다. log_id 는 카카오 서버가 매긴 메시지 ID 로, 폰 알림의
chatLogId 와 같은 값이고 시간 순으로 커진다. 같은 log_id 는 한 번만 넣고, 파일은 log_id 순으로 다시 쓴다.
"""
from __future__ import annotations

import csv
import json
import threading
from datetime import datetime
from pathlib import Path

from app.core.config import get_settings
from app.schemas.ingest import KakaoHistory
from app.services.ingest import safe_name

COLUMNS = ["일시", "사용자명", "톡 내용", "log_id"]
_lock = threading.Lock()   # 같은 CSV 를 두 요청이 동시에 다시 쓰지 않게


def kakao_dir() -> Path:
    return get_settings().raw_dir / "kakao"


def load_rooms() -> dict[str, dict]:
    path = kakao_dir() / "rooms.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _room_file(rooms: dict[str, dict], chat_id: str, kind: str, name: str) -> str:
    if kind == "group":
        return f"group/{chat_id}.csv"
    # 1:1 방은 상대 이름으로 둔다. 이름이 같은 다른 방이 이미 있으면 chat_id 를 붙인다.
    file = f"personal/{safe_name(name, chat_id)}.csv"
    if any(r["file"] == file for cid, r in rooms.items() if cid != chat_id):
        file = f"personal/{safe_name(name, chat_id)}_{chat_id}.csv"
    return file


def _merge(path: Path, rows: list[list[str]]) -> int:
    """rows 중 path 에 없는 log_id 만 더하고 더한 수를 돌려준다."""
    existing: dict[str, list[str]] = {}
    if path.exists():
        with path.open(encoding="utf-8", newline="") as f:
            existing = {r[3]: r for r in list(csv.reader(f))[1:]}
    new = {r[3]: r for r in rows if r[3] not in existing}
    if not new:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(sorted({**existing, **new}.values(), key=lambda r: int(r[3])))
    tmp.replace(path)
    return len(new)


def save_history(history: KakaoHistory) -> dict[str, int]:
    tz = get_settings().tz
    with _lock:
        rooms = load_rooms()
        for room in history.rooms:
            cid = str(room.chat_id)
            old = rooms.get(cid, {})
            rooms[cid] = {"kind": room.kind, "name": room.name,
                          "file": old.get("file") or _room_file(rooms, cid, room.kind, room.name),
                          "members": sorted(set(old.get("members", [])) | set(room.members))}
        path = kakao_dir() / "rooms.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rooms, ensure_ascii=False, indent=2), encoding="utf-8")

        by_file: dict[str, list[list[str]]] = {}
        unknown = 0
        for m in history.messages:
            room = rooms.get(str(m.chat_id))
            if room is None:   # rooms 에 없는 방(오픈채팅 등)의 메시지는 받지 않는다
                unknown += 1
                continue
            when = datetime.fromtimestamp(m.sent_at, tz).strftime("%Y-%m-%d %H:%M:%S")
            by_file.setdefault(room["file"], []).append([when, m.author, m.text, str(m.log_id)])
        added = sum(_merge(kakao_dir() / file, rows) for file, rows in by_file.items())
    return {"rooms": len(history.rooms), "added": added,
            "duplicates": len(history.messages) - unknown - added, "unknown_room": unknown}


def groups_of(name: str) -> list[str]:
    """name 이 참여한 단톡방 chat_id 목록. 폰 알림의 단톡 메시지가 어느 방 것인지 고를 때 쓴다."""
    return [cid for cid, r in load_rooms().items() if r["kind"] == "group" and name in r["members"]]
