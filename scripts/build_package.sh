#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_bin="${PYTHON:-python3}"
dist_dir="$repo_root/dist"

mkdir -p "$dist_dir"
rm -f "$dist_dir"/pal_v2-*.whl

"$python_bin" -m pip wheel . --no-deps --no-build-isolation -w "$dist_dir"

wheel_path="$(ls -t "$dist_dir"/pal_v2-*.whl | head -n 1)"
if [[ -z "${wheel_path:-}" || ! -f "$wheel_path" ]]; then
  echo "No wheel was built" >&2
  exit 1
fi

required_toml=(
  "pal/core/tool_surface.toml"
  "pal/mcp/templates/stdio_server.toml"
  "pal/minion/profile_templates/generic.toml"
  "pal/minion/profile_templates/software_engineering/coder.toml"
  "pal/minion/profile_templates/software_engineering/planner.toml"
  "pal/minion/profile_templates/software_engineering/reviewer.toml"
  "pal/plugins_builtin/mcp/plugin.toml"
  "pal/plugins_builtin/minion/plugin.toml"
  "pal/plugins_builtin/sqlite_vec_l3/plugin.toml"
  "pal/plugins_builtin/web_fetch/plugin.toml"
  "pal/plugins_builtin/web_search/plugin.toml"
)

missing=()
for path in "${required_toml[@]}"; do
  if ! unzip -l "$wheel_path" "$path" >/dev/null; then
    missing+=("$path")
  fi
done

if (( ${#missing[@]} )); then
  echo "Wheel is missing package data:" >&2
  printf '  %s\n' "${missing[@]}" >&2
  exit 1
fi

echo "Built $wheel_path"
echo "Verified ${#required_toml[@]} TOML package-data files"
