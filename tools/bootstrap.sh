#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
desk_python="${DESK_PYTHON:-python3}"
"$desk_python" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
"$desk_python" -m venv .venv
.venv/bin/python -m pip install -e . -r requirements-live.txt
.venv/bin/python -m unittest discover -s tests -v
printf '%s\n' 'Installed. Run .venv/bin/python -m desk doctor to see setup status.'
