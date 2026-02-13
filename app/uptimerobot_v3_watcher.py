# app/uptimerobot_v3_watcher.py
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional, Tuple

import requests

from app.integrations.blibsend_http import BlibsendError, send_whatsapp_text

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # python<3.9 (não é seu caso)

STATE_DIR = Path(".watcher_state")
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "uptimerobot_v3_state.json"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_local(tz_name: str) -> datetime:
    if ZoneInfo is None:
        return now_utc()
    return datetime.now(ZoneInfo(tz_name))


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

    # polling
    watch_interval_s: int = 60

    # slow detection
    slow_ms_threshold: int = 2500
    slow_consecutive: int = 3

    # uptime checks
    uptime_24h_min: float = 99.5
    uptime_7d_min: float = 99.0
    uptime_check_every_minutes: int = 60

    # alerts
    alert_to: str = ""
    alert_min_interval_s: int = 900
    recover_bypass_rate_limit: bool = True

    # weekly report
    tz_name: str = "America/Fortaleza"
    weekly_report_enabled: bool = True
    weekly_report_weekday: int = 0  # 0=Monday ... 6=Sunday
    weekly_report_hour: int = 9
    weekly_report_minute: int = 0


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
        tz_name=(os.getenv("TZ_NAME") or "America/Fortaleza").strip(),
        weekly_report_enabled=(os.getenv("WEEKLY_REPORT_ENABLED", "1").strip() in ("1", "true", "True")),
        weekly_report_weekday=int(os.getenv("WEEKLY_REPORT_WEEKDAY", "0")),  # Monday
        weekly_report_hour=int(os.getenv("WEEKLY_REPORT_HOUR", "9")),
        weekly_report_minute=int(os.getenv("WEEKLY_REPORT_MINUTE", "0")),
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


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(s: Any) -> Optional[datetime]:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def maybe_send(
    cfg: Config,
    state: dict[str, Any],
    title: str,
    msg: str,
    *,
    bypass_rate_limit: bool = False,
) -> None:
    if not bypass_rate_limit:
        last = state.get("last_alert_sent_at")
        last_dt = _parse_iso(last)
        if last_dt and (now_utc() - last_dt).total_seconds() < cfg.alert_min_interval_s:
            return

    try:
        send_whatsapp_text(to=cfg.alert_to, body=f"*{title}*\n{msg}")
        state["last_alert_sent_at"] = _iso(now_utc())
        save_state(state)
    except BlibsendError as e:
        print(f"[watcher] FAILED to send WhatsApp: {e}")


# ---------------------------
# Weekly summary helpers
# ---------------------------

def _start_of_week_local(dt_local: datetime) -> datetime:
    # retorna segunda-feira 00:00 local
    weekday = dt_local.weekday()  # 0=Mon
    start = dt_local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=weekday)
    return start


def ensure_week_bucket(cfg: Config, state: dict[str, Any]) -> None:
    """
    Garante que existe um "bucket semanal" no state.
    Se mudou a semana, reseta contadores automaticamente.
    """
    dt_local = now_local(cfg.tz_name)
    week_start = _start_of_week_local(dt_local)

    cur = state.get("weekly", {})
    cur_start = _parse_iso(cur.get("week_start_local"))

    if not isinstance(cur, dict) or cur_start is None:
        state["weekly"] = {
            "week_start_local": _iso(week_start),
            "incidents": 0,
            "downtime_seconds": 0,
            "slow_alerts": 0,
            "resp_min_ms": None,
            "resp_max_ms": None,
            "down_started_at_utc": None,
            "last_weekly_report_sent_for_start": None,
        }
        return

    # se mudou a semana (start diferente), reseta
    if cur_start != week_start:
        state["weekly"] = {
            "week_start_local": _iso(week_start),
            "incidents": 0,
            "downtime_seconds": 0,
            "slow_alerts": 0,
            "resp_min_ms": None,
            "resp_max_ms": None,
            "down_started_at_utc": None,
            "last_weekly_report_sent_for_start": None,
        }


def update_weekly_metrics(state: dict[str, Any], *, resp_ms: Optional[int]) -> None:
    w = state.get("weekly")
    if not isinstance(w, dict):
        return

    if resp_ms is not None:
        mn = w.get("resp_min_ms")
        mx = w.get("resp_max_ms")
        if mn is None or resp_ms < int(mn):
            w["resp_min_ms"] = resp_ms
        if mx is None or resp_ms > int(mx):
            w["resp_max_ms"] = resp_ms


def mark_weekly_down_transition(state: dict[str, Any]) -> None:
    w = state.get("weekly")
    if not isinstance(w, dict):
        return
    w["incidents"] = int(w.get("incidents", 0) or 0) + 1
    # registra início do down se não estava marcado
    if not w.get("down_started_at_utc"):
        w["down_started_at_utc"] = _iso(now_utc())


def mark_weekly_up_transition(state: dict[str, Any]) -> None:
    w = state.get("weekly")
    if not isinstance(w, dict):
        return
    started = _parse_iso(w.get("down_started_at_utc"))
    if started:
        w["downtime_seconds"] = int(w.get("downtime_seconds", 0) or 0) + int((now_utc() - started).total_seconds())
    w["down_started_at_utc"] = None


def mark_weekly_slow_alert(state: dict[str, Any]) -> None:
    w = state.get("weekly")
    if not isinstance(w, dict):
        return
    w["slow_alerts"] = int(w.get("slow_alerts", 0) or 0) + 1


def fmt_duration(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def should_send_weekly_report(cfg: Config, state: dict[str, Any]) -> bool:
    if not cfg.weekly_report_enabled:
        return False

    dt_local = now_local(cfg.tz_name)

    if dt_local.weekday() != cfg.weekly_report_weekday:
        return False

    if not (dt_local.hour == cfg.weekly_report_hour and dt_local.minute == cfg.weekly_report_minute):
        return False

    w = state.get("weekly")
    if not isinstance(w, dict):
        return False

    week_start = w.get("week_start_local")
    last_sent_for = w.get("last_weekly_report_sent_for_start")

    # evita mandar mais de 1x no mesmo minuto/mesma semana
    if week_start and last_sent_for == week_start:
        return False

    return True


def send_weekly_report(cfg: Config, state: dict[str, Any]) -> None:
    w = state.get("weekly", {})
    if not isinstance(w, dict):
        return

    week_start_local = _parse_iso(w.get("week_start_local")) or now_local(cfg.tz_name)
    week_end_local = now_local(cfg.tz_name)

    incidents = int(w.get("incidents", 0) or 0)
    downtime_seconds = int(w.get("downtime_seconds", 0) or 0)
    slow_alerts = int(w.get("slow_alerts", 0) or 0)
    resp_min = w.get("resp_min_ms")
    resp_max = w.get("resp_max_ms")

    u24 = state.get("uptime_24h")
    u7 = state.get("uptime_7d")

    lines = [
        f"📅 Período: {week_start_local.strftime('%d/%m %H:%M')} → {week_end_local.strftime('%d/%m %H:%M')} ({cfg.tz_name})",
        f"🆔 Monitor: {cfg.monitor_id}",
        f"📉 Incidentes (quedas): {incidents}",
        f"⏱️ Downtime total: {fmt_duration(downtime_seconds)}",
        f"🐢 Alertas de lentidão: {slow_alerts}",
        f"⚡ Resp (min/max): {resp_min if resp_min is not None else '-'}ms / {resp_max if resp_max is not None else '-'}ms",
    ]

    if isinstance(u24, (int, float)):
        lines.append(f"✅ Uptime 24h (último): {float(u24):.3f}%")
    if isinstance(u7, (int, float)):
        lines.append(f"✅ Uptime 7d (último): {float(u7):.3f}%")

    msg = "\n".join(lines)

    # bypass rate limit pra não perder o resumo
    maybe_send(
        cfg,
        state,
        "📊 Resumo semanal (UptimeRobot)",
        msg,
        bypass_rate_limit=True,
    )

    w["last_weekly_report_sent_for_start"] = w.get("week_start_local")
    state["weekly"] = w
    save_state(state)


def run_loop() -> None:
    cfg = get_cfg()
    state = load_state()

    ensure_week_bucket(cfg, state)

    slow_streak = int(state.get("slow_streak", 0) or 0)
    last_uptime_check = state.get("last_uptime_check_at")

    print(
        f"[watcher] started. interval={cfg.watch_interval_s}s monitor={cfg.monitor_id} "
        f"slow>={cfg.slow_ms_threshold}ms consecutive={cfg.slow_consecutive} "
        f"uptime24h_min={cfg.uptime_24h_min}% uptime7d_min={cfg.uptime_7d_min}% "
        f"alert_min_interval={cfg.alert_min_interval_s}s recover_bypass={cfg.recover_bypass_rate_limit} "
        f"weekly={cfg.weekly_report_enabled} tz={cfg.tz_name}"
    )

    while True:
        started = time.time()

        try:
            ensure_week_bucket(cfg, state)

            payload = v3_get_monitor(cfg)

            status_text, status_code = extract_status(payload)
            resp_ms = extract_response_time_ms(payload)

            update_weekly_metrics(state, resp_ms=resp_ms)

            # DOWN detection (ajuste conforme seu payload)
            is_down = False
            if status_text:
                is_down = status_text in ("down", "seems_down", "paused", "unknown")
            if status_code is not None:
                is_down = status_code in (8, 9)

            prev_down = bool(state.get("is_down", False))

            if is_down and not prev_down:
                mark_weekly_down_transition(state)
                maybe_send(
                    cfg,
                    state,
                    "*🚨 sistema do wesley caiu, droga!!!*",
                    f"monitor/client id: {cfg.monitor_id} - wesley motos\nstatus: {status_text or status_code}\nhora: {now_utc().strftime('%d/%m %H:%M UTC')}",
                )

            if (not is_down) and prev_down:
                mark_weekly_up_transition(state)
                maybe_send(
                    cfg,
                    state,
                    "✅ sistema do wesley voltou caralhoouuuu!!!",
                    f"monitor/client id: {cfg.monitor_id}\nstatus: {status_text or status_code}\nhora: {now_utc().strftime('%d/%m %H:%M UTC')}",
                    bypass_rate_limit=cfg.recover_bypass_rate_limit,
                )

            # lentidão consecutiva
            is_slow = resp_ms is not None and resp_ms >= cfg.slow_ms_threshold
            slow_streak = slow_streak + 1 if is_slow else 0

            if cfg.slow_ms_threshold > 0 and slow_streak >= cfg.slow_consecutive:
                mark_weekly_slow_alert(state)
                maybe_send(
                    cfg,
                    state,
                    "⚠️ sistema do wesley lento pra caralhouuu, conserta!!!",
                    f"monitor/client id: {cfg.monitor_id}\nresp~{resp_ms}ms (limite {cfg.slow_ms_threshold}ms)\nHora={now_utc().strftime('%d/%m %H:%M UTC')}",
                )
                slow_streak = 0

            # Uptime 24h/7d em frequência menor
            do_uptime = True
            if isinstance(last_uptime_check, str) and last_uptime_check:
                last_dt = _parse_iso(last_uptime_check)
                if last_dt:
                    do_uptime = (now_utc() - last_dt).total_seconds() >= cfg.uptime_check_every_minutes * 60

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
                            "📉 uptime 24h low",
                            f"monitor/client id: {cfg.monitor_id}\nuptime 24h: {u24:.3f}% (mín {cfg.uptime_24h_min}%)",
                        )
                    if u7 is not None and u7 < cfg.uptime_7d_min:
                        maybe_send(
                            cfg,
                            state,
                            "📉 uptime 7d low",
                            f"monitor/client id: {cfg.monitor_id}\nuptime 7d: {u7:.3f}% (mín {cfg.uptime_7d_min}%)",
                        )

                state["last_uptime_check_at"] = _iso(now_utc())
                state["uptime_24h"] = u24
                state["uptime_7d"] = u7

            # ✅ dispara resumo semanal (1x)
            if should_send_weekly_report(cfg, state):
                send_weekly_report(cfg, state)

            # persist state
            state["is_down"] = is_down
            state["status"] = status_text or status_code
            state["resp_ms"] = resp_ms
            state["slow_streak"] = slow_streak
            state["last_check_at"] = _iso(now_utc())
            save_state(state)

            print(f"[watcher] ok status={state['status']} down={is_down} resp_ms={resp_ms} slow_streak={slow_streak}")

        except Exception as e:
            print(f"[watcher] ERROR polling uptimerobot v3: {e}")

        elapsed = time.time() - started
        time.sleep(max(1, cfg.watch_interval_s - int(elapsed)))


if __name__ == "__main__":
    run_loop()
