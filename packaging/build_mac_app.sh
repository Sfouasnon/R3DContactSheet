#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 -m pip install -r requirements.txt
python3 -m PyInstaller -y --windowed --name "R3D Contact Sheet" app.py

echo
echo "Built app bundle:"
echo "  $ROOT/dist/R3D Contact Sheet.app"
