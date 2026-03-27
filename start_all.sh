#!/bin/bash
set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

PORT_FILE="${HOME}/.pi_radio_backend_port"
BACKEND_PID_FILE="${HOME}/.pi_radio_backend.pid"
UI_PID_FILE="${HOME}/.pi_radio_ui.pid"
LOG_FILE="backend.out"

backend_ready() {
  local p="$1"
  [ -n "$p" ] || return 1
  python3 - "$p" <<'PY2'
import json, sys, urllib.request
port = sys.argv[1]
try:
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/status', timeout=2.0) as r:
        data = json.loads(r.read().decode('utf-8'))
    raise SystemExit(0 if data.get('ok') else 1)
except Exception:
    raise SystemExit(1)
PY2
}

kill_pidfile() {
  local file="$1"
  if [ -f "$file" ]; then
    local pid
    pid=$(tr -d '[:space:]' < "$file" 2>/dev/null)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
      sleep 1
      kill -KILL "$pid" 2>/dev/null || true
    fi
    rm -f "$file"
  fi
}

stop_stale_processes() {
  kill_pidfile "$UI_PID_FILE"
  kill_pidfile "$BACKEND_PID_FILE"
  rm -f "$PORT_FILE"
}

port=""
if [ -f "$PORT_FILE" ]; then
  port=$(tr -d '[:space:]' < "$PORT_FILE")
fi

if backend_ready "$port"; then
  echo "Backend already running on port $port"
else
  echo "Starting clean radio backend..."
  stop_stale_processes
  : > "$LOG_FILE"
  nohup env PYTHONUNBUFFERED=1 python3 -u radio_backend.py >> "$LOG_FILE" 2>&1 &

  started=0
  for _ in $(seq 1 20); do
    sleep 1
    if [ -f "$PORT_FILE" ]; then
      port=$(tr -d '[:space:]' < "$PORT_FILE")
      if backend_ready "$port"; then
        started=1
        break
      fi
    fi
  done

  if [ "$started" -ne 1 ]; then
    echo "Backend failed to start. Last log lines:"
    tail -n 80 "$LOG_FILE" 2>/dev/null || true
    exit 1
  fi
  echo "Backend started on port $port"
fi

echo $$ > "$UI_PID_FILE"
exec python3 radio_ui.py
