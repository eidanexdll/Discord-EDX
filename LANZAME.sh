#!/bin/sh
cd "$(dirname "$0")"
python3 -c "import rich" 2>/dev/null || python3 -m pip install --user rich
python3 main.py "$@"
