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
  "pal/lsp/skills.py"
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
  "pal/memory/interactions.py"
  "pal/minion/gates.py"
  "pal/minion/interactions.py"
  "pal/minion/llm_broker.py"
  "pal/minion/manager.py"
  "pal/minion/plan_builder.py"
  "pal/minion/plan_store.py"
  "pal/minion/profile_templates/generic.toml"
  "pal/minion/profile_templates/software_engineering/architect.toml"
  "pal/minion/profile_templates/software_engineering/coder.toml"
  "pal/minion/profile_templates/software_engineering/reviewer.toml"
  "pal/minion/profile_templates/software_engineering/writer.toml"
  "pal/minion/runner_process.py"
  "pal/minion/sandbox.py"
  "pal/minion/scoped_execution.py"
  "pal/minion/serial_scheduler.py"
  "pal/minion/skills.py"
  "pal/minion/review_gate_store.py"
  "pal/minion/review_orchestrator.py"
  "pal/minion/workspace_environment.py"
  "pal/minion/workspace_environment_templates/clangd.toml"
  "pal/minion/workspace_environment_templates/cpp-cmake-runtime.toml"
  "pal/minion/workspace_environment_templates/python-lsp.toml"
  "pal/minion/workspace_environment_templates/python-runtime.toml"
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
        "pal/minion/profile_templates/software_engineering/architect.toml": ["plan_acceptance"],
    }
    for path, expected_gates in expected_profile_gates.items():
        payload = tomllib.loads(read_text(path))
        gate_policy = payload.get("gate_policy")
        if not isinstance(gate_policy, dict):
            fail(f"{path} missing [gate_policy]")
        gates = gate_policy.get("gates")
        if gates != expected_gates:
            fail(f"{path} gate_policy.gates={gates!r}, expected {expected_gates!r}")

    if "pal/minion/profile_templates/software_engineering/planner.toml" in names:
        fail("planner.toml must not be packaged; Pal owns requirements shaping and architect is the first software step")

    architect_profile = tomllib.loads(read_text("pal/minion/profile_templates/software_engineering/architect.toml"))
    if architect_profile.get("profile_id") != "architect" or architect_profile.get("profile_group") != "software_engineering":
        fail("architect.toml must declare software_engineering architect profile")
    architect_capabilities = architect_profile.get("capability_groups")
    if not isinstance(architect_capabilities, list) or "minion_plan_builder" not in architect_capabilities:
        fail("architect.toml must expose plan builder capabilities")
    architect_output_policy = architect_profile.get("output_policy")
    if not isinstance(architect_output_policy, dict):
        fail("architect.toml missing [output_policy]")
    if architect_output_policy.get("requires_plan_artifact") is not True:
        fail("architect.toml must require a plan artifact")
    if architect_output_policy.get("primary_artifact") != "plan.json":
        fail("architect.toml must emit plan.json as the primary artifact")

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

    expected_workspace_preparers = {
        "pal/minion/workspace_environment_templates/python-runtime.toml": ("python-runtime", "runtime", {"python"}),
        "pal/minion/workspace_environment_templates/python-lsp.toml": ("python-lsp", "lsp", {"python"}),
        "pal/minion/workspace_environment_templates/cpp-cmake-runtime.toml": (
            "cpp-cmake-runtime",
            "runtime",
            {"c", "cpp", "objc", "objcpp"},
        ),
        "pal/minion/workspace_environment_templates/clangd.toml": (
            "clangd",
            "lsp",
            {"c", "cpp", "objc", "objcpp"},
        ),
    }
    for path, (preparer_id, kind, language_ids) in expected_workspace_preparers.items():
        payload = tomllib.loads(read_text(path))
        if payload.get("preparer_id") != preparer_id:
            fail(f"{path} preparer_id={payload.get('preparer_id')!r}, expected {preparer_id!r}")
        if payload.get("kind") != kind:
            fail(f"{path} kind={payload.get('kind')!r}, expected {kind!r}")
        found_language_ids = set(payload.get("language_ids") or [])
        if found_language_ids != language_ids:
            fail(f"{path} language_ids={sorted(found_language_ids)!r}, expected {sorted(language_ids)!r}")

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
    skill_capability_source = read_text("pal/skill/capabilities.py")
    for path, source in (
        ("pal/skill/builtin_skills.py", skill_source),
        ("pal/skill/capabilities.py", skill_capability_source),
    ):
        for token in (
            "PAL_MINION_DEVELOPMENT_SKILL_ID",
            "PAL_MINION_PROFILE_DEVELOPMENT_SKILL_ID",
            "pal.minion.development",
            "pal.minion.profile.development",
            "declared.skill.pal_minion_development",
            "declared.skill.pal_minion_profile_development",
            "plugins/minion/workspace_environment",
            "src/pal/minion/workspace_environment.py",
            "WorkspaceEnvironmentPreparer",
            "PAL_LSP_TEMPLATE_DEVELOPMENT_SKILL_ID",
            "pal.lsp.template.development",
            "declared.skill.pal_lsp_template_development",
            "plugins/lsp/servers",
        ):
            if token in source:
                fail(f"{path} should not declare plugin-owned skill token {token!r}")

    lsp_skill_source = read_text("pal/lsp/skills.py")
    for token in (
        'PAL_LSP_TEMPLATE_DEVELOPMENT_SKILL_ID = "pal.lsp.template.development"',
        "PAL_LSP_TEMPLATE_DEVELOPMENT_MANUAL",
        "Pal LSP Template Development",
        "plugins/lsp/servers",
        "op_lsp_mgmt_rescan",
        "op_lsp_status",
        "op_lsp_doctor",
        "load_lsp_server_file",
        '"runtime_root_layout": "plugins/lsp/servers/<server_id>.toml"',
    ):
        if token not in lsp_skill_source:
            fail(f"pal/lsp/skills.py missing LSP template development skill token {token!r}")

    lsp_capability_source = read_text("pal/lsp/plugin.py")
    for token in (
        "PAL_LSP_TEMPLATE_DEVELOPMENT_SKILL_ID",
        'affordance_id="declared.skill.pal_lsp_template_development"',
        "Pal LSP template development skill",
        "inject skill `pal.lsp.template.development`",
        "lsp_declared_skills",
        "declared_skills",
    ):
        if token not in lsp_capability_source:
            fail(f"pal/lsp/plugin.py missing LSP template skill registration token {token!r}")

    minion_skill_source = read_text("pal/minion/skills.py")
    for token in (
        'PAL_MINION_DEVELOPMENT_SKILL_ID = "pal.minion.development"',
        "PAL_MINION_DEVELOPMENT_MANUAL",
        "Pal Minion Development",
        "op_minion_dispatch_workflow",
        "workflow_next",
        "op_minion_submit_repair_bill",
        "resource slots",
        "Coroutine runner mode",
        "workspace environment",
        "plugins/minion/workspace_environment",
        "src/pal/minion/workspace_environment.py",
        "WorkspaceEnvironmentPreparer",
        "repair bill replay",
        "reverse-propagation mechanism",
        "amended obligations for the existing DAG",
        "module-key indexed",
        "shape-compatible with the existing plan/module schema",
        "Use existing `module_id`/`module_key` values",
        "Keep patch shape isomorphic to plan shape",
        "normal DAG, slot, workspace, and gate logic",
        "GateDefinition",
        "GateChecklistEntry",
        "GateSpec",
        "Implementation Workflow",
        "Test Targets",
        "src/pal/minion/gates.py",
        "src/pal/minion/manager.py",
        "src/pal/minion/step_runner.py",
        "scripts/build_package.sh",
        "GateStrategy",
        "repair/todo ledger projection",
        "normalize_gate_policy",
        "MinionGateChecklistEntryProvider",
        "MinionGateDefinitionProvider",
        "MinionGateStrategyProvider",
        "op_minion_review_gate_submit",
        '"may_require_code_changes": True',
        '"extension_boundary": "minion"',
    ):
        if token not in minion_skill_source:
            fail(f"pal/minion/skills.py missing minion development skill token {token!r}")
    for token in (
        'PAL_MINION_PROFILE_DEVELOPMENT_SKILL_ID = "pal.minion.profile.development"',
        "PAL_MINION_PROFILE_DEVELOPMENT_MANUAL",
        "Pal Minion Profile Development",
        "Profile TOML Shape",
        "runtime_root/plugins/minion/profiles",
        "capability_groups",
        "workspace_policy",
        "capability_policy",
        "gate_policy",
        "output_policy",
        "workflow_next",
        "gates = [\"none\"]",
        "intro_minion_profile_list",
        "intro_minion_profile_read",
        "op_minion_dispatch_workflow",
        '"extension_boundary": "minion.profiles"',
    ):
        if token not in minion_skill_source:
            fail(f"pal/minion/skills.py missing minion profile development skill token {token!r}")

    minion_capability_source = read_text("pal/minion/capabilities.py")
    for token in (
        "PAL_MINION_DEVELOPMENT_SKILL_ID",
        'affordance_id="declared.skill.pal_minion_development"',
        "Pal minion development skill",
        "inject skill `pal.minion.development`",
        "workflow_next",
        "resource slots",
        "repair bill replay",
        "GateDefinition",
        "GateChecklistEntry",
        "checkpoint_quality",
        "plan_acceptance",
        "gate ledger",
    ):
        if token not in minion_capability_source:
            fail(f"pal/minion/capabilities.py missing minion development skill affordance token {token!r}")
    for token in (
        "PAL_MINION_PROFILE_DEVELOPMENT_SKILL_ID",
        'affordance_id="declared.skill.pal_minion_profile_development"',
        "Pal minion profile development skill",
        "inject skill `pal.minion.profile.development`",
        "capability_groups",
        "workspace_policy",
        "capability_policy",
        "gate_policy",
        "output_policy",
        "workflow_next",
        "plugins/minion/profiles",
        '"extension_boundary": "minion.profiles"',
    ):
        if token not in minion_capability_source:
            fail(f"pal/minion/capabilities.py missing minion profile development skill affordance token {token!r}")

print("Verified minion profiles, workspace environment templates, sandbox, plan builder, scheduler, and minion development skill semantics")
PY

echo "Built $wheel_path"
echo "Verified ${#required_wheel_paths[@]} required wheel files"
