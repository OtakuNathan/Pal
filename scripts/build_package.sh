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
  "pal/lsp/server_templates/clangd.toml"
  "pal/lsp/server_templates/pyright.toml"
  "pal/mcp/templates/stdio_server.toml"
  "pal/minion/families.py"
  "pal/minion/profiles.py"
  "pal/minion/manager.py"
  "pal/minion/runner.py"
  "pal/minion/sandbox.py"
  "pal/minion/scoped_execution.py"
  "pal/minion/family_templates/general.toml"
  "pal/minion/family_templates/lifestyle.toml"
  "pal/minion/family_templates/software_engineering.toml"
  "pal/minion/profile_templates/generic.toml"
  "pal/minion/profile_templates/general/requirements_analyst.toml"
  "pal/minion/profile_templates/general/researcher.toml"
  "pal/minion/profile_templates/general/contract_planner.toml"
  "pal/minion/profile_templates/general/architecture_reviewer.toml"
  "pal/minion/profile_templates/general/verifier.toml"
  "pal/minion/profile_templates/lifestyle/requirements_analyst.toml"
  "pal/minion/profile_templates/lifestyle/researcher.toml"
  "pal/minion/profile_templates/lifestyle/contract_planner.toml"
  "pal/minion/profile_templates/lifestyle/architecture_reviewer.toml"
  "pal/minion/profile_templates/lifestyle/nutrition_checkin_producer.toml"
  "pal/minion/profile_templates/lifestyle/verifier.toml"
  "pal/minion/profile_templates/software_engineering/v2_requirements_analyst.toml"
  "pal/minion/profile_templates/software_engineering/v2_researcher.toml"
  "pal/minion/profile_templates/software_engineering/v2_contract_planner.toml"
  "pal/minion/profile_templates/software_engineering/v2_architecture_reviewer.toml"
  "pal/minion/profile_templates/software_engineering/v2_coder.toml"
  "pal/minion/profile_templates/software_engineering/v2_verifier.toml"
  "pal/minion/profile_templates/software_engineering/v2_reviewer.toml"
  "pal/minion/v2/adapters.py"
  "pal/minion/v2/artifacts.py"
  "pal/minion/v2/catalog.py"
  "pal/minion/v2/contract_builder.py"
  "pal/minion/v2/contracts.py"
  "pal/minion/v2/execution.py"
  "pal/minion/v2/machines.py"
  "pal/minion/v2/orchestration.py"
  "pal/minion/v2/repository.py"
  "pal/minion/v2/service.py"
  "pal/minion/v2/verification.py"
  "pal/minion/v2/worker_main.py"
  "pal/minion/v2/workers.py"
  "pal/plugins_builtin/minion/plugin.toml"
  "pal/plugins_builtin/minion/runtime.py"
)

missing=()
for path in "${required_wheel_paths[@]}"; do
  if ! unzip -l "$wheel_path" "$path" >/dev/null; then
    missing+=("$path")
  fi
done
if (( ${#missing[@]} )); then
  echo "Wheel is missing required files:" >&2
  printf '  %s\n' "${missing[@]}" >&2
  exit 1
fi

"$python_bin" - "$wheel_path" <<'PY'
from __future__ import annotations

import sys
import tomllib
import zipfile


wheel_path = sys.argv[1]


def fail(message: str) -> None:
    raise SystemExit(f"Wheel semantic check failed: {message}")


with zipfile.ZipFile(wheel_path) as wheel:
    names = set(wheel.namelist())

    def read_text(path: str) -> str:
        if path not in names:
            fail(f"missing {path}")
        return wheel.read(path).decode("utf-8")

    legacy_paths = {
        "pal/minion/plan_builder.py",
        "pal/minion/plan_store.py",
        "pal/minion/serial_scheduler.py",
        "pal/minion/step_executor_main.py",
        "pal/minion/step_executor_runner.py",
        "pal/minion/review_gate_store.py",
        "pal/minion/review_orchestrator.py",
        "pal/minion/workspace_environment.py",
        "pal/minion/profile_templates/software_engineering/architect.toml",
        "pal/minion/profile_templates/software_engineering/coder.toml",
        "pal/minion/profile_templates/software_engineering/reviewer.toml",
    }
    leaked = sorted(legacy_paths & names)
    if leaked:
        fail(f"legacy Minion workflow files were packaged: {leaked}")

    required_roles = {"requirements", "research", "planner", "architecture_reviewer", "producer", "repair", "verifier"}
    for family_id in ("general", "lifestyle", "software_engineering"):
        path = f"pal/minion/family_templates/{family_id}.toml"
        payload = tomllib.loads(read_text(path))
        if payload.get("family_id") != family_id:
            fail(f"{path} has the wrong family_id")
        if payload.get("workflow_template") != "contract_dag.v2":
            fail(f"{path} must use contract_dag.v2")
        if not required_roles.issubset(set(dict(payload.get("roles") or {}))):
            fail(f"{path} does not bind the complete role set")
        if set(dict(payload.get("builders") or {}).values()) - {
            "requirements.v2", "evidence_catalog.v2", "contract_sketch.v2", "verification.v2"
        }:
            fail(f"{path} references an unknown builder")
        if any(not str(value).endswith(".v2") for value in dict(payload.get("adapters") or {}).values()):
            fail(f"{path} references a non-V2 adapter")

    profile_paths = sorted(name for name in names if name.startswith("pal/minion/profile_templates/") and name.endswith(".toml"))
    if len(profile_paths) != 19:
        fail(f"expected 19 builtin role profiles, found {len(profile_paths)}")
    for path in profile_paths:
        payload = tomllib.loads(read_text(path))
        if not str(payload.get("profile_id") or "").strip() or not str(payload.get("profile_group") or "").strip():
            fail(f"{path} is missing profile identity")
        if not list(payload.get("capability_groups") or []):
            fail(f"{path} must declare capability_groups")
        output_policy = dict(payload.get("output_policy") or {})
        if not str(output_policy.get("primary_artifact") or "").strip():
            fail(f"{path} is missing output_policy.primary_artifact")
        metadata = dict(payload.get("metadata") or {})
        if metadata.get("builtin") is not True:
            fail(f"{path} must be a managed builtin profile")

    scoped_execution = read_text("pal/minion/scoped_execution.py")
    for token in ("CONTRACT_BUILDER_TOOL_SPECS", "WORKSPACE_FILE_TOOL_SPECS", "op_path_delete", "op_git"):
        if token not in scoped_execution:
            fail(f"scoped execution is missing {token}")
    for forbidden in ("PLAN_BUILDER_CAPABILITIES", "op_minion_checkpoint_commit", "op_minion_review_checkpoint"):
        if forbidden in scoped_execution:
            fail(f"scoped execution contains legacy capability {forbidden}")

    manager = read_text("pal/minion/manager.py")
    for forbidden in ('"spawn"', '"finalize"', '"tick"', '"recover"'):
        if forbidden in manager:
            fail(f"V2 manager exposes legacy RPC {forbidden}")

    shared_messages = read_text("pal/shared/messages.py")
    for forbidden in ("TaskContextPack", "CheckpointEvent", "milestone_index"):
        if forbidden in shared_messages:
            fail(f"shared worker transport contains legacy symbol {forbidden}")

print("Verified V2 families, role profiles, controlled builders, adapters, worker transport, and legacy cutover")
PY

echo "Built $wheel_path"
echo "Verified ${#required_wheel_paths[@]} required wheel files"
