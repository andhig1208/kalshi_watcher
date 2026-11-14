#!/usr/bin/env bash
# Kalshi paper watcher: check Python, generate test data, run every test.
# Paper mode only. Needs no Kalshi keys. Standard library only, no pip installs.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Install it with your package manager (e.g. sudo apt install python3)."
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
  echo "Python 3.8 or newer is required. Found: $(python3 --version)"
  exit 1
fi
echo "Python OK: $(python3 --version)"

echo "Generating fair-market test data (1,500 windows per asset)..."
python3 make_synthetic.py --n 1500 --seed 11 \
  --windows_out w1500.csv --quotes_out q1500.csv --candles_out c1500.csv

echo "Running tests..."
python3 test_engine.py
python3 test_live_gate.py
python3 test_watcher.py
python3 test_watcher_safety.py

echo
echo "All tests passed. Paper watcher is ready."
echo "Start it with:   python3 watcher.py"
echo "Stop it with:    touch KILL   (flattens any open paper position and halts)"
