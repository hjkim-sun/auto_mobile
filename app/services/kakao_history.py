"""맥 카카오톡 DB(kakaocli)에서 읽은 대화 기록을 방마다 CSV 로 쌓는다.

data/raw/kakao/
  rooms.json                 chat_id → {kind, name, file, members}. 방 목록과 단톡 참여자
  personal/{상대 이름}.csv     1:1 방
  group/{chat_id}.csv         단톡방

CSV 열은 일시, 사용자명, 톡 내용, log_id, jev 판단, claude 판단 이다. log_id 는 카카오 서버가 매긴 메시지 ID 로,
폰 알림의 chatLogId 와 같은 값이고 시간 순으로 커진다. 같은 log_id 는 한 번만 넣고, 파일은 log_id 순으로 다시 쓴다.
판단 두 열은 방 단위 판단(app/services/kakao_judge.py)이 채운다. 비어 있으면 아직 판단하지 않은 메시지다.

새 메시지가 들어간 방은 rooms.json 에 unjudged_since(처음 들어온 시각)와 last_added(마지막으로 들어온 시각)를
적어 판단 대기로 둔다. 판단이 끝나면 mark_judged 가 지운다.

폰 알림(POST /v1/ingest/kakao)으로 온 메시지도 워커가 판별 전에 file_notification 으로 같은 CSV 에 넣는다.
- 1:1 방 알림은 보낸 사람 이름의 personal 방에 넣는다. 맥에서 아직 받지 못한 방이면 "name:{이름}" 으로 임시 등록한다.
- 단톡 알림은 보낸 사람이 참여한 단톡방(rooms.json members)에 넣는다. 후보가 여럿일 때만 Jev 가 각 방의 최근
  대화를 보고 고른다. 후보가 없으면 group/unassigned.csv 에 둔다.
- 알림의 chatLogId 는 Automate 가 double 로 넘겨 ±256 안에서 어긋난다. 연속한 두 메시지의 log_id 는 수천 이상
  차이 나므로, 512 안쪽이면 같은 메시지로 보고 512 의 배수가 아닌 값(맥이 준 정확한 값)을 남긴다.
"""
from __future__ import annotations

import bisect
import csv
import fcntl
import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.core.config import get_settings
from app.schemas.ingest import KakaoHistory
from app.services import jev
from app.services.ingest import safe_name

COLUMNS = ["일시", "사용자명", "톡 내용", "log_id", "jev 판단", "claude 판단"]
UNASSIGNED = "group/unassigned.csv"
LOG_ID_SLACK = 512    # 폰 chatLogId 오차(±256)를 넉넉히 덮는 폭
ROOM_CONTEXT = 8      # Jev 에 보여 줄 후보 방마다의 최근 메시지 수


@contextmanager
def _locked() -> Iterator[None]:
    """API(맥 기록)와 워커(폰 알림)가 다른 프로세스라 파일 잠금으로 rooms.json 과 CSV 를 지킨다."""
    path = kakao_dir() / ".lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def kakao_dir() -> Path:
    return get_settings().raw_dir / "kakao"


def load_rooms() -> dict[str, dict]:
    path = kakao_dir() / "rooms.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save_rooms(rooms: dict[str, dict]) -> None:
    path = kakao_dir() / "rooms.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rooms, ensure_ascii=False, indent=2), encoding="utf-8")


def _room_file(rooms: dict[str, dict], chat_id: str, kind: str, name: str) -> str:
    if kind == "group":
        return f"group/{chat_id}.csv"
    # 1:1 방은 상대 이름으로 둔다. 이름이 같은 다른 방이 이미 있으면 chat_id 를 붙인다.
    file = f"personal/{safe_name(name, chat_id)}.csv"
    if any(r["file"] == file for cid, r in rooms.items() if cid != chat_id):
        file = f"personal/{safe_name(name, chat_id)}_{chat_id}.csv"
    return file


def read_rows(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))[1:]
    # 판단 열이 생기기 전에 쓴 파일은 4열이다
    return [r + [""] * (len(COLUMNS) - len(r)) for r in rows]


def _write(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(rows)
    tmp.replace(path)


def _merge(path: Path, rows: list[list[str]]) -> int:
    """rows 중 path 에 없는 메시지만 더하고 더한 수를 돌려준다. 같은 메시지는 log_id 가 512 안쪽인 것이다."""
    existing = sorted(read_rows(path), key=lambda r: int(r[3]))
    ids = [int(r[3]) for r in existing]
    added = changed = 0
    for row in rows:
        row = row + [""] * (len(COLUMNS) - len(row))
        log_id = int(row[3])
        i = bisect.bisect_left(ids, log_id - LOG_ID_SLACK)
        if i < len(ids) and ids[i] <= log_id + LOG_ID_SLACK:
            # 폰 알림 값(512 의 배수)이 먼저 들어가 있으면 맥이 준 정확한 행으로 바꾼다. 판단 결과는 남긴다
            if ids[i] != log_id and ids[i] % LOG_ID_SLACK == 0 and log_id % LOG_ID_SLACK:
                existing[i], ids[i] = row[:4] + existing[i][4:], log_id
                changed += 1
            continue
        existing.insert(i, row)
        ids.insert(i, log_id)
        added += 1
    if added or changed:
        _write(path, existing)
    return added


def _flag(room: dict, now: float) -> None:
    """새 메시지가 들어간 방을 판단 대기로 둔다."""
    room.setdefault("unjudged_since", now)
    room["last_added"] = now


def save_history(history: KakaoHistory) -> dict[str, int]:
    tz = get_settings().tz
    with _locked():
        rooms = load_rooms()
        for room in history.rooms:
            cid = str(room.chat_id)
            # 폰 알림으로 먼저 만든 임시 1:1 방이 있으면 그 파일을 이어 쓴다
            old = rooms.get(cid) or (rooms.pop(f"name:{room.name}", None) if room.kind == "personal" else None) or {}
            rooms[cid] = {"kind": room.kind, "name": room.name,
                          "file": old.get("file") or _room_file(rooms, cid, room.kind, room.name),
                          "members": sorted(set(old.get("members", [])) | set(room.members))}

        by_room: dict[str, list[list[str]]] = {}
        unknown = 0
        for m in history.messages:
            if str(m.chat_id) not in rooms:   # rooms 에 없는 방(오픈채팅 등)의 메시지는 받지 않는다
                unknown += 1
                continue
            when = datetime.fromtimestamp(m.sent_at, tz).strftime("%Y-%m-%d %H:%M:%S")
            by_room.setdefault(str(m.chat_id), []).append([when, m.author, m.text, str(m.log_id)])
        added = 0
        for cid, rows in by_room.items():
            if n := _merge(kakao_dir() / rooms[cid]["file"], rows):
                _flag(rooms[cid], time.time())
                added += n
        _save_rooms(rooms)
    return {"rooms": len(history.rooms), "added": added,
            "duplicates": len(history.messages) - unknown - added, "unknown_room": unknown}


def groups_of(name: str, rooms: dict[str, dict] | None = None) -> list[str]:
    """name 이 참여한 단톡방 chat_id 목록. 폰 알림의 단톡 메시지가 어느 방 것인지 고를 때 쓴다."""
    rooms = load_rooms() if rooms is None else rooms
    return [cid for cid, r in rooms.items() if r["kind"] == "group" and name in r["members"]]


def choose_room(msg: dict, candidates: dict[str, dict]) -> tuple[str, dict[str, float]]:
    """단톡 알림 msg 가 candidates(chat_id → 방) 중 어느 방 것인지 Jev 에 묻는다. (chat_id, 방별 확률)"""
    criteria = {}
    for cid, room in candidates.items():
        recent = read_rows(kakao_dir() / room["file"])[-ROOM_CONTEXT:]
        lines = [f"방 이름: {room['name']}", f"참여자: {', '.join(room['members'])}", "최근 대화:"]
        lines += [f"{r[0]} {r[1]}: {r[2]}" for r in recent] or ["(없음)"]
        criteria[f"room_{cid}"] = "\n".join(lines)
    state = "\n".join([f"보낸 사람: {msg['sender']}", f"시각: {msg.get('date_text')}", "", "메시지:", msg["text"]])
    question = {"type": "choice", "criteria": criteria,
                "instructions": "이 카카오톡 단톡 메시지는 어느 단톡방에서 온 것인가? 각 방의 최근 대화와 "
                                "주제·시각이 이어지는 방을 고른다."}
    probs = jev.classify(state, {"room": question})["answers"]["room"]["probabilities"]
    probs = {k.removeprefix("room_"): v for k, v in probs.items()}
    return max(probs, key=probs.get), probs


def file_notification(msg: dict, choose: Callable = choose_room) -> dict | None:
    """폰 알림 메시지(이벤트 payload)를 방 CSV 에 넣고 고른 방을 돌려준다. chatLogId 가 없으면 넣지 않는다."""
    if msg.get("chat_log_id") is None or not msg.get("text"):
        return None
    sender = msg["sender"]
    when = msg.get("date_text") or datetime.now(get_settings().tz).strftime("%Y-%m-%d %H:%M:%S")
    with _locked():
        rooms = load_rooms()
        result: dict = {}
        if msg.get("group"):
            candidates = {cid: rooms[cid] for cid in groups_of(sender, rooms)}
            if len(candidates) == 1:
                result = {"chat_id": next(iter(candidates)), "by": "only_group"}
            elif candidates:
                cid, probs = choose(msg, candidates)
                result = {"chat_id": cid, "by": "jev", "probabilities": probs}
            else:
                result = {"chat_id": None, "by": "unassigned"}
        else:
            cid = next((c for c, r in rooms.items() if r["kind"] == "personal" and r["name"] == sender), None)
            if cid is None:   # 맥에서 아직 받지 못한 1:1 방
                cid = f"name:{sender}"
                rooms[cid] = {"kind": "personal", "name": sender,
                              "file": _room_file(rooms, cid, "personal", sender), "members": []}
                _save_rooms(rooms)
            result = {"chat_id": cid, "by": "personal"}
        result["file"] = rooms[result["chat_id"]]["file"] if result["chat_id"] else UNASSIGNED
        if _merge(kakao_dir() / result["file"], [[when, sender, msg["text"], str(msg["chat_log_id"])]]) \
                and result["chat_id"]:
            _flag(rooms[result["chat_id"]], time.time())
            _save_rooms(rooms)
    return result


def due_rooms(now: float, settle: float, max_wait: float) -> dict[str, dict]:
    """판단할 때가 된 방. 대화가 settle 초 동안 멈췄거나, 첫 새 메시지 뒤 max_wait 초가 지난 방."""
    with _locked():
        rooms = load_rooms()
    return {cid: r for cid, r in rooms.items() if "unjudged_since" in r and now >= r.get("retry_at", 0)
            and (now - r["last_added"] >= settle or now - r["unjudged_since"] >= max_wait)}


def mark_judged(chat_id: str, started: float, marks: dict[str, tuple[str, str]],
                retry_at: float | None = None) -> None:
    """판단 결과를 CSV 의 판단 열에 적고 방의 판단 대기를 푼다. marks: log_id → (jev 판단, claude 판단).

    판단하는 동안(started 뒤) 새 메시지가 들어왔으면 대기를 풀지 않는다. retry_at 을 주면 그때 다시 판단한다.
    """
    with _locked():
        rooms = load_rooms()
        room = rooms.get(chat_id)
        if room is None:
            return
        if marks:
            path = kakao_dir() / room["file"]
            rows = read_rows(path)
            for r in rows:
                if r[3] in marks:
                    r[4], r[5] = marks[r[3]]
            _write(path, rows)
        if retry_at is not None:
            room["retry_at"] = retry_at
        elif room.get("last_added", 0) <= started:
            for key in ("unjudged_since", "last_added", "retry_at"):
                room.pop(key, None)
        else:   # 판단하는 사이 들어온 메시지는 다음 판단에 넘긴다
            room["unjudged_since"] = started
            room.pop("retry_at", None)
        _save_rooms(rooms)
