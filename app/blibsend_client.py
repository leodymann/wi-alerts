# app/blibsend_client.py
from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from typing import Optional, Sequence

import requests


class BlibsendError(RuntimeError):
    pass


def _must_env(name: str) -> str:
    v = (os.getenv(name) or "").strip()
    if not v:
        raise BlibsendError(f"{name} não configurado.")
    return v


def _base_url() -> str:
    # você já passou o host prod, mas deixo configurável
    return (os.getenv("BLIBSEND_BASE_URL") or "https://prod.blibsend.click/v2").rstrip("/")


def _basic_header_value(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return base64.b64encode(raw).decode("utf-8")


@dataclass
class _TokenCache:
    token: str
    expires_at_epoch: float


_TOKEN_CACHE: Optional[_TokenCache] = None


def get_bearer_token() -> str:
    """
    POST /auth/signin com Authorization Basic + session_token.
    Cacheia access_token usando expires_in quando disponível.
    """
    global _TOKEN_CACHE

    if _TOKEN_CACHE and _TOKEN_CACHE.expires_at_epoch > time.time() + 30:
        return _TOKEN_CACHE.token

    base = _base_url()
    url = f"{base}/auth/signin"

    client_id = _must_env("BLIBSEND_CLIENT_ID")
    client_secret = _must_env("BLIBSEND_CLIENT_SECRET")
    session_token = _must_env("BLIBSEND_SESSION_TOKEN")

    headers = {
        "Authorization": f"Basic {_basic_header_value(client_id, client_secret)}",
        "session_token": session_token,
        "Content-Type": "application/json",
        "User-Agent": "wi-alerts/1.0",
        "accept": "application/json",
    }

    # geralmente o signin não precisa de body; mando {} por segurança
    r = requests.post(url, headers=headers, json={}, timeout=20)
    if not r.ok:
        raise BlibsendError(f"Auth HTTP {r.status_code}: {r.text[:400]}")

    data = r.json()
    token = (data.get("access_token") or data.get("token") or "").strip()
    if not token:
        raise BlibsendError(f"Auth não retornou access_token. keys={list(data.keys())}")

    # se não vier expires_in, assume 50 min
    expires_in = data.get("expires_in")
    try:
        expires_in = int(expires_in) if expires_in is not None else 3000
    except Exception:
        expires_in = 3000

    _TOKEN_CACHE = _TokenCache(token=token, expires_at_epoch=time.time() + expires_in)
    return token


def send_whatsapp_text(*, to: Sequence[str] | str, body: str) -> None:
    """
    POST /messages/send
    Payload: { "to": ["55..."], "body": "..." }
    """
    base = _base_url()
    url = f"{base}/messages/send"

    session_token = _must_env("BLIBSEND_SESSION_TOKEN")
    token = get_bearer_token()

    if isinstance(to, str):
        to_list = [to]
    else:
        to_list = list(to)

    headers = {
        "Authorization": f"Bearer {token}",
        "session_token": session_token,
        "Content-Type": "application/json",
        "User-Agent": "wi-alerts/1.0",
        "accept": "application/json",
    }

    payload = {"to": to_list, "body": body}

    r = requests.post(url, headers=headers, json=payload, timeout=20)
    if not r.ok:
        raise BlibsendError(f"Send HTTP {r.status_code}: {r.text[:400]}")
