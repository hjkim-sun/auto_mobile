"""Buzz relay 와 주고받기. 공식 buzz CLI(JSON in, JSON out)를 subprocess 로 부른다.

Buzz 는 Nostr relay 라 요청마다 Schnorr 서명이 필요하다. 서명은 CLI 에 맡기고, 키는 명령줄이 아니라
환경변수로 넘겨 ps 에 드러나지 않게 한다.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
from urllib.parse import urlencode

import httpx

from app.core.config import get_settings
from app.services import nostr

CLI_TIMEOUT = 60


class BuzzError(Exception):
    pass


def to_hex_pubkey(value: str) -> str:
    """npub1… 또는 64자 hex 를 hex 로 바꾼다."""
    value = value.strip().lower()
    if len(value) == 64 and all(c in "0123456789abcdef" for c in value):
        return value
    return nostr.bech32_decode(value, "npub").hex()


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


def set_name(name: str) -> None:
    run("users", "set-profile", "--name", name)


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


def mint_invite(ttl_secs: int = 7 * 24 * 3600, max_uses: int = 1) -> dict:
    """커뮤니티 초대 링크. 봇이 relay owner/admin 이어야 한다. buzz CLI 에 없어 직접 NIP-98 로 부른다.

    {"code", "url", "app_url", "expires_at", "max_uses", "uses_remaining"}
    url 은 웹 안내 페이지고, 폰 Buzz 앱에는 app_url(buzz://join?relay=wss://…&code=…)을 넣는다.
    """
    settings = get_settings()
    base = settings.buzz_relay_url.rstrip("/")
    url = f"{base}/api/invites"
    body = json.dumps({"ttl_secs": ttl_secs, "max_uses": max_uses}).encode()
    auth = nostr.nip98_header(settings.buzz_private_key.get_secret_value(), "POST", url, body)
    resp = httpx.post(url, content=body, timeout=30,
                      headers={"Authorization": auth, "Content-Type": "application/json"})
    if resp.status_code >= 400:
        raise BuzzError(f"invite mint {resp.status_code}: {resp.text[:300]}")
    invite = resp.json()
    relay_ws = "ws" + base.removeprefix("http")
    invite["app_url"] = "buzz://join?" + urlencode({"relay": relay_ws, "code": invite["code"]})
    return invite


def pairing_code() -> dict:
    """폰 Buzz 앱의 "Use pairing code" 칸에 넣을 코드와, 그 안에 든 새 사용자 키의 pubkey.

    앱 버전에 따라 첫 화면에 초대 링크 입력칸이 없고 데스크톱 앱이 만든 페어링 코드만 받는다.
    데스크톱의 legacy 형식(buzz:// + base64url JSON {relayUrl, pubkey, nsec})을 그대로 만든다.
    코드에 비밀키가 들어 있으니 저장하지 말고, pubkey 는 relay 멤버로 넣고 BUZZ_OWNER_PUBKEY 로 쓴다.
    """
    secret = nostr.generate_key()
    pubkey = nostr.public_key(secret)
    payload = {"relayUrl": get_settings().buzz_relay_url.rstrip("/"), "pubkey": pubkey,
               "nsec": nostr.bech32_encode("nsec", bytes.fromhex(secret))}
    code = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return {"pubkey": pubkey, "code": "buzz://" + code}
