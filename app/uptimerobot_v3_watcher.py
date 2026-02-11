# app/uptimerobot_v3_watcher.py
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from app.integrations.blibsend_http import BlibsendError, send_whatsapp_text


STATE_DIR = Path(".watcher_state")
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "uptimerobot_v3_state.json"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def monitor_obj(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Normaliza o objeto do monitor (nunca retorna None).
    A API v3 pode retornar o monitor dentro de payload["monitor"] ou no root.
    """
    mon_any = payload.get("monitor")
    if isinstance(mon_any, dict):
        return mon_any
    return payload


@dataclass
class Config:
    token: str
    monitor_id: str
    base_url: str = "https://api.uptimerobot.com/v3"

    watch_interval_s: int = 60

    slow_ms_threshold: int = 2500
    slow_consecutive: int = 3

    uptime_24h_min: float = 99.5
    uptime_7d_min: float = 99.0
    uptime_check_every_minutes: int = 60

    alert_to: str = ""
    alert_min_interval_s: int = 900  # rate limit geral
    recover_bypass_rate_limit: bool = True  # ✅ sempre avisar RECUPEROU


def get_cfg() -> Config:
    token = os.getenv("UPTIMEROBOT_V3_TOKEN", "").strip()
    monitor_id = os.getenv("UPTIMEROBOT_MONITOR_ID", "").strip()
    alert_to = os.getenv("ALERT_TO", "").strip()

    if not token:
        raise RuntimeError("UPTIMEROBOT_V3_TOKEN não configurado.")
    if not monitor_id:
        raise RuntimeError("UPTIMEROBOT_MONITOR_ID não configurado.")
    if not alert_to:
        raise RuntimeError("ALERT_TO não configurado.")

    return Config(
        token=token,
        monitor_id=monitor_id,
        watch_interval_s=int(os.getenv("WATCH_INTERVAL_SECONDS", "60")),
        slow_ms_threshold=int(os.getenv("SLOW_MS_THRESHOLD", "2500")),
        slow_consecutive=int(os.getenv("SLOW_CONSECUTIVE", "3")),
        uptime_24h_min=float(os.getenv("UPTIME_24H_MIN", "99.5")),
        uptime_7d_min=float(os.getenv("UPTIME_7D_MIN", "99.0")),
        uptime_check_every_minutes=int(os.getenv("UPTIME_CHECK_EVERY_MINUTES", "60")),
        alert_min_interval_s=int(os.getenv("ALERT_MIN_INTERVAL_SECONDS", "900")),
        recover_bypass_rate_limit=os.getenv("RECOVER_BYPASS_RATE_LIMIT", "1").strip() in ("1", "true", "True"),
        alert_to=alert_to,
    )


def v3_get_monitor(cfg: Config) -> dict[str, Any]:
    url = f"{cfg.base_url}/monitors/{cfg.monitor_id}"
    r = requests.get(
        url,
        headers={
            "accept": "application/json",
            "authorization": f"Bearer {cfg.token}",
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, dict) else {}


def _try_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _try_int(x: Any) -> Optional[int]:
    try:
        if x is None:
            return None
        return int(float(x))
    except Exception:
        return None


def extract_status(payload: dict[str, Any]) -> tuple[Optional[str], Optional[int]]:
    """
    Retorna (status_text, status_code) se existir.
    """
    mon = monitor_obj(payload)
    status = mon.get("status")

    if isinstance(status, str):
        return status.lower(), None
    if isinstance(status, (int, float)):
        return None, int(status)
    return None, None


def extract_response_time_ms(payload: dict[str, Any]) -> Optional[int]:
    mon = monitor_obj(payload)

    for key in ("response_time", "responseTime", "avg_response_time", "avgResponseTime", "last_response_time"):
        ms = _try_int(mon.get(key))
        if ms is not None:
            return ms

    stats_any = mon.get("stats") or mon.get("metrics")
    if isinstance(stats_any, dict):
        stats: dict[str, Any] = stats_any
        for key in ("response_time", "avg_response_time", "avgResponseTime"):
            ms = _try_int(stats.get(key))
            if ms is not None:
                return ms

    return None


def extract_uptime_ratios(payload: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    mon = monitor_obj(payload)

    uptime_24h = _try_float(mon.get("uptime_24h") or mon.get("uptime24h") or mon.get("uptime_1d"))
    uptime_7d = _try_float(mon.get("uptime_7d") or mon.get("uptime7d") or mon.get("uptime_7days"))

    if uptime_24h is not None or uptime_7d is not None:
        return uptime_24h, uptime_7d

    ratios_any = mon.get("uptime") or mon.get("ratios") or mon.get("uptime_ratios")
    if isinstance(ratios_any, dict):
        ratios: dict[str, Any] = ratios_any
        uptime_24h = _try_float(ratios.get("24h") or ratios.get("1d"))
        uptime_7d = _try_float(ratios.get("7d") or ratios.get("7days"))
        return uptime_24h, uptime_7d

    return None, None


def maybe_send(
    cfg: Config,
    state: dict[str, Any],
    title: str,
    msg: str,
    *,
    bypass_rate_limit: bool = False,
) -> None:
    """
    ✅ Correção principal:
    - permite bypass do rate-limit (usado no RECUPEROU)
    - evita travar recuperação se DOWN e UP acontecerem dentro de 15 min
    """
    if not bypass_rate_limit:
        last = state.get("last_alert_sent_at")
        if isinstance(last, str) and last:
            try:
                last_dt = datetime.fromisoformat(last)
                if (now_utc() - last_dt).total_seconds() < cfg.alert_min_interval_s:
                    return
            except Exception:
                pass

    try:
        send_whatsapp_text(to=cfg.alert_to, body=f"*{title}*\n{msg}")
        state["last_alert_sent_at"] = now_utc().isoformat()
        save_state(state)
    except BlibsendError as e:
        print(f"[watcher] FAILED to send WhatsApp: {e}")


def run_loop() -> None:
    cfg = get_cfg()
    state = load_state()

    slow_streak = int(state.get("slow_streak", 0) or 0)
    last_uptime_check = state.get("last_uptime_check_at")

    print(
        f"[watcher] started. interval={cfg.watch_interval_s}s monitor={cfg.monitor_id} "
        f"slow>={cfg.slow_ms_threshold}ms consecutive={cfg.slow_consecutive} "
        f"uptime24h_min={cfg.uptime_24h_min}% uptime7d_min={cfg.uptime_7d_min}% "
        f"alert_min_interval={cfg.alert_min_interval_s}s recover_bypass={cfg.recover_bypass_rate_limit}"
    )

    while True:
        started = time.time()

        try:
            payload = v3_get_monitor(cfg)

            status_text, status_code = extract_status(payload)
            resp_ms = extract_response_time_ms(payload)

            # DOWN detection (ajuste conforme seu payload)
            is_down = False
            if status_text:
                is_down = status_text in ("down", "seems_down", "paused", "unknown")
            if status_code is not None:
                # se vier parecido com v2: 2=up, 8=seems down, 9=down
                is_down = status_code in (8, 9)

            prev_down = bool(state.get("is_down", False))

            if is_down and not prev_down:
                maybe_send(
                    cfg,
                    state,
                    "🚨 API DOWN",
                    f"Monitor {cfg.monitor_id}\nstatus={status_text or status_code}\nHora={now_utc().strftime('%d/%m %H:%M UTC')}",
                )

            if (not is_down) and prev_down:
                # ✅ sempre avisar recuperação (por padrão)
                maybe_send(
                    cfg,
                    state,
                    "✅ API RECUPEROU",
                    f"Monitor {cfg.monitor_id}\nstatus={status_text or status_code}\nHora={now_utc().strftime('%d/%m %H:%M UTC')}",
                    bypass_rate_limit=cfg.recover_bypass_rate_limit,
                )

            # lentidão consecutiva
            is_slow = resp_ms is not None and resp_ms >= cfg.slow_ms_threshold
            slow_streak = slow_streak + 1 if is_slow else 0

            if cfg.slow_ms_threshold > 0 and slow_streak >= cfg.slow_consecutive:
                maybe_send(
                    cfg,
                    state,
                    "⚠️ API LENTA",
                    f"Monitor {cfg.monitor_id}\nresp~{resp_ms}ms (limite {cfg.slow_ms_threshold}ms)\nHora={now_utc().strftime('%d/%m %H:%M UTC')}",
                )
                slow_streak = 0  # evita spam

            # Uptime 24h/7d em frequência menor
            do_uptime = True
            if isinstance(last_uptime_check, str) and last_uptime_check:
                try:
                    last_dt = datetime.fromisoformat(last_uptime_check)
                    do_uptime = (now_utc() - last_dt).total_seconds() >= cfg.uptime_check_every_minutes * 60
                except Exception:
                    do_uptime = True

            if do_uptime:
                u24, u7 = extract_uptime_ratios(payload)

                if u24 is None and u7 is None:
                    mon = monitor_obj(payload)
                    keys = list(mon.keys())[:50]
                    print(f"[watcher] uptime ratios not found. keys(sample)={keys}")
                else:
                    if u24 is not None and u24 < cfg.uptime_24h_min:
                        maybe_send(
                            cfg,
                            state,
                            "📉 UPTIME 24H BAIXO",
                            f"Monitor {cfg.monitor_id}\nUptime 24h: {u24:.3f}% (mín {cfg.uptime_24h_min}%)",
                        )
                    if u7 is not None and u7 < cfg.uptime_7d_min:
                        maybe_send(
                            cfg,
                            state,
                            "📉 UPTIME 7D BAIXO",
                            f"Monitor {cfg.monitor_id}\nUptime 7d: {u7:.3f}% (mín {cfg.uptime_7d_min}%)",
                        )

                state["last_uptime_check_at"] = now_utc().isoformat()
                state["uptime_24h"] = u24
                state["uptime_7d"] = u7

            # persist state
            state["is_down"] = is_down
            state["status"] = status_text or status_code
            state["resp_ms"] = resp_ms
            state["slow_streak"] = slow_streak
            state["last_check_at"] = now_utc().isoformat()
            save_state(state)

            print(f"[watcher] ok status={state['status']} down={is_down} resp_ms={resp_ms} slow_streak={slow_streak}")

        except Exception as e:
            print(f"[watcher] ERROR polling uptimerobot v3: {e}")

        elapsed = time.time() - started
        time.sleep(max(1, cfg.watch_interval_s - int(elapsed)))


if __name__ == "__main__":
    run_loop()
