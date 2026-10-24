#!/usr/bin/env bash
# Create/update .venv from requirements.txt and start the GUI.
# Usage: ./run.sh [recording.csv]   start the GUI
#        ./run.sh --setup           only prepare .venv
set -euo pipefail
cd "$(dirname "$0")"

VENV=.venv
PY="${PYTHON:-python3}"

if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating virtual environment in $VENV ($("$PY" --version))"
    "$PY" -m venv "$VENV"
fi

STAMP="$VENV/.requirements.sha256"
SUM=$(shasum -a 256 requirements.txt | cut -d' ' -f1)
if [ "$(cat "$STAMP" 2>/dev/null)" != "$SUM" ]; then
    echo "Installing Python dependencies"
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
    "$VENV/bin/python" -m pip install --quiet -r requirements.txt
    echo "$SUM" > "$STAMP"
fi

if [ "${1:-}" = "--setup" ]; then
    exit 0
fi

exec "$VENV/bin/python" ut71a_gui.py "$@"
