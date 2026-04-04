#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

APP_NAME="R3D Contact Sheet"
APP_BUNDLE="$ROOT/dist/${APP_NAME}.app"
APP_EXE="$APP_BUNDLE/Contents/MacOS/${APP_NAME}"
REQUESTED_ARCH="${R3DCS_TARGET_ARCH:-universal2}"
STRICT_TARGET_ARCH="${R3DCS_STRICT_TARGET_ARCH:-0}"
BUILD_LOG_DIR="$ROOT/build_logs"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: Required command not found: $1" >&2
    exit 1
  fi
}

archs_for_file() {
  local target="$1"
  if [[ ! -e "$target" ]]; then
    echo "missing"
    return 0
  fi
  if command -v lipo >/dev/null 2>&1; then
    local archs
    archs="$(lipo -archs "$target" 2>/dev/null || true)"
    if [[ -n "$archs" ]]; then
      echo "$archs"
      return 0
    fi
  fi
  file -b "$target" 2>/dev/null || echo "unknown"
}

normalize_arch_label() {
  local raw="$1"
  case "$raw" in
    *"x86_64 arm64"*|*"arm64 x86_64"*)
      echo "universal2"
      ;;
    *"arm64"*)
      echo "arm64 only"
      ;;
    *"x86_64"*)
      echo "x86_64 only"
      ;;
    *)
      echo "$raw"
      ;;
  esac
}

require_cmd python3
require_cmd file

HOST_ARCH="$(uname -m)"
PYTHON_EXEC="$(python3 - <<'PY'
import sys
print(sys.executable)
PY
)"
PYTHON_SYSCONFIG_PLATFORM="$(python3 - <<'PY'
import sysconfig
print(sysconfig.get_platform())
PY
)"
PYTHON_ARCHS_RAW="$(archs_for_file "$PYTHON_EXEC")"
PYTHON_ARCHS="$(normalize_arch_label "$PYTHON_ARCHS_RAW")"

TARGET_ARCH="$REQUESTED_ARCH"
if [[ "$REQUESTED_ARCH" == "universal2" && "$PYTHON_SYSCONFIG_PLATFORM" != *"universal2"* ]]; then
  echo "WARNING: Requested universal2 build, but this Python interpreter is not a universal2 build." >&2
  echo "WARNING: Falling back to native architecture build for host ${HOST_ARCH}." >&2
  TARGET_ARCH="$HOST_ARCH"
fi

echo "Build environment:"
echo "  Host architecture:            $HOST_ARCH"
echo "  Python executable:            $PYTHON_EXEC"
echo "  Python executable arch:       $PYTHON_ARCHS"
echo "  Python sysconfig platform:    $PYTHON_SYSCONFIG_PLATFORM"
echo "  Requested build architecture: $REQUESTED_ARCH"
echo "  Effective build architecture: $TARGET_ARCH"

echo
echo "Cleaning previous build artifacts..."
rm -rf "$ROOT/build"
rm -rf "$BUILD_LOG_DIR"
rm -rf "$ROOT/dist"/* 2>/dev/null || true
rmdir "$ROOT/dist" 2>/dev/null || true
rm -rf "$ROOT/dist"
rm -f "$ROOT/R3D Contact Sheet.spec"
mkdir -p "$BUILD_LOG_DIR"

echo "Installing packaging requirements..."
python3 -m pip install -r requirements.txt

run_pyinstaller_build() {
  local target_arch="$1"
  local log_file="$BUILD_LOG_DIR/pyinstaller_${target_arch}.log"
  echo "Building macOS app bundle for target arch: $target_arch"
  set +e
  python3 -m PyInstaller \
    -y \
    --clean \
    --windowed \
    --name "$APP_NAME" \
    --target-arch "$target_arch" \
    --add-data "r3dcontactsheet_logo.png:." \
    app.py 2>&1 | tee "$log_file"
  local status=${PIPESTATUS[0]}
  set -e
  return "$status"
}

ACTUAL_TARGET_ARCH="$TARGET_ARCH"
if ! run_pyinstaller_build "$TARGET_ARCH"; then
  if [[ "$TARGET_ARCH" == "universal2" ]]; then
    echo "WARNING: universal2 build failed in this environment." >&2
    echo "WARNING: A packaged Python dependency is likely single-architecture, so PyInstaller cannot emit a fat app bundle." >&2
    echo "WARNING: See build log: $BUILD_LOG_DIR/pyinstaller_${TARGET_ARCH}.log" >&2
    if [[ "$STRICT_TARGET_ARCH" == "1" ]]; then
      echo "ERROR: Strict target architecture mode is enabled; refusing fallback to a native build." >&2
      exit 1
    fi
    echo "WARNING: Falling back to a native ${HOST_ARCH} build for this machine." >&2
    rm -rf "$ROOT/build"
    rm -rf "$ROOT/dist"
    rm -f "$ROOT/R3D Contact Sheet.spec"
    ACTUAL_TARGET_ARCH="$HOST_ARCH"
    run_pyinstaller_build "$ACTUAL_TARGET_ARCH"
  else
    echo "ERROR: Build failed for target architecture $TARGET_ARCH." >&2
    echo "ERROR: See build log: $BUILD_LOG_DIR/pyinstaller_${TARGET_ARCH}.log" >&2
    exit 1
  fi
fi

if [[ ! -x "$APP_EXE" ]]; then
  echo "ERROR: Expected app executable not found at $APP_EXE" >&2
  exit 1
fi

APP_ARCHS_RAW="$(archs_for_file "$APP_EXE")"
APP_ARCHS="$(normalize_arch_label "$APP_ARCHS_RAW")"

PYTHON_RUNTIME=""
if [[ -d "$APP_BUNDLE/Contents/Frameworks" ]]; then
  PYTHON_RUNTIME="$(find "$APP_BUNDLE/Contents/Frameworks" -type f \( -name Python -o -name Python3 \) | head -n 1 || true)"
fi
PYTHON_RUNTIME_ARCHS="not found"
if [[ -n "$PYTHON_RUNTIME" ]]; then
  PYTHON_RUNTIME_ARCHS="$(normalize_arch_label "$(archs_for_file "$PYTHON_RUNTIME")")"
fi

echo
echo "Post-build architecture report:"
echo "  Built app executable:         $APP_EXE"
echo "  Built app architecture:       $APP_ARCHS"
echo "  Embedded Python runtime:      ${PYTHON_RUNTIME:-not found}"
echo "  Embedded Python arch:         $PYTHON_RUNTIME_ARCHS"
echo "  Build logs:                   $BUILD_LOG_DIR"

if [[ "$REQUESTED_ARCH" == "universal2" && "$APP_ARCHS" != "universal2" ]]; then
  echo "WARNING: Requested universal2, but the built app is $APP_ARCHS." >&2
  echo "WARNING: This app may not run on Macs that do not match that architecture." >&2
  echo "WARNING: Likely limitation: one or more packaged Python dependencies are not universal2 in this environment." >&2
  if [[ "$STRICT_TARGET_ARCH" == "1" ]]; then
    echo "ERROR: Strict target architecture mode is enabled; refusing single-architecture output." >&2
    exit 1
  fi
fi

echo
echo "Build complete:"
echo "  App: $APP_BUNDLE"
echo "  Architecture: $APP_ARCHS"
echo "  REDline bundled: no"
echo "  External requirement: REDCINE-X PRO / REDline installed on target Mac"
