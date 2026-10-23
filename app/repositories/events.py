"""events 테이블: 수신 API 가 1건당 1행을 넣고, 워커가 꺼내 처리한다.

status 흐름
  pending → processing → kept       (쓸모 있음, 다음 단계 대상)
                       → discarded  (쓸모 없음, 종료)
                       → filed      (방을 찾은 카톡 알림. 방 CSV 에 넣고 판단은 kakao_judge 가 방 단위로 한다)
                       → pending    (실패 후 재시도 대기)
                       → failed     (재시도 횟수 초과)
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

from app.db.session import connect


def now(offset_seconds: float = 0) -> str:
    t = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return t.isoformat(timespec="seconds")


def insert_event(source: str, dedup_key: str, payload: dict, raw_path: str | None = None,
                 delay: float = 0, status: str = "pending") -> tuple[int, bool]:
    """새 이벤트면 (id, True), 이미 받은 것이면 (기존 id, False).

    통화는 status=transcribing 으로 넣어 워커가 꺼내지 않게 하고, 전사가 끝나면 release_transcribed 로 푼다.
    """
    ts = now()
    with closing(connect()) as conn:
        row = conn.execute(
            "INSERT INTO events (source, dedup_key, payload, raw_path, status, available_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(dedup_key) DO NOTHING RETURNING id",
            (source, dedup_key, json.dumps(payload, ensure_ascii=False), raw_path, status, now(delay), ts, ts),
        ).fetchone()
        if row:
            return row["id"], True
        return _find_id(conn, dedup_key), False


def find_event_id(dedup_key: str) -> int | None:
    with closing(connect()) as conn:
        return _find_id(conn, dedup_key)


def find_event(dedup_key: str) -> sqlite3.Row | None:
    with closing(connect()) as conn:
        return conn.execute("SELECT * FROM events WHERE dedup_key = ?", (dedup_key,)).fetchone()


def get(event_id: int) -> sqlite3.Row | None:
    with closing(connect()) as conn:
        return conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def recent_kakao(room: str | None, sender: str, limit: int) -> list[dict]:
    """같은 카톡 방(방 이름이 없으면 같은 보낸 사람)의 최근 메시지 (오래된 것부터). [{"sender", "text"}]"""
    field, value = ("room", room) if room else ("sender", sender)
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT json_extract(payload, '$.sender') AS sender, json_extract(payload, '$.text') AS text"
            f" FROM events WHERE source = 'kakao' AND json_extract(payload, '$.{field}') = ?"
            " ORDER BY id DESC LIMIT ?", (value, limit)).fetchall()
    return [{"sender": r["sender"], "text": r["text"]} for r in reversed(rows)]


def _find_id(conn: sqlite3.Connection, dedup_key: str) -> int | None:
    row = conn.execute("SELECT id FROM events WHERE dedup_key = ?", (dedup_key,)).fetchone()
    return row["id"] if row else None


def fill_payload(event_id: int, values: dict) -> bool:
    """payload 에 없거나 비어 있는 키만 values 로 채운다. 바뀌었으면 True."""
    with closing(connect()) as conn:
        row = conn.execute("SELECT payload FROM events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            return False
        payload = json.loads(row["payload"])
        missing = {k: v for k, v in values.items() if v not in (None, "") and payload.get(k) in (None, "")}
        if not missing:
            return False
        conn.execute("UPDATE events SET payload = ?, updated_at = ? WHERE id = ?",
                     (json.dumps({**payload, **missing}, ensure_ascii=False), now(), event_id))
        return True


def postpone(event_id: int, delay: float) -> None:
    """아직 처리 전이면 처리 시점을 뒤로 민다 (MMS 첨부 도착 시)."""
    with closing(connect()) as conn:
        conn.execute(
            "UPDATE events SET available_at = ?, updated_at = ? WHERE id = ? AND status = 'pending'",
            (now(delay), now(), event_id),
        )


def claim_next() -> sqlite3.Row | None:
    with closing(connect()) as conn:
        return conn.execute(
            "UPDATE events SET status = 'processing', attempts = attempts + 1, updated_at = ?"
            " WHERE id = (SELECT id FROM events WHERE status = 'pending' AND available_at <= ?"
            "             ORDER BY available_at, id LIMIT 1)"
            " RETURNING *",
            (now(), now()),
        ).fetchone()


def finish(event_id: int, status: str, verdict: dict | None = None, error: str | None = None) -> None:
    with closing(connect()) as conn:
        conn.execute(
            "UPDATE events SET status = ?, verdict = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, json.dumps(verdict, ensure_ascii=False) if verdict is not None else None,
             error, now(), event_id),
        )


def retry_later(event_id: int, error: str, delay: float) -> None:
    with closing(connect()) as conn:
        conn.execute(
            "UPDATE events SET status = 'pending', error = ?, available_at = ?, updated_at = ? WHERE id = ?",
            (error, now(delay), now(), event_id),
        )


def release_transcribed(raw_path: str) -> None:
    """전사가 끝났거나 실패한 통화를 판별 대기열에 넣는다."""
    with closing(connect()) as conn:
        conn.execute(
            "UPDATE events SET status = 'pending', available_at = ?, updated_at = ?"
            " WHERE raw_path = ? AND status = 'transcribing'",
            (now(), now(), raw_path),
        )


def release_orphaned_transcribing() -> int:
    """전사 작업이 없는 transcribing 통화를 푼다. API 가 이벤트를 넣고 전사를 접수하기 전에 죽은 경우."""
    with closing(connect()) as conn:
        return conn.execute(
            "UPDATE events SET status = 'pending', available_at = ?, updated_at = ? WHERE status = 'transcribing'"
            " AND NOT EXISTS (SELECT 1 FROM transcriptions t WHERE t.input_path = events.raw_path"
            "                 AND t.status IN ('queued', 'running'))",
            (now(), now()),
        ).rowcount


def reset_stale() -> int:
    """워커가 처리 도중 죽었을 때 남은 processing 을 되돌린다. 워커는 1개만 뜬다는 전제."""
    with closing(connect()) as conn:
        return conn.execute(
            "UPDATE events SET status = 'pending', updated_at = ? WHERE status = 'processing'", (now(),)
        ).rowcount


def status_counts() -> dict[str, int]:
    with closing(connect()) as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM events GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}


def list_events(source: str, status: str | None = None, limit: int = 50,
                before_id: int | None = None) -> list[sqlite3.Row]:
    """최신순. before_id 로 다음 페이지를 이어 받는다."""
    sql, args = "SELECT * FROM events WHERE source = ?", [source]
    if status:
        sql += " AND status = ?"
        args.append(status)
    if before_id:
        sql += " AND id < ?"
        args.append(before_id)
    with closing(connect()) as conn:
        return conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
