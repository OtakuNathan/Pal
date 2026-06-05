#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_bin="${PYTHON:-python3}"
dist_dir="$repo_root/dist"

mkdir -p "$dist_dir"
rm -rf "$repo_root/build" "$repo_root/src/pal_v2.egg-info"
rm -f "$dist_dir"/pal_v2-*.whl

"$python_bin" -m pip wheel . --no-deps --no-build-isolation -w "$dist_dir"

wheel_path="$(ls -t "$dist_dir"/pal_v2-*.whl | head -n 1)"
if [[ -z "${wheel_path:-}" || ! -f "$wheel_path" ]]; then
  echo "No wheel was built" >&2
  exit 1
fi

required_wheel_paths=(
  "pal/core/tool_surface.toml"
  "pal/lsp/config.py"
  "pal/lsp/connector.py"
  "pal/lsp/ipc.py"
  "pal/lsp/manager.py"
  "pal/lsp/manager_main.py"
  "pal/lsp/plugin.py"
  "pal/lsp/server_templates/clangd.toml"
  "pal/lsp/server_templates/csharp.toml"
  "pal/lsp/server_templates/css.toml"
  "pal/lsp/server_templates/go.toml"
  "pal/lsp/server_templates/html.toml"
  "pal/lsp/server_templates/java.toml"
  "pal/lsp/server_templates/json.toml"
  "pal/lsp/server_templates/lua.toml"
  "pal/lsp/server_templates/pyright.toml"
  "pal/lsp/server_templates/rust.toml"
  "pal/lsp/server_templates/shell.toml"
  "pal/lsp/server_templates/typescript.toml"
  "pal/lsp/server_templates/yaml.toml"
  "pal/mcp/templates/stdio_server.toml"
  "pal/minion/profile_templates/generic.toml"
  "pal/minion/profile_templates/software_engineering/coder.toml"
  "pal/minion/profile_templates/software_engineering/planner.toml"
  "pal/minion/profile_templates/software_engineering/reviewer.toml"
  "pal/plugins_builtin/lsp/plugin.toml"
  "pal/plugins_builtin/lsp/runtime.py"
  "pal/plugins_builtin/mcp/plugin.toml"
  "pal/plugins_builtin/minion/plugin.toml"
  "pal/plugins_builtin/sqlite_vec_l3/plugin.toml"
  "pal/plugins_builtin/web_fetch/plugin.toml"
  "pal/plugins_builtin/web_search/plugin.toml"
)

missing=()
for path in "${required_wheel_paths[@]}"; do
  if ! unzip -l "$wheel_path" "$path" >/dev/null; then
    missing+=("$path")
  fi
done

if (( ${#missing[@]} )); then
  echo "Wheel is missing package data:" >&2
  printf '  %s\n' "${missing[@]}" >&2
  exit 1
fi

if unzip -l "$wheel_path" "pal/lsp/templates/*" >/dev/null 2>&1; then
  echo "Wheel contains legacy LSP template path: pal/lsp/templates/*" >&2
  exit 1
fi

echo "Built $wheel_path"
echo "Verified ${#required_wheel_paths[@]} required wheel files"
