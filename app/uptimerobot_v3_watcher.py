from __future__ import annotations

import os
import time
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from app.integrations.blibsend import send_whatsapp_text, BlibsendError


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
    alert_min_interval_s: int = 900


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
    return r.json()


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
    A v3 pode trazer campos diferentes dependendo do endpoint.
    """
    # Tentativas comuns:
    # payload["monitor"]["status"] ou payload["status"]
    mon = payload.get("monitor") if isinstance(payload.get("monitor"), dict) else payload
    status = mon.get("status")
    # status pode ser string ("up") ou número
    if isinstance(status, str):
        return status.lower(), None
    if isinstance(status, (int, float)):
        return None, int(status)
    return None, None


def extract_response_time_ms(payload: dict[str, Any]) -> Optional[int]:
    """
    Tenta achar response time recente/médio.
    Ajuste se a v3 retornar outro campo.
    """
    mon = payload.get("monitor") if isinstance(payload.get("monitor"), dict) else payload

    # Possíveis nomes:
    for key in ("response_time", "responseTime", "avg_response_time", "avgResponseTime", "last_response_time"):
        v = mon.get(key)
        ms = _try_int(v)
        if ms is not None:
            return ms

    # Às vezes vem dentro de "stats" ou "metrics"
    stats = mon.get("stats") or mon.get("metrics") or {}
    if isinstance(stats, dict):
        for key in ("response_time", "avg_response_time", "avgResponseTime"):
            ms = _try_int(stats.get(key))
            if ms is not None:
                return ms

    return None


def extract_uptime_ratios(payload: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    """
    Retorna (uptime_24h, uptime_7d) em %.
    A API pode devolver isso como "99.98" (string/num).
    """
    mon = payload.get("monitor") if isinstance(payload.get("monitor"), dict) else payload

    # Tentativas de campos diretos
    uptime_24h = _try_float(mon.get("uptime_24h") or mon.get("uptime24h") or mon.get("uptime_1d"))
    uptime_7d = _try_float(mon.get("uptime_7d") or mon.get("uptime7d") or mon.get("uptime_7days"))

    if uptime_24h is not None or uptime_7d is not None:
        return uptime_24h, uptime_7d

    # Tentativas por arrays/objetos de ratio
    ratios = mon.get("uptime") or mon.get("ratios") or mon.get("uptime_ratios")
    # Pode ser dict: {"24h": 99.9, "7d": 99.7}
    if isinstance(ratios, dict):
        uptime_24h = _try_float(ratios.get("24h") or ratios.get("1d"))
        uptime_7d = _try_float(ratios.get("7d") or ratios.get("7days"))
        return uptime_24h, uptime_7d

    # Pode ser list (às vezes uptime ratio em lista por períodos)
    # Se vier assim, você me cola um exemplo do JSON e eu mapeio certinho.
    return None, None


def maybe_send(cfg: Config, state: dict[str, Any], title: str, msg: str) -> None:
    last = state.get("last_alert_sent_at")
    if last:
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

    # contadores para “lentidão consecutiva”
    slow_streak = int(state.get("slow_streak", 0) or 0)
    last_uptime_check = state.get("last_uptime_check_at")  # iso
    print(
        f"[watcher] started. interval={cfg.watch_interval_s}s monitor={cfg.monitor_id} "
        f"slow>={cfg.slow_ms_threshold}ms consecutive={cfg.slow_consecutive} "
        f"uptime24h_min={cfg.uptime_24h_min}% uptime7d_min={cfg.uptime_7d_min}%"
    )

    while True:
        started = time.time()

        try:
            payload = v3_get_monitor(cfg)

            status_text, status_code = extract_status(payload)
            resp_ms = extract_response_time_ms(payload)

            # DOWN detection: aceitando string ou int
            # Ajuste conforme a v3 retornar no seu payload
            is_down = False
            if status_text:
                is_down = status_text in ("down", "seems_down", "paused", "unknown")
            if status_code is not None:
                # se vier parecido com v2: 2=up, 8=seems down, 9=down
                is_down = status_code in (8, 9)

            prev_down = bool(state.get("is_down", False))

            # DOWN alert
            if is_down and not prev_down:
                maybe_send(
                    cfg,
                    state,
                    "🚨 API DOWN",
                    f"Monitor {cfg.monitor_id}\nstatus={status_text or status_code}\nHora={now_utc().strftime('%d/%m %H:%M UTC')}",
                )

            # RECOVER alert
            if (not is_down) and prev_down:
                maybe_send(
                    cfg,
                    state,
                    "✅ API RECUPEROU",
                    f"Monitor {cfg.monitor_id}\nstatus={status_text or status_code}\nHora={now_utc().strftime('%d/%m %H:%M UTC')}",
                )

            # SLOW logic
            is_slow = resp_ms is not None and resp_ms >= cfg.slow_ms_threshold
            if is_slow:
                slow_streak += 1
            else:
                slow_streak = 0

            if cfg.slow_ms_threshold > 0 and slow_streak >= cfg.slow_consecutive:
                maybe_send(
                    cfg,
                    state,
                    "⚠️ API LENTA",
                    f"Monitor {cfg.monitor_id}\nresp~{resp_ms}ms (limite {cfg.slow_ms_threshold}ms)\nHora={now_utc().strftime('%d/%m %H:%M UTC')}",
                )
                # reseta pra não spammar todo ciclo
                slow_streak = 0

            # UPTIME ratios (24h/7d) com frequência menor
            do_uptime = True
            if last_uptime_check:
                try:
                    last_dt = datetime.fromisoformat(last_uptime_check)
                    do_uptime = (now_utc() - last_dt).total_seconds() >= cfg.uptime_check_every_minutes * 60
                except Exception:
                    do_uptime = True

            if do_uptime:
                u24, u7 = extract_uptime_ratios(payload)

                # se não conseguiu extrair, loga um resumo pra você me mandar e eu ajusto em 1 linha
                if u24 is None and u7 is None:
                    mon = payload.get("monitor") if isinstance(payload.get("monitor"), dict) else payload
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

            print(
                f"[watcher] ok status={state['status']} down={is_down} resp_ms={resp_ms} "
                f"slow_streak={slow_streak}"
            )

        except Exception as e:
            print(f"[watcher] ERROR polling uptimerobot v3: {e}")

        elapsed = time.time() - started
        time.sleep(max(1, cfg.watch_interval_s - int(elapsed)))


if __name__ == "__main__":
    run_loop()
