#!/bin/bash
set -u
cd "$(dirname "$0")" || exit 1
export PYTHONUNBUFFERED=1
echo $$ > "${HOME}/.pi_radio_backend.pid"
exec python3 -u radio_backend.py
