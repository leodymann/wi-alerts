# app/uptimerobot_v3_watcher.py
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import requests

from app.integrations.blibsend_http import BlibsendError, send_whatsapp_text

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


STATE_DIR = Path(".watcher_state")
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "weekly_probe_state.json"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_local(tz_name: str) -> datetime:
    # Windows pode não ter tzdata -> fallback pro timezone do sistema
    if ZoneInfo is None:
        return datetime.now().astimezone()
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now().astimezone()


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(s: Any) -> Optional[datetime]:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


@dataclass
class Config:
    api_url: str
    alert_to: str

    # loop
    watch_interval_s: int = 60

    # probe
    probe_path: str = "/health"
    probe_timeout_s: int = 10

    # slow detection
    slow_ms_threshold: int = 2500
    slow_consecutive: int = 3

    # down detection (probe fail)
    fail_consecutive: int = 3  # quantas falhas seguidas pra alertar "fora do ar"

    # alerts
    alert_min_interval_s: int = 900
    recover_bypass_rate_limit: bool = True

    # weekly report
    tz_name: str = "America/Fortaleza"
    weekly_report_enabled: bool = True
    weekly_report_weekday: int = 0  # 0=Mon ... 6=Sun
    weekly_report_hour: int = 9
    weekly_report_minute: int = 0

    # ✅ janela de tolerância (min) para não depender do minuto exato
    weekly_report_window_minutes: int = 180  # 3h default


def get_cfg() -> Config:
    api_url = (os.getenv("API_URL") or "").strip()
    alert_to = (os.getenv("ALERT_TO") or "").strip()

    if not api_url:
        raise RuntimeError("API_URL não configurado.")
    if not alert_to:
        raise RuntimeError("ALERT_TO não configurado.")

    return Config(
        api_url=api_url,
        alert_to=alert_to,
        watch_interval_s=int(os.getenv("WATCH_INTERVAL_SECONDS", "60")),
        probe_path=(os.getenv("PROBE_PATH") or "/health").strip(),
        probe_timeout_s=int(os.getenv("PROBE_TIMEOUT_SECONDS", "10")),
        slow_ms_threshold=int(os.getenv("SLOW_MS_THRESHOLD", "2500")),
        slow_consecutive=int(os.getenv("SLOW_CONSECUTIVE", "3")),
        fail_consecutive=int(os.getenv("FAIL_CONSECUTIVE", "3")),
        alert_min_interval_s=int(os.getenv("ALERT_MIN_INTERVAL_SECONDS", "900")),
        recover_bypass_rate_limit=os.getenv("RECOVER_BYPASS_RATE_LIMIT", "1").strip() in ("1", "true", "True"),
        tz_name=(os.getenv("TZ_NAME") or "America/Fortaleza").strip(),
        weekly_report_enabled=(os.getenv("WEEKLY_REPORT_ENABLED", "1").strip() in ("1", "true", "True")),
        weekly_report_weekday=int(os.getenv("WEEKLY_REPORT_WEEKDAY", "0")),
        weekly_report_hour=int(os.getenv("WEEKLY_REPORT_HOUR", "9")),
        weekly_report_minute=int(os.getenv("WEEKLY_REPORT_MINUTE", "0")),
        # ✅ nova env opcional
        weekly_report_window_minutes=int(os.getenv("WEEKLY_REPORT_WINDOW_MINUTES", "180")),
    )


def maybe_send(cfg: Config, state: dict[str, Any], title: str, msg: str, *, bypass_rate_limit: bool = False) -> None:
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


def probe_api(cfg: Config) -> tuple[Optional[int], Optional[int]]:
    """
    Retorna (ms, status_code) ou (None, None) se falhar.
    Consideramos "probe OK" somente se status 2xx.
    """
    base = cfg.api_url.rstrip("/")
    path = cfg.probe_path if cfg.probe_path.startswith("/") else ("/" + cfg.probe_path)
    url = base + path

    try:
        t0 = time.time()
        r = requests.get(url, timeout=cfg.probe_timeout_s)
        ms = int((time.time() - t0) * 1000)
        return ms, r.status_code
    except Exception:
        return None, None


# ---------------------------
# Weekly bucket
# ---------------------------

def _start_of_week_local(dt_local: datetime) -> datetime:
    weekday = dt_local.weekday()  # 0=Mon
    return dt_local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=weekday)


def ensure_week_bucket(cfg: Config, state: dict[str, Any]) -> None:
    dt_local = now_local(cfg.tz_name)
    week_start = _start_of_week_local(dt_local)

    cur = state.get("weekly", {})
    cur_start = _parse_iso(cur.get("week_start_local")) if isinstance(cur, dict) else None

    if not isinstance(cur, dict) or cur_start is None or cur_start != week_start:
        state["weekly"] = {
            "week_start_local": _iso(week_start),
            "slow_alerts": 0,
            "outage_alerts": 0,
            "probe_failures": 0,
            "probe_http_non_2xx": 0,
            "probe_min_ms": None,
            "probe_max_ms": None,
            "downtime_seconds": 0,
            "down_started_at_utc": None,
            "last_weekly_report_sent_for_start": None,
        }


def update_weekly_probe_metrics(state: dict[str, Any], *, probe_ms: Optional[int], http_status: Optional[int]) -> None:
    w = state.get("weekly")
    if not isinstance(w, dict):
        return

    if probe_ms is None:
        w["probe_failures"] = int(w.get("probe_failures", 0) or 0) + 1
        return

    if http_status is not None and not (200 <= int(http_status) < 300):
        w["probe_http_non_2xx"] = int(w.get("probe_http_non_2xx", 0) or 0) + 1

    mn = w.get("probe_min_ms")
    mx = w.get("probe_max_ms")
    if mn is None or probe_ms < int(mn):
        w["probe_min_ms"] = probe_ms
    if mx is None or probe_ms > int(mx):
        w["probe_max_ms"] = probe_ms


def mark_weekly_slow_alert(state: dict[str, Any]) -> None:
    w = state.get("weekly")
    if isinstance(w, dict):
        w["slow_alerts"] = int(w.get("slow_alerts", 0) or 0) + 1


def mark_weekly_outage_start(state: dict[str, Any]) -> None:
    w = state.get("weekly")
    if not isinstance(w, dict):
        return
    w["outage_alerts"] = int(w.get("outage_alerts", 0) or 0) + 1
    if not w.get("down_started_at_utc"):
        w["down_started_at_utc"] = _iso(now_utc())


def mark_weekly_outage_end(state: dict[str, Any]) -> None:
    w = state.get("weekly")
    if not isinstance(w, dict):
        return
    started = _parse_iso(w.get("down_started_at_utc"))
    if started:
        w["downtime_seconds"] = int(w.get("downtime_seconds", 0) or 0) + int((now_utc() - started).total_seconds())
    w["down_started_at_utc"] = None


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
    """
    ✅ Versão robusta:
    - não depende do minuto exato
    - manda 1x por semana quando o horário alvo já passou
    - limita envio a uma janela (default 180 min) após o horário alvo
    """
    if not cfg.weekly_report_enabled:
        return False

    dt_local = now_local(cfg.tz_name)

    # só no dia configurado
    if dt_local.weekday() != cfg.weekly_report_weekday:
        return False

    w = state.get("weekly")
    if not isinstance(w, dict):
        return False

    week_start = w.get("week_start_local")
    last_sent_for = w.get("last_weekly_report_sent_for_start")

    # já mandou nessa semana
    if week_start and last_sent_for == week_start:
        return False

    # horário alvo no dia de hoje (local)
    target = dt_local.replace(
        hour=cfg.weekly_report_hour,
        minute=cfg.weekly_report_minute,
        second=0,
        microsecond=0,
    )

    # se ainda não chegou no horário, não manda
    if dt_local < target:
        return False

    # janela de tolerância
    window_sec = int(cfg.weekly_report_window_minutes) * 60
    if (dt_local - target).total_seconds() > window_sec:
        return False

    return True


def send_weekly_report(cfg: Config, state: dict[str, Any]) -> None:
    w = state.get("weekly")
    if not isinstance(w, dict):
        return

    week_start_local = _parse_iso(w.get("week_start_local")) or now_local(cfg.tz_name)
    week_end_local = now_local(cfg.tz_name)

    slow_alerts = int(w.get("slow_alerts", 0) or 0)
    outage_alerts = int(w.get("outage_alerts", 0) or 0)
    downtime_seconds = int(w.get("downtime_seconds", 0) or 0)
    probe_failures = int(w.get("probe_failures", 0) or 0)
    probe_non2xx = int(w.get("probe_http_non_2xx", 0) or 0)
    probe_min = w.get("probe_min_ms")
    probe_max = w.get("probe_max_ms")

    lines = [
        f"📅 Período: {week_start_local.strftime('%d/%m %H:%M')} → {week_end_local.strftime('%d/%m %H:%M')} ({cfg.tz_name})",
        f"🌐 API: {cfg.api_url}",
        f"🐢 Alertas de lentidão: {slow_alerts} (limite {cfg.slow_ms_threshold}ms, {cfg.slow_consecutive}x seguidas)",
        f"🚫 Alertas de indisponibilidade (probe): {outage_alerts} (falhas seguidas >= {cfg.fail_consecutive})",
        f"⏱️ Downtime total (probe): {fmt_duration(downtime_seconds)}",
        f"🧪 Probe {cfg.probe_path} (min/max): {probe_min if probe_min is not None else '-'}ms / {probe_max if probe_max is not None else '-'}ms",
        f"🧪 Probe falhas: {probe_failures} | HTTP não-2xx: {probe_non2xx}",
        "ℹ️ Queda/recuperação oficial continua vindo pelo webhook do UptimeRobot no serviço FastAPI.",
    ]

    maybe_send(cfg, state, "📊 Resumo semanal (WI Alerts)", "\n".join(lines), bypass_rate_limit=True)

    w["last_weekly_report_sent_for_start"] = w.get("week_start_local")
    state["weekly"] = w
    save_state(state)


def run_loop() -> None:
    cfg = get_cfg()
    state = load_state()

    ensure_week_bucket(cfg, state)

    slow_streak = int(state.get("slow_streak", 0) or 0)
    fail_streak = int(state.get("fail_streak", 0) or 0)
    is_down = bool(state.get("is_down", False))

    print(
        f"[watcher] started. interval={cfg.watch_interval_s}s api={cfg.api_url} probe={cfg.probe_path} "
        f"slow>={cfg.slow_ms_threshold}ms consecutive={cfg.slow_consecutive} fail_consecutive={cfg.fail_consecutive} "
        f"weekly={cfg.weekly_report_enabled} tz={cfg.tz_name} window_min={cfg.weekly_report_window_minutes}"
    )

    while True:
        started = time.time()

        try:
            ensure_week_bucket(cfg, state)

            probe_ms, http_status = probe_api(cfg)
            update_weekly_probe_metrics(state, probe_ms=probe_ms, http_status=http_status)

            ok_http = http_status is not None and (200 <= int(http_status) < 300)
            probe_ok = (probe_ms is not None) and ok_http

            # indisponibilidade por falhas seguidas
            fail_streak = fail_streak + 1 if not probe_ok else 0

            prev_down = is_down
            is_down = fail_streak >= cfg.fail_consecutive

            if is_down and not prev_down:
                mark_weekly_outage_start(state)
                maybe_send(
                    cfg,
                    state,
                    "🚨 API fora do ar (probe)",
                    f"API: {cfg.api_url}\nprobe: {cfg.probe_path}\nhttp={http_status}\nms={probe_ms}\nhora: {now_utc().strftime('%d/%m %H:%M UTC')}",
                )

            if (not is_down) and prev_down:
                mark_weekly_outage_end(state)
                maybe_send(
                    cfg,
                    state,
                    "✅ API voltou (probe)",
                    f"API: {cfg.api_url}\nprobe: {cfg.probe_path}\nhttp={http_status}\nms={probe_ms}\nhora: {now_utc().strftime('%d/%m %H:%M UTC')}",
                    bypass_rate_limit=cfg.recover_bypass_rate_limit,
                )

            # lentidão (somente quando probe OK)
            is_slow = probe_ok and (probe_ms is not None) and probe_ms >= cfg.slow_ms_threshold
            slow_streak = slow_streak + 1 if is_slow else 0

            if cfg.slow_ms_threshold > 0 and slow_streak >= cfg.slow_consecutive:
                mark_weekly_slow_alert(state)
                maybe_send(
                    cfg,
                    state,
                    "⚠️ API lenta (probe)",
                    f"API: {cfg.api_url}\nprobe: {cfg.probe_path}\nresp~{probe_ms}ms (limite {cfg.slow_ms_threshold}ms)\nHora={now_utc().strftime('%d/%m %H:%M UTC')}",
                )
                slow_streak = 0

            # resumo semanal (✅ com janela)
            if should_send_weekly_report(cfg, state):
                send_weekly_report(cfg, state)

            # persist
            state["slow_streak"] = slow_streak
            state["fail_streak"] = fail_streak
            state["is_down"] = is_down
            state["last_probe_ms"] = probe_ms
            state["last_http_status"] = http_status
            state["last_check_at"] = _iso(now_utc())
            save_state(state)

            print(
                f"[watcher] ok probe_ms={probe_ms} http={http_status} "
                f"probe_ok={probe_ok} fail_streak={fail_streak} slow_streak={slow_streak} down={is_down}"
            )

        except Exception as e:
            print(f"[watcher] ERROR: {e}")

        elapsed = time.time() - started
        time.sleep(max(1, cfg.watch_interval_s - int(elapsed)))


if __name__ == "__main__":
    run_loop()
