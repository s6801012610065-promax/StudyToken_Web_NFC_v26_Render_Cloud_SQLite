"""Shared request-signing protocol between the Render web app and the Local NFC Bridge.

Used by BOTH sides (single source of truth). The secret (BRIDGE_TOKEN) is never sent;
every request carries an HMAC-SHA256 signature over method, path+query, timestamp,
nonce and body hash. TLS (https) protects confidentiality; the signature gives
authentication, integrity and replay protection.
"""
from __future__ import annotations
import hashlib, hmac, secrets, time

PROTOCOL_VERSION = 1
H_ID = 'X-Bridge-Id'
H_TS = 'X-Bridge-Timestamp'
H_NONCE = 'X-Bridge-Nonce'
H_SIG = 'X-Bridge-Signature'
MAX_SKEW_SECONDS = 120


def body_hash(body: bytes) -> str:
    return hashlib.sha256(body or b'').hexdigest()


def new_nonce() -> str:
    return secrets.token_hex(16)


def sign(secret: str, method: str, target: str, ts: str, nonce: str, body: bytes) -> str:
    msg = '\n'.join(['v1', method.upper(), target, str(ts), nonce, body_hash(body)]).encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def make_headers(secret: str, bridge_id: str, method: str, target: str, body: bytes, now: float | None = None) -> dict:
    ts = str(int(now if now is not None else time.time()))
    nonce = new_nonce()
    return {H_ID: bridge_id, H_TS: ts, H_NONCE: nonce, H_SIG: sign(secret, method, target, ts, nonce, body)}


def check_signature(secrets_list, method, target, ts, nonce, body, signature) -> bool:
    ok = False
    for s in secrets_list:                       # no early exit: constant work per configured secret
        good = sign(s, method, target, ts, nonce, body)
        ok |= hmac.compare_digest(good, signature or '')
    return ok
