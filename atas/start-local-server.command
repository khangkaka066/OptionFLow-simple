#!/bin/zsh
set -euo pipefail
QUANT_PROJECT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
QUANT_PYTHON="$QUANT_PROJECT/.venv/bin/python"
if [[ ! -x "$QUANT_PYTHON" ]]; then
  QUANT_PYTHON=python3
fi
cd "$QUANT_PROJECT"
exec "$QUANT_PYTHON" -u scripts/live_server.py --host 127.0.0.1 --port 8899 --no-github-pull --skip-retention --secondary-ticker "" --interval-seconds 60
