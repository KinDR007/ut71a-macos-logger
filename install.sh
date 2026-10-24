#!/usr/bin/env bash
# Install everything needed to run UT71A Logger on a clean macOS.
# Safe to run repeatedly: installed parts are skipped.
#
# Usage: ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_FORMULA=python@3.14
# Version the project was tested with (Homebrew always installs its current version).
PYTHON_TESTED=3.14.5

for arg in "$@"; do
    case "$arg" in
        -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWarning:\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "This installer supports macOS only."

step "Homebrew"
if ! command -v brew >/dev/null 2>&1; then
    for prefix in /opt/homebrew /usr/local; do
        [ -x "$prefix/bin/brew" ] && eval "$("$prefix/bin/brew" shellenv)" && break
    done
fi
if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew not found, installing (asks for your password; installs Xcode Command Line Tools if needed)"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    for prefix in /opt/homebrew /usr/local; do
        [ -x "$prefix/bin/brew" ] && eval "$("$prefix/bin/brew" shellenv)" && break
    done
    command -v brew >/dev/null 2>&1 || die "Homebrew installation failed."
    echo "Add Homebrew to your shell:  echo 'eval \"\$($(command -v brew) shellenv)\"' >> ~/.zprofile"
fi
brew --version | head -1

step "Python ($PYTHON_FORMULA)"
if brew list --versions "$PYTHON_FORMULA" >/dev/null 2>&1; then
    echo "$PYTHON_FORMULA already installed"
else
    brew install "$PYTHON_FORMULA"
fi
installed=$(brew list --versions "$PYTHON_FORMULA" | awk '{print $NF}')
if [ "$installed" = "$PYTHON_TESTED" ]; then
    echo "$PYTHON_FORMULA $installed OK"
else
    warn "$PYTHON_FORMULA $installed installed, tested with $PYTHON_TESTED"
fi

step "Python virtual environment"
PY="$(brew --prefix "$PYTHON_FORMULA")/bin/python${PYTHON_FORMULA#python@}"
[ -x "$PY" ] || die "Python not found at $PY"
if [ -x .venv/bin/python ] && ! .venv/bin/python -c 'import sys' >/dev/null 2>&1; then
    echo "Existing .venv is broken, recreating"
    rm -rf .venv
fi
PYTHON="$PY" ./run.sh --setup

step "Self-test"
.venv/bin/python - <<'PY'
import hid, numpy, pyqtgraph
from PyQt6.QtCore import QT_VERSION_STR
import ut71a
print(f"hidapi OK, numpy {numpy.__version__}, pyqtgraph {pyqtgraph.__version__}, Qt {QT_VERSION_STR}")
assert ut71a.decode(b"000001101\r\n")["function"] == "DC V"
devs = hid.enumerate(ut71a.VID, ut71a.PID)
print("UT-D04 cable:", "connected" if devs else "not connected (that is fine for now)")
PY

chmod +x run.sh "UT71A Logger.command" ut71a.py ut71a_gui.py 2>/dev/null || true

step "Done"
cat <<EOF
Start the GUI:     ./run.sh   (or double-click "UT71A Logger.command")
Command line:      .venv/bin/python ut71a.py

On the meter hold SEND until "SEND" is shown on the display.
If the cable keeps disconnecting, quit Google Chrome (see README, Troubleshooting).
EOF
