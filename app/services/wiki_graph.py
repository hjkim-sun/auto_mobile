"""data/wiki 의 마크다운 페이지를 노드, 페이지 사이 링크를 선으로 만든 그래프. 웹 화면(/web/wiki)이 그린다.

- 노드 id 는 위키 기준 상대 경로(people/홍길동.md). 종류(type)는 맨 위 폴더로 정한다.
- sources/{id}.md 는 events.id 와 같아서, 그 이벤트의 source(call, message, email, 나중에 kakao 등)를 붙인다.
- index.md 는 모든 페이지를 가리켜 그래프를 뭉개므로 빼고, log.md 는 프로그램이 적는 기록이라 뺀다.
- 위키 디렉토리는 git 저장소라, 파일이 처음 커밋된 시각(added_at)을 노드마다 붙여 화면이 자라는 과정을 되감아 보여 준다.
  선은 양 끝 노드가 모두 생긴 시각에 생긴 것으로 친다.
페이지가 수천 개가 돼도 파일을 다시 읽지 않게, 파일 수와 마지막 수정 시각이 같으면 앞서 만든 그래프를 돌려준다.
"""
from __future__ import annotations

import posixpath
import re
import subprocess
import threading
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote

from app.core.config import get_settings
from app.db.session import connect

SKIP = {"index.md", "log.md"}
TYPES = {"sources": "source", "people": "person", "orgs": "org", "topics": "topic"}
LINK = re.compile(r"\]\(\s*<?([^)\s>]+?\.md)(?:#[^)\s>]*)?>?(?:\s+\"[^\"]*\")?\s*\)")
SUMMARY_CHARS = 160

_lock = threading.Lock()
_cache: dict = {"key": None, "graph": None}


def page_type(path: str) -> str:
    top = path.split("/", 1)[0] if "/" in path else ""
    if top:
        return TYPES.get(top, top)
    return "schedule" if path == "schedule.md" else "page"


def _title_summary(text: str, fallback: str) -> tuple[str, str]:
    title, summary = None, ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if title is None and line.startswith("# "):
            title = line[2:].strip()
        elif not line.startswith(("#", ">", "-", "|", "```")) and not summary:
            summary = line[:SUMMARY_CHARS]
        if title is not None and summary:
            break
    return title or fallback, summary


def links_of(path: str, text: str, known: set[str]) -> set[str]:
    """페이지 본문의 상대 경로 링크 중 위키 안의 다른 페이지를 가리키는 것."""
    base = posixpath.dirname(path)
    out = set()
    for href in LINK.findall(text):
        if "://" in href or href.startswith("/"):
            continue
        target = posixpath.normpath(posixpath.join(base, unquote(href)))
        if target in known and target != path:
            out.add(target)
    return out


def _git(wiki_dir: Path, *args: str) -> str:
    try:
        proc = subprocess.run(["git", "-c", "core.quotepath=off", "-C", str(wiki_dir), *args],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def history(wiki_dir: Path) -> tuple[list[dict], dict[str, str]]:
    """(오래된 것부터 커밋 목록, 경로별로 처음 커밋된 시각). 위키가 git 저장소가 아니면 빈 값."""
    out = _git(wiki_dir, "log", "--reverse", "--no-renames", "--name-status", "--format=%x00%H%x09%cI%x09%s")
    commits, added = [], {}
    for block in out.split("\x00")[1:]:
        head, *files = block.strip("\n").split("\n")
        sha, at, subject = (head.split("\t", 2) + ["", ""])[:3]
        n = 0
        for line in files:
            status, _, path = line.partition("\t")
            if status == "A" and path.endswith(".md"):
                added.setdefault(path, at)
                n += 1
        commits.append({"hash": sha[:10], "at": at, "subject": subject, "added": n})
    return commits, added


def _event_sources(ids: list[int]) -> dict[int, dict]:
    if not ids or not get_settings().db_path.exists():
        return {}
    with closing(connect()) as conn:
        rows = conn.execute(f"SELECT id, source, created_at FROM events WHERE id IN ({', '.join('?' * len(ids))})",
                            ids).fetchall()
    return {r["id"]: {"source": r["source"], "received_at": r["created_at"]} for r in rows}


def _files(wiki_dir: Path) -> list[Path]:
    return sorted(p for p in wiki_dir.rglob("*.md")
                  if ".git" not in p.relative_to(wiki_dir).parts and p.relative_to(wiki_dir).as_posix() not in SKIP)


def build(wiki_dir: Path) -> dict:
    files = _files(wiki_dir)
    texts = {p.relative_to(wiki_dir).as_posix(): p.read_text(encoding="utf-8", errors="replace") for p in files}
    known = set(texts)
    commits, added = history(wiki_dir)
    source_ids = [int(Path(p).stem) for p in texts if page_type(p) == "source" and Path(p).stem.isdigit()]
    sources = _event_sources(source_ids)

    pairs: dict[tuple[str, str], int] = {}
    for path, text in texts.items():
        for target in links_of(path, text, known):
            key = tuple(sorted((path, target)))
            pairs[key] = pairs.get(key, 0) + 1
    degree: dict[str, int] = {}
    for a, b in pairs:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1

    nodes, counts = [], {}
    for path, text in texts.items():
        kind = page_type(path)
        title, summary = _title_summary(text, Path(path).stem)
        node = {"id": path, "title": title, "type": kind, "summary": summary,
                "degree": degree.get(path, 0), "added_at": added.get(path)}
        if kind == "source":
            stem = Path(path).stem
            event = sources.get(int(stem)) if stem.isdigit() else None
            node["event_id"] = int(stem) if stem.isdigit() else None
            node["source"] = event["source"] if event else None
            # 커밋되기 전 페이지는 이벤트를 받은 시각으로 둔다
            node["added_at"] = node["added_at"] or (event and event["received_at"])
        nodes.append(node)
        counts[kind] = counts.get(kind, 0) + 1

    when = {n["id"]: n["added_at"] for n in nodes}
    links = [{"source": a, "target": b, "weight": w,
              "added_at": max((when[a] or ""), (when[b] or "")) or None} for (a, b), w in pairs.items()]
    return {"nodes": nodes, "links": links, "commits": commits, "counts": counts,
            "source_counts": _count(n.get("source") for n in nodes if n["type"] == "source")}


def _count(values) -> dict:
    out: dict = {}
    for v in values:
        key = v or "unknown"
        out[key] = out.get(key, 0) + 1
    return out


def graph() -> dict:
    wiki_dir = get_settings().wiki_dir
    if not wiki_dir.is_dir():
        return {"nodes": [], "links": [], "commits": [], "counts": {}, "source_counts": {}}
    files = _files(wiki_dir)
    head = (wiki_dir / ".git" / "HEAD").exists() and _git(wiki_dir, "rev-parse", "HEAD").strip()
    key = (len(files), max((p.stat().st_mtime_ns for p in files), default=0), head)
    with _lock:
        if _cache["key"] != key:
            _cache["graph"], _cache["key"] = build(wiki_dir), key
        return _cache["graph"]


class PageNotFound(LookupError):
    pass


def page(path: str) -> dict:
    """페이지 하나의 마크다운과 그 페이지를 가리키는 페이지 목록. 위키 밖이나 .md 가 아니면 PageNotFound."""
    wiki_dir = get_settings().wiki_dir.resolve()
    target = (wiki_dir / path).resolve()
    if (not target.is_relative_to(wiki_dir) or target.suffix != ".md" or ".git" in target.parts
            or not target.is_file()):
        raise PageNotFound(path)
    rel = target.relative_to(wiki_dir).as_posix()
    g = graph()
    titles = {n["id"]: n["title"] for n in g["nodes"]}
    backlinks = sorted({l["source"] if l["target"] == rel else l["target"]
                        for l in g["links"] if rel in (l["source"], l["target"])})
    text = target.read_text(encoding="utf-8", errors="replace")
    return {"path": rel, "title": titles.get(rel) or _title_summary(text, target.stem)[0], "type": page_type(rel),
            "markdown": text, "linked": [{"id": p, "title": titles.get(p, p)} for p in backlinks]}

