#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Install a packaged Pal release.

The wheel and pal_v2-runtime-root-overlay.tar.gz must be beside this script
unless their paths are supplied explicitly.

Usage:
  ./install-pal.sh [options]

Options:
  --runtime-root PATH  Pal runtime root (default: $PAL_RUNTIME_ROOT or ~/.pal)
  --install-root PATH  Dedicated Pal virtualenv root
                       (default: $PAL_INSTALL_ROOT or ~/.local/share/pal)
  --bin-dir PATH       Directory for the pal launcher
                       (default: $PAL_BIN_DIR or ~/.local/bin)
  --wheel PATH         Pal wheel to install
  --overlay PATH       Runtime-root overlay archive to extract
  -h, --help           Show this help

Environment:
  PAL_PYTHON           Python executable to use on Linux (must be 3.11+)

On macOS the installer always uses Homebrew Python. If Homebrew is absent, the
official Homebrew installer is run first.
EOF
}

fail() {
  echo "install-pal: $*" >&2
  exit 1
}

find_homebrew() {
  local candidate
  if command -v brew >/dev/null 2>&1; then
    command -v brew
    return 0
  fi
  for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [[ -x "$candidate" ]]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

find_packaged_artifact() {
  local pattern="$1"
  local label="$2"
  local candidate
  local found=""
  local count=0

  for candidate in "$script_dir"/$pattern; do
    if [[ -f "$candidate" ]]; then
      found="$candidate"
      count=$((count + 1))
    fi
  done
  if [[ "$count" -eq 0 ]]; then
    fail "no $label found beside $script_path"
  fi
  if [[ "$count" -ne 1 ]]; then
    fail "expected exactly one $label beside $script_path, found $count"
  fi
  echo "$found"
}

script_path="${BASH_SOURCE[0]}"
script_dir="$(cd "$(dirname "$script_path")" && pwd)"
runtime_root="${PAL_RUNTIME_ROOT:-$HOME/.pal}"
install_root="${PAL_INSTALL_ROOT:-$HOME/.local/share/pal}"
bin_dir="${PAL_BIN_DIR:-$HOME/.local/bin}"
wheel_path=""
overlay_path=""

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --runtime-root)
      [[ "$#" -ge 2 ]] || fail "--runtime-root requires a path"
      runtime_root="$2"
      shift 2
      ;;
    --install-root)
      [[ "$#" -ge 2 ]] || fail "--install-root requires a path"
      install_root="$2"
      shift 2
      ;;
    --bin-dir)
      [[ "$#" -ge 2 ]] || fail "--bin-dir requires a path"
      bin_dir="$2"
      shift 2
      ;;
    --wheel)
      [[ "$#" -ge 2 ]] || fail "--wheel requires a path"
      wheel_path="$2"
      shift 2
      ;;
    --overlay)
      [[ "$#" -ge 2 ]] || fail "--overlay requires a path"
      overlay_path="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
done

if [[ -z "$wheel_path" ]]; then
  wheel_path="$(find_packaged_artifact 'pal_v2-*.whl' 'Pal wheel')"
fi
if [[ -z "$overlay_path" ]]; then
  overlay_path="$(find_packaged_artifact 'pal_v2-runtime-root-overlay.tar.gz' 'runtime-root overlay')"
fi
[[ -f "$wheel_path" ]] || fail "wheel does not exist: $wheel_path"
[[ -f "$overlay_path" ]] || fail "runtime-root overlay does not exist: $overlay_path"

platform="$(uname -s)"
case "$platform" in
  Linux)
    python_bin="${PAL_PYTHON:-}"
    if [[ -z "$python_bin" ]]; then
      python_bin="$(command -v python3 || true)"
    fi
    [[ -n "$python_bin" && -x "$python_bin" ]] \
      || fail "Python 3 was not found; install Python 3.11+ or set PAL_PYTHON"
    ;;
  Darwin)
    brew_bin="$(find_homebrew || true)"
    if [[ -z "$brew_bin" ]]; then
      command -v curl >/dev/null 2>&1 \
        || fail "curl is required to install Homebrew"
      echo "Homebrew not found; running the official Homebrew installer..."
      /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
      brew_bin="$(find_homebrew || true)"
    fi
    [[ -n "$brew_bin" ]] || fail "Homebrew installation completed but brew was not found"

    echo "Ensuring Homebrew Python is installed..."
    "$brew_bin" install python
    brew_prefix="$("$brew_bin" --prefix)"
    python_formula_prefix="$("$brew_bin" --prefix python)"
    python_bin=""
    for candidate in \
      "$brew_prefix/bin/python3" \
      "$python_formula_prefix/libexec/bin/python3" \
      "$python_formula_prefix/bin/python3"; do
      if [[ -x "$candidate" ]]; then
        python_bin="$candidate"
        break
      fi
    done
    [[ -n "$python_bin" ]] || fail "Homebrew Python was installed but python3 was not found"
    ;;
  *)
    fail "unsupported operating system: $platform"
    ;;
esac

"$python_bin" - <<'PY'
from __future__ import annotations

import sys

if sys.version_info < (3, 11):
    raise SystemExit(
        "Pal requires Python 3.11+, found "
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
print(f"Using Python {sys.version.split()[0]} at {sys.executable}")
PY

venv_dir="$install_root/venv"
venv_python="$venv_dir/bin/python"
pal_bin="$venv_dir/bin/pal"

mkdir -p "$install_root"
echo "Creating Pal virtual environment at $venv_dir..."
"$python_bin" -m venv --clear "$venv_dir"

echo "Installing $(basename "$wheel_path")..."
"$venv_python" -m pip install --upgrade "$wheel_path"

echo "Verifying sqlite-vec can be loaded by the selected Python..."
"$venv_python" - <<'PY'
from __future__ import annotations

import sqlite3

import sqlite_vec

connection = sqlite3.connect(":memory:")
try:
    connection.enable_load_extension(True)
    sqlite_vec.load(connection)
    version = connection.execute("select vec_version()").fetchone()[0]
finally:
    connection.enable_load_extension(False)
    connection.close()
print(f"sqlite-vec {version} loaded successfully")
PY

"$pal_bin" --help >/dev/null

echo "Installing runtime-root overlay into $runtime_root..."
mkdir -p "$runtime_root"
tar -xzf "$overlay_path" -C "$runtime_root"

mkdir -p "$bin_dir"
launcher="$bin_dir/pal"
if [[ -d "$launcher" && ! -L "$launcher" ]]; then
  fail "cannot install launcher because a directory exists at $launcher"
fi
rm -f "$launcher"
ln -s "$pal_bin" "$launcher"

echo
echo "Pal installed successfully."
echo "  Python:       $venv_python"
echo "  Launcher:     $launcher"
echo "  Runtime root: $runtime_root"
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) echo "  PATH note:    add $bin_dir to PATH, or invoke $launcher directly" ;;
esac

echo
echo "Starting interactive Pal setup..."
"$pal_bin" setup --runtime-root "$runtime_root"

echo
echo "Running Pal dependency doctor..."
"$pal_bin" doctor
