#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_bin="${PYTHON:-python3}"
dist_dir="$repo_root/dist"
provider_dist_dir="$dist_dir/providers"
installer_source="$repo_root/scripts/install_package.sh"
installer_path="$dist_dir/install-pal.sh"
install_bundle_dir="$dist_dir/install_bundle"
install_bundle_path="$dist_dir/pal_v2-install-bundle.tar.gz"

mkdir -p "$dist_dir"
rm -rf "$repo_root/build" "$repo_root/src/pal_v2.egg-info"
rm -f "$dist_dir"/pal_v2-*.whl
rm -rf "$provider_dist_dir"
rm -rf "$dist_dir/runtime_root"
rm -f "$dist_dir/pal_v2-runtime-root-overlay.tar.gz"
rm -f "$installer_path"
rm -rf "$install_bundle_dir"
rm -f "$install_bundle_path"

"$python_bin" -m pip wheel . --no-deps --no-build-isolation -w "$dist_dir"
PYTHON="$python_bin" "$repo_root/scripts/build_provider_packages.sh" "$provider_dist_dir"

wheel_path="$(ls -t "$dist_dir"/pal_v2-*.whl | head -n 1)"
if [[ -z "${wheel_path:-}" || ! -f "$wheel_path" ]]; then
  echo "No wheel was built" >&2
  exit 1
fi

telegram_provider_wheel_path="$(ls -t "$provider_dist_dir"/pal_channel_provider_telegram-*.whl | head -n 1)"
if [[ -z "${telegram_provider_wheel_path:-}" || ! -f "$telegram_provider_wheel_path" ]]; then
  echo "No Telegram provider wheel was built" >&2
  exit 1
fi
websocket_provider_wheel_path="$(ls -t "$provider_dist_dir"/pal_channel_provider_websocket_bridge-*.whl | head -n 1)"
if [[ -z "${websocket_provider_wheel_path:-}" || ! -f "$websocket_provider_wheel_path" ]]; then
  echo "No WebSocket bridge provider wheel was built" >&2
  exit 1
fi

required_wheel_paths=(
  "pal/packages/service.py"
  "pal/packages/hook_runner.py"
  "pal/web_fetch/provisioning.py"
  "pal/plugins_builtin/web_fetch/installation.py"
  "pal/lsp/server_templates/clangd.toml"
  "pal/lsp/server_templates/pyright.toml"
  "pal/mcp/templates/stdio_server.toml"
  "pal/bunshin/families.py"
  "pal/bunshin/harnesses.py"
  "pal/bunshin/profiles.py"
  "pal/bunshin/manager.py"
  "pal/bunshin/runner.py"
  "pal/bunshin/sandbox.py"
  "pal/bunshin/scoped_execution.py"
  "pal/bunshin/family_templates/general.toml"
  "pal/bunshin/family_templates/lifestyle.toml"
  "pal/bunshin/family_templates/software_engineering.toml"
  "pal/bunshin/profile_templates/generic.toml"
  "pal/bunshin/profile_templates/general/architect.toml"
  "pal/bunshin/profile_templates/general/reviewer.toml"
  "pal/bunshin/profile_templates/general/verifier.toml"
  "pal/bunshin/profile_templates/lifestyle/architect.toml"
  "pal/bunshin/profile_templates/lifestyle/nutritionist.toml"
  "pal/bunshin/profile_templates/lifestyle/reviewer.toml"
  "pal/bunshin/architecture_templates/base/schema.json"
  "pal/bunshin/architecture_templates/base/architect.yaml.j2"
  "pal/bunshin/architecture_specializations/general.v1/specialization.json"
  "pal/bunshin/architecture_specializations/general.v1/preamble.j2"
  "pal/bunshin/architecture_specializations/general.v1/context.j2"
  "pal/bunshin/architecture_specializations/general.v1/module_definition.j2"
  "pal/bunshin/architecture_specializations/general.v1/graph_satellite.j2"
  "pal/bunshin/architecture_specializations/lifestyle.nutrition_checkin.v1/specialization.json"
  "pal/bunshin/architecture_specializations/lifestyle.nutrition_checkin.v1/preamble.j2"
  "pal/bunshin/architecture_specializations/lifestyle.nutrition_checkin.v1/context.j2"
  "pal/bunshin/architecture_specializations/lifestyle.nutrition_checkin.v1/module_definition.j2"
  "pal/bunshin/architecture_specializations/lifestyle.nutrition_checkin.v1/graph_satellite.j2"
  "pal/bunshin/architecture_specializations/software_engineering.v1/specialization.json"
  "pal/bunshin/architecture_specializations/software_engineering.v1/preamble.j2"
  "pal/bunshin/architecture_specializations/software_engineering.v1/context.j2"
  "pal/bunshin/architecture_specializations/software_engineering.v1/module_definition.j2"
  "pal/bunshin/architecture_specializations/software_engineering.v1/graph_satellite.j2"
  "pal/bunshin/profile_templates/software_engineering/v2_architect.toml"
  "pal/bunshin/profile_templates/software_engineering/v2_coder.toml"
  "pal/bunshin/profile_templates/software_engineering/v2_reviewer.toml"
  "pal/bunshin/profile_templates/software_engineering/v2_verifier.toml"
  "pal/bunshin/adapters.py"
  "pal/bunshin/artifacts.py"
  "pal/bunshin/architecture_compilation.py"
  "pal/bunshin/candidate_builder.py"
  "pal/bunshin/workflow_capabilities.py"
  "pal/bunshin/workflow_catalog.py"
  "pal/bunshin/ask_question.py"
  "pal/bunshin/contract_protocol.py"
  "pal/bunshin/contract_runtime.py"
  "pal/bunshin/contract_submission.py"
  "pal/bunshin/contracts.py"
  "pal/bunshin/coroutine_runtime.py"
  "pal/bunshin/cycle_protocol.py"
  "pal/bunshin/delivery.py"
  "pal/bunshin/execution.py"
  "pal/bunshin/graph_compiler.py"
  "pal/bunshin/graph_executor.py"
  "pal/bunshin/graph_protocol.py"
  "pal/bunshin/graph_satellites.py"
  "pal/bunshin/machines.py"
  "pal/bunshin/module_protocol.py"
  "pal/bunshin/orchestration.py"
  "pal/bunshin/paths.py"
  "pal/bunshin/process_lifecycle.py"
  "pal/bunshin/projections.py"
  "pal/bunshin/recovery.py"
  "pal/bunshin/replan.py"
  "pal/bunshin/repository.py"
  "pal/bunshin/review_findings.py"
  "pal/bunshin/review_submission.py"
  "pal/bunshin/role_gateway.py"
  "pal/bunshin/role_runtime.py"
  "pal/bunshin/service.py"
  "pal/bunshin/sessions.py"
  "pal/bunshin/skeleton.py"
  "pal/bunshin/submission_drafts.py"
  "pal/bunshin/submission_preflight.py"
  "pal/bunshin/swe_verification.py"
  "pal/bunshin/task_ledger.py"
  "pal/bunshin/verification.py"
  "pal/bunshin/verification_builder.py"
  "pal/bunshin/work_items.py"
  "pal/bunshin/worker_main.py"
  "pal/bunshin/workflow_runtime.py"
  "pal/bunshin/semantic_orchestration/architecture.py"
  "pal/bunshin/semantic_orchestration/contracts.py"
  "pal/bunshin/semantic_orchestration/implementation.py"
  "pal/bunshin/semantic_orchestration/orchestrator.py"
  "pal/bunshin/semantic_orchestration/review.py"
  "pal/bunshin/semantic_orchestration/verification.py"
  "pal/plugins_builtin/bunshin/plugin.toml"
  "pal/plugins_builtin/bunshin/runtime.py"
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

"$python_bin" - "$wheel_path" "$repo_root/providers" "$repo_root" <<'PY'
from __future__ import annotations

import json
import sys
import tomllib
import zipfile
from pathlib import Path


wheel_path = sys.argv[1]
provider_root = Path(sys.argv[2])
repo_root = Path(sys.argv[3])


def fail(message: str) -> None:
    raise SystemExit(f"Wheel semantic check failed: {message}")


with zipfile.ZipFile(wheel_path) as wheel:
    names = set(wheel.namelist())

    def read_text(path: str) -> str:
        if path not in names:
            fail(f"missing {path}")
        return wheel.read(path).decode("utf-8")

    # ``required_wheel_paths`` below intentionally names the files that are
    # part of the semantic cutover contract.  It is not, however, a safe
    # inventory of the Python package: new modules can be added without being
    # remembered there.  Keep the packaging boundary mechanical as well.  All
    # Python sources and package data types declared by pyproject.toml must be
    # present in the wheel, while developer docs and generated egg-info remain
    # source-only.
    package_source_root = repo_root / "src"
    package_source_paths = sorted(
        path.relative_to(package_source_root).as_posix()
        for path in package_source_root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and not any(part.endswith(".egg-info") for part in path.parts)
        and path.suffix in {".py", ".toml", ".json", ".j2"}
    )
    missing_package_sources = [
        path for path in package_source_paths if path not in names
    ]
    if missing_package_sources:
        fail(
            "wheel is missing package source/data files: "
            f"{missing_package_sources}"
        )

    packaged_channel_provider_paths = sorted(
        name
        for name in names
        if name.startswith("pal/channel/providers/")
        or name.startswith("pal/channel/endpoints/telegram_endpoint")
        or name.startswith("pal/plugins_builtin/telegram_channel/")
    )
    if packaged_channel_provider_paths:
        fail(
            "Detachable channel providers must be runtime-root-only, but the wheel contains "
            f"{packaged_channel_provider_paths}"
        )

    def read_provider_text(provider_id: str, path: str) -> str:
        target = provider_root / provider_id / path
        if not target.is_file():
            fail(f"missing runtime provider source {target}")
        return target.read_text(encoding="utf-8")

    websocket_manifest = tomllib.loads(read_provider_text("websocket_bridge", "provider.toml"))
    if websocket_manifest.get("provider_id") != "websocket_bridge":
        fail("WebSocket bridge manifest has the wrong provider_id")
    if websocket_manifest.get("entrypoint") != "runtime.py":
        fail("WebSocket bridge manifest must load runtime.py")
    if websocket_manifest.get("enabled") is not True:
        fail("WebSocket bridge manifest must be enabled")

    channel_capabilities = read_text("pal/channel/capabilities.py")
    for token in (
        'aliases=("send_channel_message",)',
        "ChannelCapabilitiesChannelIntrospectionProviderSendMessageInput",
        "ChannelCapabilitiesChannelIntrospectionProviderSendMessageOutput",
    ):
        if token not in channel_capabilities:
            fail(f"channel active-send contract is missing {token}")

    websocket_sidecar = read_provider_text("websocket_bridge", "sidecar.py")
    for token in (
        'method == "send_message"',
        'return {"message_id": request_id}',
        "MAX_PEER_MESSAGE_COUNT",
        "PEER_END_SENTINEL",
        "bridge_socket_path",
        "importlib.import_module(f\"{module_name}.exceptions\")",
        "websocket bridge sidecar terminated unexpectedly",
    ):
        if token not in websocket_sidecar:
            fail(f"WebSocket sidecar contract is missing {token}")
    for forbidden in (
        "pending_requests",
        "message_timeout_seconds",
        "socket_channel_path",
        "forward_reply",
    ):
        if forbidden in websocket_sidecar:
            fail(f"WebSocket sidecar contains obsolete synchronous-send state {forbidden}")

    websocket_runtime = read_provider_text("websocket_bridge", "runtime.py")
    for token in (
        "configure_process_logging",
        'status="accepted"',
        'socket_path=data_root / "channel.sock"',
        "render_peer_input",
    ):
        if token not in websocket_runtime:
            fail(f"WebSocket provider runtime is missing {token}")
    for forbidden in (
        "stdout=subprocess.DEVNULL",
        "stderr=subprocess.DEVNULL",
        "message_timeout_seconds",
        "socket_channel_path",
    ):
        if forbidden in websocket_runtime:
            fail(f"WebSocket provider runtime contains obsolete behavior {forbidden}")

    telegram_manifest = tomllib.loads(read_provider_text("telegram", "provider.toml"))
    if telegram_manifest.get("provider_id") != "telegram":
        fail("Telegram manifest has the wrong provider_id")
    if telegram_manifest.get("entrypoint") != "runtime.py":
        fail("Telegram manifest must load runtime.py")
    telegram_endpoint = read_provider_text("telegram", "endpoint.py")
    for token in (
        "TelegramInteractionStore",
        "_segment_text(interaction_text(spec)",
        "data_root=",
    ):
        if token not in telegram_endpoint:
            fail(f"Telegram provider is missing {token}")

    metadata_paths = sorted(name for name in names if name.endswith(".dist-info/METADATA"))
    if len(metadata_paths) != 1:
        fail(f"expected one wheel METADATA file, found {len(metadata_paths)}")
    metadata = read_text(metadata_paths[0])
    if not any(
        line.startswith("Requires-Dist: websockets") for line in metadata.splitlines()
    ):
        fail("wheel metadata does not declare the websockets dependency")

    legacy_paths = {
        "pal/bunshin/plan_builder.py",
        "pal/bunshin/plan_store.py",
        "pal/bunshin/serial_scheduler.py",
        "pal/bunshin/step_executor_main.py",
        "pal/bunshin/step_executor_runner.py",
        "pal/bunshin/review_gate_store.py",
        "pal/bunshin/review_orchestrator.py",
        "pal/bunshin/workspace_environment.py",
        "pal/bunshin/profile_templates/software_engineering/architect.toml",
        "pal/bunshin/profile_templates/software_engineering/coder.toml",
        "pal/bunshin/profile_templates/software_engineering/reviewer.toml",
        "pal/bunshin/profile_templates/general/requirements_analyst.toml",
        "pal/bunshin/profile_templates/general/researcher.toml",
        "pal/bunshin/profile_templates/general/contract_planner.toml",
        "pal/bunshin/profile_templates/general/architecture_reviewer.toml",
        "pal/bunshin/profile_templates/lifestyle/requirements_analyst.toml",
        "pal/bunshin/profile_templates/lifestyle/researcher.toml",
        "pal/bunshin/profile_templates/lifestyle/contract_planner.toml",
        "pal/bunshin/profile_templates/lifestyle/architecture_reviewer.toml",
        "pal/bunshin/profile_templates/software_engineering/v2_requirements_analyst.toml",
        "pal/bunshin/profile_templates/software_engineering/v2_researcher.toml",
        "pal/bunshin/profile_templates/software_engineering/v2_contract_planner.toml",
        "pal/bunshin/profile_templates/software_engineering/v2_architecture_reviewer.toml",
        "pal/bunshin/workers.py",
    }
    leaked = sorted(legacy_paths & names)
    if leaked:
        fail(f"legacy Bunshin workflow files were packaged: {leaked}")

    required_roles = {"architect", "reviewer", "implementation", "verifier"}
    for family_id in ("general", "lifestyle", "software_engineering"):
        path = f"pal/bunshin/family_templates/{family_id}.toml"
        payload = tomllib.loads(read_text(path))
        if payload.get("family_id") != family_id:
            fail(f"{path} has the wrong family_id")
        if payload.get("workflow_template") != "contract_dag.v2":
            fail(f"{path} must use contract_dag.v2")
        if set(dict(payload.get("role_bindings") or {})) != required_roles:
            fail(f"{path} does not bind the complete role set")
        architecture = dict(payload.get("architecture") or {})
        specialization = str(
            architecture.get("specialization") or ""
        ).strip()
        if not specialization:
            fail(f"{path} must declare architecture.specialization")
        specialization_path = (
            "pal/bunshin/architecture_specializations/"
            f"{specialization}/specialization.json"
        )
        specialization_payload = json.loads(
            read_text(specialization_path)
        )
        if specialization_payload.get("family_id") != family_id:
            fail(
                f"{path} architecture specialization belongs to another family"
            )
        if payload.get("builders"):
            fail(f"{path} must not declare legacy builder bindings")
        if any(not str(value).endswith(".v2") for value in dict(payload.get("adapters") or {}).values()):
            fail(f"{path} references a non-V2 adapter")

    profile_paths = sorted(name for name in names if name.startswith("pal/bunshin/profile_templates/") and name.endswith(".toml"))
    if len(profile_paths) != 11:
        fail(f"expected 11 builtin role profiles, found {len(profile_paths)}")
    for path in profile_paths:
        payload = tomllib.loads(read_text(path))
        if not str(payload.get("profile_id") or "").strip() or not str(payload.get("profile_group") or "").strip():
            fail(f"{path} is missing profile identity")
        role = dict(payload.get("role") or {})
        if role and not list(payload.get("capability_groups") or []):
            fail(f"{path} role participant must declare capability_groups")
        if payload.get("contract"):
            fail(
                f"{path} must not select an architecture schema; "
                "the Family owns specialization"
            )
        output_policy = dict(payload.get("output_policy") or {})
        if role and not str(output_policy.get("primary_artifact") or "").strip():
            fail(f"{path} role participant is missing output_policy.primary_artifact")
        metadata = dict(payload.get("metadata") or {})
        if metadata.get("builtin") is not True:
            fail(f"{path} must be a managed builtin profile")

    scoped_execution = read_text("pal/bunshin/scoped_execution.py")
    for token in (
        "CANDIDATE_BUILDER_TOOL_SPECS",
        "VERIFICATION_BUILDER_TOOL_SPECS",
        "SWE_VERIFICATION_TOOL_SPECS",
        "UPDATE_CHECKLIST_TOOL_SPEC",
        "ADD_FINDING_TOOL_SPEC",
        "CONTRACT_SUBMIT_TOOL_SPEC",
        "REVIEW_SUBMIT_TOOL_SPEC",
        "op_path_delete",
    ):
        if token not in scoped_execution:
            fail(f"scoped execution is missing {token}")
    for forbidden in (
        "PLAN_BUILDER_CAPABILITIES",
        "CONTRACT_BUILDER_TOOL_SPECS",
        "SKELETON_BUILDER_TOOL_SPECS",
        "op_bunshin_checkpoint_commit",
        "op_bunshin_review_checkpoint",
    ):
        if forbidden in scoped_execution:
            fail(f"scoped execution contains legacy capability {forbidden}")

    generated_models = read_text("pal/execution/generated_tool_models.py")
    for forbidden in (
        "BunshinV2ContractBuilder",
        "BunshinV2SkeletonBuilder",
        "'executor': (Literal['profile', 'null']",
    ):
        if forbidden in generated_models:
            fail(f"generated tool models contain legacy contract protocol {forbidden}")

    manager = read_text("pal/bunshin/manager.py")
    for forbidden in ('"spawn"', '"finalize"', '"tick"', '"recover"'):
        if forbidden in manager:
            fail(f"V2 manager exposes legacy RPC {forbidden}")

    shared_messages = read_text("pal/shared/messages.py")
    for forbidden in ("TaskContextPack", "CheckpointEvent", "milestone_index"):
        if forbidden in shared_messages:
            fail(f"shared worker transport contains legacy symbol {forbidden}")

print(
    "Verified package source/data coverage "
    f"({len(package_source_paths)} files), runtime-root-only channel providers, "
    "V2 families, role playbooks, contract protocols, adapters, worker "
    "transport, and legacy cutover"
)
PY

install -m 0755 "$installer_source" "$installer_path"
bash -n "$installer_path"

mkdir -p "$install_bundle_dir"
install -m 0644 "$wheel_path" "$install_bundle_dir/$(basename "$wheel_path")"
install -m 0755 "$installer_path" "$install_bundle_dir/$(basename "$installer_path")"
mkdir -p "$install_bundle_dir/providers"
for provider_wheel in "$provider_dist_dir"/pal_channel_provider_*.whl; do
  install -m 0644 "$provider_wheel" "$install_bundle_dir/providers/$(basename "$provider_wheel")"
done
tar -czf "$install_bundle_path" -C "$install_bundle_dir" .
rm -rf "$install_bundle_dir"

echo "Built $wheel_path"
echo "Built $telegram_provider_wheel_path"
echo "Built $websocket_provider_wheel_path"
echo "Built $installer_path"
echo "Built $install_bundle_path"
echo "Verified ${#required_wheel_paths[@]} semantic wheel contract files plus all package source/data files"
echo "Verified provider wheels under providers/"
