"""Nostr 키와 서명 (BIP-340 Schnorr, NIP-01 이벤트, NIP-98 HTTP 인증). 순수 Python.

buzz CLI 에 없는 relay API(초대 링크 만들기)를 부를 때만 쓴다. 메시지 송수신은 CLI 가 한다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time

P = 2**256 - 2**32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)
BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % P == 0:
        return None
    if a == b:
        lam = 3 * a[0] * a[0] * pow(2 * a[1], -1, P) % P
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], -1, P) % P
    x = (lam * lam - a[0] - b[0]) % P
    return x, (lam * (a[0] - x) - a[1]) % P


def _mul(k: int, point=G):
    result = None
    while k:
        if k & 1:
            result = _add(result, point)
        point = _add(point, point)
        k >>= 1
    return result


def _tagged(tag: str, data: bytes) -> bytes:
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + data).digest()


def _b(x: int) -> bytes:
    return x.to_bytes(32, "big")


def generate_key() -> str:
    return f"{secrets.randbelow(N - 1) + 1:064x}"


def secret_from(value: str) -> int:
    """hex 또는 nsec1… 비밀키."""
    value = value.strip().lower()
    if value.startswith("nsec1"):
        value = bech32_decode(value, "nsec").hex()
    d = int(value, 16)
    if not 0 < d < N:
        raise ValueError("private key out of range")
    return d


def public_key(secret: str) -> str:
    return _b(_mul(secret_from(secret))[0]).hex()


def schnorr_sign(msg: bytes, secret: str, aux: bytes | None = None) -> bytes:
    d = secret_from(secret)
    pub = _mul(d)
    if pub[1] % 2:
        d = N - d
    aux = secrets.token_bytes(32) if aux is None else aux
    t = _b(d ^ int.from_bytes(_tagged("BIP0340/aux", aux), "big"))
    k = int.from_bytes(_tagged("BIP0340/nonce", t + _b(pub[0]) + msg), "big") % N
    if k == 0:
        raise ValueError("bad nonce")
    r = _mul(k)
    if r[1] % 2:
        k = N - k
    e = int.from_bytes(_tagged("BIP0340/challenge", _b(r[0]) + _b(pub[0]) + msg), "big") % N
    return _b(r[0]) + _b((k + e * d) % N)


def sign_event(secret: str, kind: int, content: str, tags: list[list[str]]) -> dict:
    pubkey = public_key(secret)
    created_at = int(time.time())
    serialized = json.dumps([0, pubkey, created_at, kind, tags, content],
                            separators=(",", ":"), ensure_ascii=False)
    event_id = hashlib.sha256(serialized.encode()).digest()
    return {"id": event_id.hex(), "pubkey": pubkey, "created_at": created_at, "kind": kind,
            "tags": tags, "content": content, "sig": schnorr_sign(event_id, secret).hex()}


def nip98_header(secret: str, method: str, url: str, body: bytes | None = None) -> str:
    tags = [["u", url], ["method", method]]
    if body:
        tags.append(["payload", hashlib.sha256(body).hexdigest()])
    event = sign_event(secret, 27235, "", tags)
    return "Nostr " + base64.b64encode(json.dumps(event, separators=(",", ":")).encode()).decode()


def _polymod(values) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convert(data, frm: int, to: int, pad: bool) -> list[int]:
    acc = bits = 0
    out = []
    for v in data:
        acc, bits = (acc << frm) | v, bits + frm
        while bits >= to:
            bits -= to
            out.append((acc >> bits) & ((1 << to) - 1))
    if pad and bits:
        out.append((acc << (to - bits)) & ((1 << to) - 1))
    return out


def bech32_encode(hrp: str, data: bytes) -> str:
    words = _convert(data, 8, 5, True)
    pm = _polymod(_hrp_expand(hrp) + words + [0] * 6) ^ 1
    return hrp + "1" + "".join(BECH32[d] for d in words + [(pm >> 5 * (5 - i)) & 31 for i in range(6)])


def bech32_decode(value: str, hrp: str) -> bytes:
    value = value.strip().lower()
    if not value.startswith(hrp + "1"):
        raise ValueError(f"not a {hrp}: {value!r}")
    words = [BECH32.index(c) for c in value[len(hrp) + 1:]]
    if _polymod(_hrp_expand(hrp) + words) != 1:
        raise ValueError(f"bad {hrp} checksum")
    data = bytes(_convert(words[:-6], 5, 8, False))
    if len(data) != 32:
        raise ValueError(f"bad {hrp} length")
    return data
