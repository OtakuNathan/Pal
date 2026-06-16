#!/usr/bin/env bash
set -euo pipefail

# Cleanup-only macOS helper for Pal installs.
#
# It removes python.org's Python for one minor version, removes stale
# python.org symlinks, uninstalls Playwright from likely Python installs, and
# clears Playwright browser caches. It does not install Homebrew Python, Pal, or
# Chromium.
#
# Usage:
#   scripts/macos_repair_python_playwright.sh
#   FORCE=1 scripts/macos_repair_python_playwright.sh
#   PYTHON_MINOR=3.12 FORCE=1 scripts/macos_repair_python_playwright.sh

PYTHON_MINOR="${PYTHON_MINOR:-3.13}"
FORCE="${FORCE:-0}"
STOP_PAL="${STOP_PAL:-1}"
REMOVE_PYTHON_ORG="${REMOVE_PYTHON_ORG:-1}"
REMOVE_PLAYWRIGHT_PACKAGES="${REMOVE_PLAYWRIGHT_PACKAGES:-1}"
CLEAR_PLAYWRIGHT_CACHE="${CLEAR_PLAYWRIGHT_CACHE:-1}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script is intended for macOS only." >&2
  exit 1
fi

PYTHON_FRAMEWORK="/Library/Frameworks/Python.framework/Versions/$PYTHON_MINOR"
PYTHON_APP="/Applications/Python $PYTHON_MINOR"
LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"

pal_plists=()
if [[ -d "$LAUNCH_AGENT_DIR" ]]; then
  while IFS= read -r -d '' plist; do
    pal_plists+=("$plist")
  done < <(find "$LAUNCH_AGENT_DIR" -maxdepth 1 -name 'com.pal*.plist' -print0 2>/dev/null)
fi

plist_label() {
  local plist="$1"
  /usr/libexec/PlistBuddy -c 'Print :Label' "$plist" 2>/dev/null || basename "$plist" .plist
}

confirm() {
  if [[ "$FORCE" == "1" ]]; then
    return
  fi

  cat <<EOF
This cleanup will:
  - stop Pal LaunchAgents matching ~/Library/LaunchAgents/com.pal*.plist
  - remove: $PYTHON_FRAMEWORK
  - remove: $PYTHON_APP
  - remove /usr/local/bin symlinks pointing into that Python framework
  - uninstall Playwright from likely Python interpreters when possible
  - remove Playwright browser caches

It will not touch /usr/bin/python3.

Type CLEAN to continue:
EOF

  local answer
  read -r answer
  if [[ "$answer" != "CLEAN" ]]; then
    echo "Aborted."
    exit 1
  fi
}

stop_pal_launchagents() {
  if [[ "$STOP_PAL" != "1" || "${#pal_plists[@]}" -eq 0 ]]; then
    return
  fi

  echo "Stopping Pal LaunchAgents..."
  for plist in "${pal_plists[@]}"; do
    local label
    label="$(plist_label "$plist")"
    launchctl bootout "gui/$UID/$label" >/dev/null 2>&1 || true
  done
}

remove_python_org_symlinks() {
  local bin_dir="/usr/local/bin"
  if [[ ! -d "$bin_dir" ]]; then
    return
  fi

  echo "Removing python.org symlinks from $bin_dir..."
  local path target
  for path in "$bin_dir"/*; do
    [[ -L "$path" ]] || continue
    target="$(readlink "$path" || true)"
    if [[ "$target" == "$PYTHON_FRAMEWORK/"* || "$target" == "/Library/Frameworks/Python.framework/Versions/$PYTHON_MINOR/"* ]]; then
      sudo rm -f "$path"
    fi
  done
}

remove_python_org_framework() {
  if [[ "$REMOVE_PYTHON_ORG" != "1" ]]; then
    return
  fi

  echo "Removing python.org Python $PYTHON_MINOR..."
  if [[ -d "$PYTHON_FRAMEWORK" ]]; then
    sudo rm -rf "$PYTHON_FRAMEWORK"
  fi
  if [[ -d "$PYTHON_APP" ]]; then
    sudo rm -rf "$PYTHON_APP"
  fi
}

candidate_pythons() {
  local candidates=()
  candidates+=("/Library/Frameworks/Python.framework/Versions/$PYTHON_MINOR/bin/python$PYTHON_MINOR")
  candidates+=("/Library/Frameworks/Python.framework/Versions/$PYTHON_MINOR/bin/python3")
  candidates+=("/usr/local/bin/python$PYTHON_MINOR")
  candidates+=("/usr/local/bin/python3")
  candidates+=("/opt/homebrew/bin/python$PYTHON_MINOR")
  candidates+=("/usr/local/bin/python$PYTHON_MINOR")
  if command -v python3 >/dev/null 2>&1; then
    candidates+=("$(command -v python3)")
  fi

  local seen=""
  local candidate
  for candidate in "${candidates[@]}"; do
    [[ -x "$candidate" ]] || continue
    case ":$seen:" in
      *":$candidate:"*) continue ;;
    esac
    seen="$seen:$candidate"
    printf '%s\n' "$candidate"
  done
}

uninstall_playwright_packages() {
  if [[ "$REMOVE_PLAYWRIGHT_PACKAGES" != "1" ]]; then
    return
  fi

  echo "Uninstalling Playwright packages from likely Python interpreters..."
  local py
  while IFS= read -r py; do
    echo "  $py"
    "$py" -m pip uninstall -y playwright >/dev/null 2>&1 || true
  done < <(candidate_pythons)
}

clear_playwright_caches() {
  if [[ "$CLEAR_PLAYWRIGHT_CACHE" != "1" ]]; then
    return
  fi

  echo "Clearing Playwright browser caches..."
  rm -rf "$HOME/Library/Caches/ms-playwright"
  rm -rf "$HOME/.cache/ms-playwright"
  rm -rf "$HOME/Library/Caches/pip"
}

echo "Cleanup target Python minor: $PYTHON_MINOR"
confirm
stop_pal_launchagents
uninstall_playwright_packages
remove_python_org_symlinks
remove_python_org_framework
clear_playwright_caches

echo
echo "Cleanup complete."
echo "Next install step can use Homebrew Python, for example:"
echo "  brew install python@$PYTHON_MINOR sqlite"
