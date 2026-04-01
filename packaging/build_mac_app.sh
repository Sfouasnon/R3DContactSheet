#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "Cleaning previous build artifacts..."
rm -rf "$ROOT/build"
rm -rf "$ROOT/dist"/* 2>/dev/null || true
rmdir "$ROOT/dist" 2>/dev/null || true
rm -rf "$ROOT/dist"
rm -f "$ROOT/R3D Contact Sheet.spec"

echo "Installing packaging requirements..."
python3 -m pip install -r requirements.txt

echo "Building macOS app bundle..."
python3 -m PyInstaller -y --windowed --name "R3D Contact Sheet" --add-data "r3dcontactsheet_logo.png:." app.py

echo
echo "Build complete."
echo "App bundle:"
echo "  $ROOT/dist/R3D Contact Sheet.app"
