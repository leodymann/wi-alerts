# app/integrations/blibsend_http.py
from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from typing import Optional

import requests


class BlibsendError(RuntimeError):
    pass


@dataclass
class _TokenCache:
    token: str
    expires_at_epoch: float


_TOKEN_CACHE: Optional[_TokenCache] = None


def _base_url() -> str:
    base = os.getenv("BLIBSEND_BASE_URL", "https://prod.blibsend.click/v2").rstrip("/")
    return base


def _session_token() -> str:
    st = os.getenv("BLIBSEND_SESSION_TOKEN", "").strip()
    if not st:
        raise BlibsendError("BLIBSEND_SESSION_TOKEN não configurado.")
    return st


def _basic_header_value(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("utf-8")


def get_bearer_token() -> str:
    global _TOKEN_CACHE

    # cache
    if _TOKEN_CACHE and _TOKEN_CACHE.expires_at_epoch > time.time() + 30:
        return _TOKEN_CACHE.token

    client_id = os.getenv("BLIBSEND_CLIENT_ID", "").strip()
    client_secret = os.getenv("BLIBSEND_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise BlibsendError("BLIBSEND_CLIENT_ID/BLIBSEND_CLIENT_SECRET não configurados.")

    url = _base_url() + "/auth/signin"
    headers = {"Authorization": _basic_header_value(client_id, client_secret)}

    r = requests.post(url, headers=headers, timeout=20)
    if r.status_code >= 400:
        raise BlibsendError(f"Falha auth/signin: {r.status_code} {r.text}")

    data = r.json()
    token = data.get("access_token") or data.get("token") or data.get("bearer")
    if not token:
        raise BlibsendError(f"Resposta signin sem token: {data}")

    # expiração: se não vier, cache curto
    expires_in = float(data.get("expires_in") or 300)
    _TOKEN_CACHE = _TokenCache(token=token, expires_at_epoch=time.time() + expires_in)
    return token


def send_whatsapp_text(to: str, body: str) -> None:
    token = get_bearer_token()

    url = _base_url() + "/messages/send"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "wi-alerts/1.0",
        "session_token": _session_token(),
    }
    payload = {"to": [to], "body": body}

    r = requests.post(url, headers=headers, json=payload, timeout=25)
    if r.status_code >= 400:
        raise BlibsendError(f"Falha messages/send: {r.status_code} {r.text}")
