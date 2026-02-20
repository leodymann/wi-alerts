# app/main.py
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Header, HTTPException

from app.integrations.uazapi import send_whatsapp_text, UazapiError

app = FastAPI(title="WI Alerts")

ALERT_SECRET = (os.getenv("ALERT_SECRET") or "").strip()
ALERT_TO = (os.getenv("ALERT_TO") or "").strip()  # ex: 5583...
API_URL = (os.getenv("API_URL") or "https://wi-api-production.up.railway.app").strip()


def now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/webhook/uptimerobot")
async def uptimerobot_webhook(
    request: Request,
    x_alert_secret: str | None = Header(default=None),
):
    if ALERT_SECRET and x_alert_secret != ALERT_SECRET:
        raise HTTPException(status_code=401, detail="invalid secret")

    if not ALERT_TO:
        raise HTTPException(status_code=500, detail="ALERT_TO not configured")

    raw = await request.body()
    text = raw.decode("utf-8", errors="ignore").strip() or "(sem payload)"

    msg = (
        "🚨 *ALERTA API*\n"
        f"🕒 {now_utc_str()}\n"
        f"🌐 API: {API_URL}\n"
        f"📩 Evento:\n{text[:1200]}"
    )

    try:
        # Uazapi espera string no "number"
        send_whatsapp_text(to=ALERT_TO, body=msg)
    except UazapiError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {"ok": True}
