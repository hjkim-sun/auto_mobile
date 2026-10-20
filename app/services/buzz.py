"""Buzz relay 와 주고받기. 공식 buzz CLI(JSON in, JSON out)를 subprocess 로 부른다.

Buzz 는 Nostr relay 라 요청마다 Schnorr 서명이 필요하다. 서명은 CLI 에 맡기고, 키는 명령줄이 아니라
환경변수로 넘겨 ps 에 드러나지 않게 한다.
"""
from __future__ import annotations

import json
import os
import subprocess

from app.core.config import get_settings

CLI_TIMEOUT = 60
BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


class BuzzError(Exception):
    pass


def to_hex_pubkey(value: str) -> str:
    """npub1… 또는 64자 hex 를 hex 로 바꾼다."""
    value = value.strip().lower()
    if len(value) == 64 and all(c in "0123456789abcdef" for c in value):
        return value
    if not value.startswith("npub1"):
        raise ValueError(f"not an npub or hex pubkey: {value!r}")
    data = [BECH32.index(c) for c in value[5:]][:-6]   # 끝 6자는 체크섬
    acc = bits = 0
    out = bytearray()
    for d in data:
        acc, bits = (acc << 5) | d, bits + 5
        if bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    if len(out) != 32:
        raise ValueError(f"bad npub length: {value!r}")
    return out.hex()


def run(*args: str, stdin: str | None = None):
    settings = get_settings()
    env = {**os.environ, "BUZZ_RELAY_URL": settings.buzz_relay_url,
           "BUZZ_PRIVATE_KEY": settings.buzz_private_key.get_secret_value()}
    proc = subprocess.run([str(settings.buzz_cli_path), *args], input=stdin, capture_output=True,
                          text=True, env=env, timeout=CLI_TIMEOUT)
    if proc.returncode != 0:
        raise BuzzError(f"buzz {' '.join(args[:2])} exit {proc.returncode}: {proc.stderr.strip()[:500]}")
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def whoami() -> dict:
    return run("users", "get")


def open_dm(pubkey_hex: str) -> str:
    """DM 채널 UUID. 이미 있으면 같은 채널을 돌려준다."""
    return run("dms", "open", "--pubkey", pubkey_hex)["dm_id"]


def send(channel: str, content: str) -> dict:
    return run("messages", "send", "--channel", channel, "--content", "-", stdin=content)


def get_messages(channel: str, since: int | None = None, limit: int = 50) -> list[dict]:
    """Nostr 이벤트 목록(id, pubkey, content, created_at, ...)."""
    args = ["messages", "get", "--channel", channel, "--limit", str(limit)]
    if since is not None:
        args += ["--since", str(since)]
    result = run(*args)
    if isinstance(result, dict):   # 버전에 따라 {"messages": [...]} 로 감싸 올 수 있다
        result = result.get("messages") or result.get("events") or []
    return result or []
