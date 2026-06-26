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
  "pal/minion/gates.py"
  "pal/minion/llm_broker.py"
  "pal/minion/manager.py"
  "pal/minion/plan_builder.py"
  "pal/minion/plan_store.py"
  "pal/minion/profile_templates/generic.toml"
  "pal/minion/profile_templates/software_engineering/coder.toml"
  "pal/minion/profile_templates/software_engineering/planner.toml"
  "pal/minion/profile_templates/software_engineering/reviewer.toml"
  "pal/minion/profile_templates/software_engineering/writer.toml"
  "pal/minion/runner_process.py"
  "pal/minion/sandbox.py"
  "pal/minion/scoped_execution.py"
  "pal/minion/serial_scheduler.py"
  "pal/minion/review_gate_store.py"
  "pal/minion/review_orchestrator.py"
  "pal/minion/workspace_environment.py"
  "pal/minion/workspace_file_tools.py"
  "pal/minion/workspace_tools.py"
  "pal/plugins_builtin/lsp/plugin.toml"
  "pal/plugins_builtin/lsp/runtime.py"
  "pal/plugins_builtin/mcp/plugin.toml"
  "pal/plugins_builtin/minion/plugin.toml"
  "pal/plugins_builtin/sqlite_vec_l3/plugin.toml"
  "pal/skill/builtin_skills.py"
  "pal/skill/capabilities.py"
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

    expected_profile_gates = {
        "pal/minion/profile_templates/generic.toml": ["none"],
        "pal/minion/profile_templates/software_engineering/coder.toml": ["checkpoint_admission", "module_quality"],
        "pal/minion/profile_templates/software_engineering/planner.toml": ["plan_acceptance"],
    }
    for path, expected_gates in expected_profile_gates.items():
        payload = tomllib.loads(read_text(path))
        gate_policy = payload.get("gate_policy")
        if not isinstance(gate_policy, dict):
            fail(f"{path} missing [gate_policy]")
        gates = gate_policy.get("gates")
        if gates != expected_gates:
            fail(f"{path} gate_policy.gates={gates!r}, expected {expected_gates!r}")

    reviewer_profile = tomllib.loads(read_text("pal/minion/profile_templates/software_engineering/reviewer.toml"))
    reviewer_gate_policy = reviewer_profile.get("gate_policy")
    if not isinstance(reviewer_gate_policy, dict) or reviewer_gate_policy.get("submits_review_gate") is not True:
        fail("reviewer.toml must declare gate_policy.submits_review_gate = true")

    writer_profile = tomllib.loads(read_text("pal/minion/profile_templates/software_engineering/writer.toml"))
    if writer_profile.get("profile_id") != "writer" or writer_profile.get("profile_group") != "software_engineering":
        fail("writer.toml must declare software_engineering writer profile")
    writer_capabilities = writer_profile.get("default_allowed_capabilities")
    if not isinstance(writer_capabilities, list) or "op_file_write" not in writer_capabilities:
        fail("writer.toml must allow file writing for document artifacts")

    gates_source = read_text("pal/minion/gates.py")
    for token in (
        "GateDefinition",
        "GateChecklistEntry",
        "GateSpec",
        "checkpoint_quality",
        "checkpoint_admission",
        "module_quality",
        "plan_acceptance",
        "none",
        "normalize_gate_policy",
        "MinionGateChecklistEntryProvider",
        "MinionGateDefinitionProvider",
        "MinionGateStrategyProvider",
    ):
        if token not in gates_source:
            fail(f"pal/minion/gates.py missing {token!r}")

    sandbox_source = read_text("pal/minion/sandbox.py")
    for token in (
        "MinionSandboxSpec",
        "MINION_SANDBOX_BLACKLIST_COMMANDS",
        "sandbox_supported_backend",
        "with_minion_sandbox_metadata",
        "build_sandboxed_runner_invocation",
        "scrub_minion_sandbox_env",
        "ensure_sandbox_files",
        "PAL_MINION_LLM_BROKER",
        "PAL_MINION_SANDBOXED",
        "secret_policy",
        "host_llm_broker",
        "--share-net",
        "bubblewrap is required for Linux minion sandboxing",
    ):
        if token not in sandbox_source:
            fail(f"pal/minion/sandbox.py missing sandbox token {token!r}")

    scoped_execution_source = read_text("pal/minion/scoped_execution.py")
    for token in (
        "MINION_DISCOVERY_TOOL_SURFACE",
        "MINION_CODE_INTEL_TOOL_SURFACE",
        "MINION_DIRECT_WORK_TOOL_SURFACE",
        "WORKSPACE_TOOL_SPECS",
        "PLAN_BUILDER_CAPABILITIES",
        "WORKSPACE_FILE_TOOL_SPECS",
        "op_path_delete",
        "op_git",
        "op_minion_review_checkpoint",
        "op_minion_checkpoint_commit",
    ):
        if token not in scoped_execution_source:
            fail(f"pal/minion/scoped_execution.py missing scoped execution token {token!r}")

    plan_builder_source = read_text("pal/minion/plan_builder.py")
    for token in (
        "PLAN_BUILDER_READ_CAPABILITIES",
        "PLAN_BUILDER_WRITE_CAPABILITIES",
        "op_minion_plan_checkout",
        "op_minion_plan_update_acceptance_criterion",
        "op_minion_plan_delete_acceptance_criterion",
        "module_quality_criteria",
        "checkpoint_admission_evidence",
        "negative_cases",
        "depends_on_module_keys",
        "fork_join_linear",
    ):
        if token not in plan_builder_source:
            fail(f"pal/minion/plan_builder.py missing plan builder token {token!r}")

    scheduler_source = read_text("pal/minion/serial_scheduler.py")
    for token in (
        "SerialMilestoneScheduler",
        "auto_advance",
        "next_serial_module_turn",
        "mark_serial_module_completed",
        "record_plan_module_completion",
        "auto_continue_work_order",
    ):
        if token not in scheduler_source:
            fail(f"pal/minion/serial_scheduler.py missing serial scheduler token {token!r}")

    skill_source = read_text("pal/skill/builtin_skills.py")
    for token in (
        'PAL_MINION_GATE_DEVELOPMENT_SKILL_ID = "pal.minion.gate.development"',
        "PAL_MINION_GATE_DEVELOPMENT_MANUAL",
        "Pal Minion Gate Development",
        "GateDefinition",
        "GateChecklistEntry",
        "GateSpec",
        "Builtin Implementation Workflow",
        "Test Targets",
        "src/pal/minion/gates.py",
        "scripts/build_package.sh",
        "GateStrategy",
        "repair/todo ledger projection",
        "normalize_gate_policy",
        "MinionGateChecklistEntryProvider",
        "MinionGateDefinitionProvider",
        "MinionGateStrategyProvider",
        "op_minion_review_gate_submit",
        "op_minion_review_checkpoint",
        '"may_require_code_changes": True',
        '"extension_boundary": "minion.gates"',
    ):
        if token not in skill_source:
            fail(f"pal/skill/builtin_skills.py missing minion gate skill token {token!r}")

    skill_capability_source = read_text("pal/skill/capabilities.py")
    for token in (
        "PAL_MINION_GATE_DEVELOPMENT_SKILL_ID",
        'affordance_id="declared.skill.pal_minion_gate_development"',
        "Pal minion gate development skill",
        "inject skill `pal.minion.gate.development`",
        "GateDefinition",
        "GateChecklistEntry",
        "checkpoint_quality",
        "plan_acceptance",
        "gate ledger",
    ):
        if token not in skill_capability_source:
            fail(f"pal/skill/capabilities.py missing gate skill affordance token {token!r}")

print("Verified minion profile gates, sandbox, plan builder, scheduler, and gate development skill semantics")
PY

echo "Built $wheel_path"
echo "Verified ${#required_wheel_paths[@]} required wheel files"
