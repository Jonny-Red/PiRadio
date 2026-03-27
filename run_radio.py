#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND = SCRIPT_DIR / 'radio_backend.py'
UI = SCRIPT_DIR / 'radio_ui.py'
PYTHON = sys.executable or 'python3'
PORT_FILE = Path.home() / '.pi_radio_backend_port'
BACKEND_PID_FILE = Path.home() / '.pi_radio_backend.pid'
BACKEND_LOG = SCRIPT_DIR / 'backend.out'


def backend_ready(port: str | int | None) -> bool:
    if not port:
        return False
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{int(port)}/status', timeout=2.0) as r:
            data = json.loads(r.read().decode('utf-8'))
        return bool(data.get('ok'))
    except Exception:
        return False


def stop_stale_backend() -> None:
    try:
        if BACKEND_PID_FILE.exists():
            pid = int(BACKEND_PID_FILE.read_text().strip())
            try:
                os.kill(pid, 15)
                time.sleep(1)
            except Exception:
                pass
            try:
                os.kill(pid, 9)
            except Exception:
                pass
            BACKEND_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        PORT_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def start_backend() -> int:
    port = None
    try:
        if PORT_FILE.exists():
            port = PORT_FILE.read_text().strip()
    except Exception:
        port = None
    if backend_ready(port):
        return int(port)

    stop_stale_backend()
    BACKEND_LOG.write_text('')
    with open(BACKEND_LOG, 'ab', buffering=0) as logf:
        subprocess.Popen(
            [PYTHON, '-u', str(BACKEND)],
            cwd=str(SCRIPT_DIR),
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    for _ in range(20):
        time.sleep(1)
        try:
            if PORT_FILE.exists():
                port = PORT_FILE.read_text().strip()
                if backend_ready(port):
                    return int(port)
        except Exception:
            pass

    try:
        tail = BACKEND_LOG.read_text(errors='replace').splitlines()[-80:]
    except Exception:
        tail = ['(unable to read backend.out)']
    print('Backend failed to start. Last log lines:\n', file=sys.stderr)
    print('\n'.join(tail), file=sys.stderr)
    raise SystemExit(1)


def launch_ui() -> int:
    try:
        proc = subprocess.run([PYTHON, str(UI)], cwd=str(SCRIPT_DIR))
        return proc.returncode
    except FileNotFoundError:
        print('Python was not found when trying to launch the UI.', file=sys.stderr)
        return 1
    except Exception as exc:
        print(f'UI failed to launch: {exc}', file=sys.stderr)
        return 1


def main() -> int:
    if not BACKEND.exists() or not UI.exists():
        print('Missing program files. Make sure run_radio.py is in the same folder as radio_backend.py and radio_ui.py.', file=sys.stderr)
        return 1

    try:
        import tkinter  # noqa: F401
    except Exception as exc:
        print('Tkinter is required for the desktop UI but is not available on this system.', file=sys.stderr)
        print(f'Details: {exc}', file=sys.stderr)
        return 1

    port = start_backend()
    print(f'Backend ready on http://127.0.0.1:{port}')
    return launch_ui()


if __name__ == '__main__':
    raise SystemExit(main())
