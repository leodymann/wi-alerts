# app/main.py
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import requests
from fastapi import FastAPI, Request, Header, HTTPException

from app.blibsend_client import send_whatsapp_text, BlibsendError

app = FastAPI(title="WI Alerts")

ALERT_SECRET = (os.getenv("ALERT_SECRET") or "").strip()
ALERT_TO = (os.getenv("ALERT_TO") or "").strip()  # ex: 5583...
API_URL = (os.getenv("API_URL") or "https://wi-api-production.up.railway.app").strip()

# ===== UptimeRobot v3 =====
UPTIMEROBOT_V3_TOKEN = (os.getenv("UPTIMEROBOT_V3_TOKEN") or "").strip()
UPTIMEROBOT_MONITOR_ID = (os.getenv("UPTIMEROBOT_MONITOR_ID") or "").strip()
UPTIMEROBOT_BASE_URL = (os.getenv("UPTIMEROBOT_BASE_URL") or "https://api.uptimerobot.com/v3").strip().rstrip("/")

# Paths (ajuste conforme sua doc v3)
UPTIMEROBOT_UPTIME_STATS_PATH = (os.getenv("UPTIMEROBOT_UPTIME_STATS_PATH") or "/monitors/{id}/uptime-statistics").strip()
UPTIMEROBOT_RESPONSE_TIME_STATS_PATH = (os.getenv("UPTIMEROBOT_RESPONSE_TIME_STATS_PATH") or "/monitors/{id}/response-time-stats").strip()
UPTIMEROBOT_INCIDENTS_PATH = (os.getenv("UPTIMEROBOT_INCIDENTS_PATH") or "/incidents").strip()

# Se a sua API v3 usa outros nomes de query param, ajuste aqui também
UPTIMEROBOT_RANGE_PARAM_START = (os.getenv("UPTIMEROBOT_RANGE_PARAM_START") or "start").strip()
UPTIMEROBOT_RANGE_PARAM_END = (os.getenv("UPTIMEROBOT_RANGE_PARAM_END") or "end").strip()
UPTIMEROBOT_MONITOR_PARAM = (os.getenv("UPTIMEROBOT_MONITOR_PARAM") or "monitorId").strip()


def now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _require_secret(x_alert_secret: str | None):
    if ALERT_SECRET and x_alert_secret != ALERT_SECRET:
        raise HTTPException(status_code=401, detail="invalid secret")


def _require_alert_to():
    if not ALERT_TO:
        raise HTTPException(status_code=500, detail="ALERT_TO not configured")


def _uptime_headers() -> dict[str, str]:
    if not UPTIMEROBOT_V3_TOKEN:
        raise HTTPException(status_code=500, detail="UPTIMEROBOT_V3_TOKEN not configured")
    return {
        "accept": "application/json",
        "authorization": f"Bearer {UPTIMEROBOT_V3_TOKEN}",
    }


def _uptime_get(path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    path = path.lstrip("/")
    url = f"{UPTIMEROBOT_BASE_URL}/{path}"
    r = requests.get(url, headers=_uptime_headers(), params=params or {}, timeout=25)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, dict) else {}


def _pick_first_number(d: dict[str, Any], keys: list[str]) -> Optional[float]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except Exception:
                pass
    return None


def _fmt_dt_iso(v: Any) -> str:
    if isinstance(v, str) and v:
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            # mostrar em UTC mesmo, simples e consistente
            return dt.astimezone(timezone.utc).strftime("%d/%m %H:%M UTC")
        except Exception:
            return v
    if isinstance(v, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(v), tz=timezone.utc)
            return dt.strftime("%d/%m %H:%M UTC")
        except Exception:
            return str(v)
    return "-"


def build_weekly_summary() -> str:
    if not UPTIMEROBOT_MONITOR_ID:
        raise HTTPException(status_code=500, detail="UPTIMEROBOT_MONITOR_ID not configured")

    end_utc = datetime.now(timezone.utc)
    start_utc = end_utc - timedelta(days=7)

    common_params = {
        UPTIMEROBOT_MONITOR_PARAM: UPTIMEROBOT_MONITOR_ID,
        UPTIMEROBOT_RANGE_PARAM_START: start_utc.isoformat(),
        UPTIMEROBOT_RANGE_PARAM_END: end_utc.isoformat(),
    }

    # 1) uptime stats
    uptime_stats: dict[str, Any]
    try:
        up_path = UPTIMEROBOT_UPTIME_STATS_PATH.format(id=UPTIMEROBOT_MONITOR_ID)
        uptime_stats = _uptime_get(up_path, params=common_params)
    except Exception as e:
        uptime_stats = {"_error": str(e)}

    # 2) response time stats
    resp_stats: dict[str, Any]
    try:
        rt_path = UPTIMEROBOT_RESPONSE_TIME_STATS_PATH.format(id=UPTIMEROBOT_MONITOR_ID)
        resp_stats = _uptime_get(rt_path, params=common_params)
    except Exception as e:
        resp_stats = {"_error": str(e)}

    # 3) incidents
    incidents: dict[str, Any]
    try:
        inc_path = UPTIMEROBOT_INCIDENTS_PATH.format(id=UPTIMEROBOT_MONITOR_ID)
        incidents = _uptime_get(inc_path, params=common_params)
    except Exception as e:
        incidents = {"_error": str(e)}

    # ===== extrair uptime (%)
    uptime_7d = None
    if isinstance(uptime_stats, dict) and not uptime_stats.get("_error"):
        uptime_7d = _pick_first_number(uptime_stats, ["uptime", "uptime7d", "ratio", "availability", "uptimeRatio"])
        for sub in ("stats", "data", "result"):
            subd = uptime_stats.get(sub)
            if uptime_7d is None and isinstance(subd, dict):
                uptime_7d = _pick_first_number(subd, ["uptime", "uptime7d", "ratio", "availability", "uptimeRatio"])

    # ===== extrair response time
    avg_ms = None
    p95_ms = None
    if isinstance(resp_stats, dict) and not resp_stats.get("_error"):
        avg_ms = _pick_first_number(resp_stats, ["avg", "avgMs", "average", "avgResponseTime", "mean"])
        p95_ms = _pick_first_number(resp_stats, ["p95", "p95Ms", "p95ResponseTime"])
        for sub in ("stats", "data", "result"):
            subd = resp_stats.get(sub)
            if isinstance(subd, dict):
                avg_ms = avg_ms or _pick_first_number(subd, ["avg", "avgMs", "average", "avgResponseTime", "mean"])
                p95_ms = p95_ms or _pick_first_number(subd, ["p95", "p95Ms", "p95ResponseTime"])

    # ===== extrair incidents
    inc_list: list[dict[str, Any]] = []
    if isinstance(incidents, dict) and not incidents.get("_error"):
        for k in ("incidents", "data", "items", "results"):
            v = incidents.get(k)
            if isinstance(v, list):
                inc_list = [x for x in v if isinstance(x, dict)]
                break

    inc_count = len(inc_list)
    last_inc_txt = "—"
    if inc_list:
        def _ts(x: dict[str, Any]) -> float:
            for k in ("startedAt", "start", "createDateTime", "created", "createdAt"):
                v = x.get(k)
                if isinstance(v, (int, float)):
                    return float(v)
                if isinstance(v, str):
                    try:
                        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        pass
            return 0.0

        inc_sorted = sorted(inc_list, key=_ts, reverse=True)
        last = inc_sorted[0]
        reason = last.get("reason") or last.get("message") or last.get("title") or last.get("id") or "incidente"
        started = _fmt_dt_iso(last.get("startedAt") or last.get("start") or last.get("createdAt") or last.get("created"))
        last_inc_txt = f"{reason} ({started})"

    period_txt = f"{start_utc.strftime('%d/%m')}–{end_utc.strftime('%d/%m')}"

    uptime_txt = f"{uptime_7d:.3f}%" if isinstance(uptime_7d, (int, float)) else "n/d"
    avg_txt = f"{int(avg_ms)}ms" if isinstance(avg_ms, (int, float)) else "n/d"
    p95_txt = f"{int(p95_ms)}ms" if isinstance(p95_ms, (int, float)) else "n/d"

    msg = (
        f"📊 *Resumo semanal (UptimeRobot)*\n"
        f"Período: {period_txt}\n"
        f"Monitor: {UPTIMEROBOT_MONITOR_ID}\n\n"
        f"✅ Uptime (7d): {uptime_txt}\n"
        f"⚡ Resp. média: {avg_txt}\n"
        f"📈 Resp. p95: {p95_txt}\n"
        f"🚨 Incidentes (7d): {inc_count}\n"
        f"🧾 Último incidente: {last_inc_txt}\n"
    )

    errs = []
    if uptime_stats.get("_error"):
        errs.append(f"uptime-stats: {uptime_stats['_error']}")
    if resp_stats.get("_error"):
        errs.append(f"resp-time-stats: {resp_stats['_error']}")
    if incidents.get("_error"):
        errs.append(f"incidents: {incidents['_error']}")

    if errs:
        msg += "\n⚠️ *Obs:* alguns endpoints falharam:\n- " + "\n- ".join(errs)

    return msg


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/webhook/uptimerobot")
async def uptimerobot_webhook(
    request: Request,
    x_alert_secret: str | None = Header(default=None),
):
    _require_secret(x_alert_secret)
    _require_alert_to()

    raw = await request.body()
    text = raw.decode("utf-8", errors="ignore").strip() or "(sem payload)"

    msg = (
        "🚨 *ALERTA API*\n"
        f"🕒 {now_utc_str()}\n"
        f"🌐 API: {API_URL}\n"
        f"📩 Evento:\n{text[:1200]}"
    )

    try:
        send_whatsapp_text(to=[ALERT_TO], body=msg)
    except BlibsendError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {"ok": True}


# ✅ NOVO: rota para disparar o resumo semanal (você chama via cron)
@app.post("/jobs/uptimerobot/weekly")
def uptimerobot_weekly_summary(
    x_alert_secret: str | None = Header(default=None),
):
    _require_secret(x_alert_secret)
    _require_alert_to()

    summary = build_weekly_summary()

    try:
        send_whatsapp_text(to=[ALERT_TO], body=summary)
    except BlibsendError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {"ok": True, "sent_to": ALERT_TO}
