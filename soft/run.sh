#!/usr/bin/env bash
# Launch the soft-synth match server (drop-an-audio-file → gradient-match a patch).
# Port from SOFT_PORT (default 8767, 127.0.0.1 only). Then open the URL it prints.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${SOFT_PORT:-8767}"
PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY="python3"
echo "s1 soft (match) → http://127.0.0.1:${PORT}"
exec env SOFT_PORT="$PORT" "$PY" -m soft.server
