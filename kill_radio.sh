#!/bin/bash
set +e
cd "$(dirname "$0")" || exit 1

PORT_FILE="${HOME}/.pi_radio_backend_port"
BACKEND_PID_FILE="${HOME}/.pi_radio_backend.pid"
UI_PID_FILE="${HOME}/.pi_radio_ui.pid"

echo "Stopping radio program..."

if [ -f "$PORT_FILE" ]; then
  PORT=$(tr -d '[:space:]' < "$PORT_FILE" 2>/dev/null)
  if [ -n "$PORT" ]; then
    python3 - "$PORT" <<'PY2'
import sys, urllib.request
port = sys.argv[1]
try:
    req = urllib.request.Request(f'http://127.0.0.1:{port}/shutdown', data=b'{"stop_playback": true}', method='POST', headers={'Content-Type':'application/json'})
    urllib.request.urlopen(req, timeout=2)
except Exception:
    pass
PY2
    sleep 1
  fi
fi

for FILE in "$UI_PID_FILE" "$BACKEND_PID_FILE"; do
  if [ -f "$FILE" ]; then
    PID=$(tr -d '[:space:]' < "$FILE" 2>/dev/null)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
      kill -TERM "$PID" 2>/dev/null
      sleep 1
      kill -KILL "$PID" 2>/dev/null
    fi
    rm -f "$FILE"
  fi
done

rm -f "$PORT_FILE"

echo "Cleanup complete."
