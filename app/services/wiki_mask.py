"""위키 화면(/wiki/graph, /wiki/page)에 실명·번호 대신 가명을 보낸다. 촬영처럼 화면을 남에게 보여 줄 때 쓴다.

- data/wiki_mask.json({"pairs": [["실제 값", "가명"], ...]})이 있을 때만 켜진다. 파일을 지우거나 이름을 바꾸면 원래대로 보인다.
- data/wiki 파일은 건드리지 않는다. 위키 빌더가 그동안 페이지를 고쳐도 화면에 나갈 때마다 다시 바꾼다.
- 긴 값부터 한 번에 바꾸므로 바꾼 가명이 다시 바뀌지 않는다.
  숫자로 시작하거나 끝나는 값은 앞뒤 숫자에 붙은 것(11,000원 안의 1,000원)을 건드리지 않는다.
"""
from __future__ import annotations

import json
import logging
import re
import threading

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict = {"key": None, "rule": None}


def _rule() -> tuple[re.Pattern, dict[str, str]] | None:
    path = get_settings().wiki_mask_path
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    key = (stat.st_mtime_ns, stat.st_size)
    with _lock:
        if _cache["key"] != key:
            try:
                pairs = json.loads(path.read_text(encoding="utf-8"))["pairs"]
                table = {real: fake for real, fake in pairs if real}
            except (OSError, ValueError, KeyError, TypeError):
                # 매핑을 못 읽었다고 실명을 내보내면 안 되므로 화면을 막는다
                logger.exception("wiki mask unreadable: %s", path)
                raise
            parts = []
            for real in sorted(table, key=len, reverse=True):
                pattern = re.escape(real)
                if real[0].isdigit():
                    pattern = r"(?<![\d,])" + pattern
                if real[-1].isdigit():
                    pattern += r"(?!\d)"
                parts.append(pattern)
            _cache["rule"] = (re.compile("|".join(parts)), table) if parts else None
            _cache["key"] = key
        return _cache["rule"]


def text(value: str | None) -> str | None:
    rule = _rule()
    if not rule or not value:
        return value
    pattern, table = rule
    return pattern.sub(lambda m: table[m.group(0)], value)


def graph(g: dict) -> dict:
    """wiki_graph.graph() 결과의 노드 id(경로)·제목·요약, 선 양 끝, 커밋 제목을 가명으로 바꾼 사본."""
    if not _rule():
        return g
    nodes = [{**n, "id": text(n["id"]), "title": text(n["title"]), "summary": text(n["summary"])}
             for n in g["nodes"]]
    links = [{**l, "source": text(l["source"]), "target": text(l["target"])} for l in g["links"]]
    commits = [{**c, "subject": text(c["subject"])} for c in g["commits"]]
    return {**g, "nodes": nodes, "links": links, "commits": commits}


def real_path(path: str, ids: list[str]) -> str:
    """화면이 보낸 가명 경로를 실제 경로로. 가명 경로가 아니면 그대로."""
    if not _rule():
        return path
    return next((i for i in ids if text(i) == path), path)
