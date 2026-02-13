#!/bin/sh
set -e

echo ">>> starting WI Alerts (web + watcher)"

# (opcional) log de ambiente útil
echo ">>> PORT=${PORT:-8000}"
echo ">>> WATCHER=python -u -m app.uptimerobot_v3_watcher"

# inicia watcher em background
python -u -m app.uptimerobot_v3_watcher &
WATCHER_PID=$!
echo ">>> watcher started pid=$WATCHER_PID"

# garante shutdown limpo
term_handler() {
  echo ">>> received termination, stopping watcher pid=$WATCHER_PID"
  kill -TERM "$WATCHER_PID" 2>/dev/null || true
  wait "$WATCHER_PID" 2>/dev/null || true
  echo ">>> watcher stopped"
  exit 0
}

trap term_handler TERM INT

# inicia web em foreground (mantém container vivo)
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
