#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_bin="${PYTHON:-python3}"
dist_dir="$repo_root/dist"
provider_dist_dir="$dist_dir/providers"
runtime_overlay_dir="$dist_dir/runtime_root"
runtime_overlay_path="$dist_dir/pal_v2-runtime-root-overlay.tar.gz"
installer_source="$repo_root/scripts/install_package.sh"
installer_path="$dist_dir/install-pal.sh"
install_bundle_dir="$dist_dir/install_bundle"
install_bundle_path="$dist_dir/pal_v2-install-bundle.tar.gz"
codex_harness_source="$repo_root/plugins/codex_architect_harness"
codex_harness_relative="plugins/community/codex_architect_harness"
codex_harness_files=(
  "plugin.toml"
  "codex_architect_harness_runtime.py"
  "codex_architect_worker.py"
)

mkdir -p "$dist_dir"
rm -rf "$repo_root/build" "$repo_root/src/pal_v2.egg-info"
rm -f "$dist_dir"/pal_v2-*.whl
rm -rf "$provider_dist_dir"
rm -rf "$runtime_overlay_dir"
rm -f "$runtime_overlay_path"
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
  "pal/core/tool_surface.toml"
  "pal/lsp/server_templates/clangd.toml"
  "pal/lsp/server_templates/pyright.toml"
  "pal/mcp/templates/stdio_server.toml"
  "pal/bunshin/families.py"
  "pal/bunshin/harness_request.py"
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
  "pal/bunshin/v2/adapters.py"
  "pal/bunshin/v2/artifacts.py"
  "pal/bunshin/v2/architecture_templates.py"
  "pal/bunshin/v2/candidate_builder.py"
  "pal/bunshin/v2/capabilities.py"
  "pal/bunshin/v2/catalog.py"
  "pal/bunshin/v2/ask_question.py"
  "pal/bunshin/v2/contract_protocol.py"
  "pal/bunshin/v2/contract_runtime.py"
  "pal/bunshin/v2/contract_submission.py"
  "pal/bunshin/v2/contracts.py"
  "pal/bunshin/v2/coroutine_runtime.py"
  "pal/bunshin/v2/cycle_protocol.py"
  "pal/bunshin/v2/delivery.py"
  "pal/bunshin/v2/execution.py"
  "pal/bunshin/v2/graph_compiler.py"
  "pal/bunshin/v2/graph_executor.py"
  "pal/bunshin/v2/graph_protocol.py"
  "pal/bunshin/v2/graph_satellites.py"
  "pal/bunshin/v2/machines.py"
  "pal/bunshin/v2/module_protocol.py"
  "pal/bunshin/v2/orchestration.py"
  "pal/bunshin/v2/paths.py"
  "pal/bunshin/v2/process_lifecycle.py"
  "pal/bunshin/v2/projections.py"
  "pal/bunshin/v2/recovery.py"
  "pal/bunshin/v2/replan.py"
  "pal/bunshin/v2/repository.py"
  "pal/bunshin/v2/review_findings.py"
  "pal/bunshin/v2/review_submission.py"
  "pal/bunshin/v2/role_gateway.py"
  "pal/bunshin/v2/role_runtime.py"
  "pal/bunshin/v2/service.py"
  "pal/bunshin/v2/sessions.py"
  "pal/bunshin/v2/skeleton.py"
  "pal/bunshin/v2/submission_drafts.py"
  "pal/bunshin/v2/submission_preflight.py"
  "pal/bunshin/v2/swe_verification.py"
  "pal/bunshin/v2/task_ledger.py"
  "pal/bunshin/v2/verification.py"
  "pal/bunshin/v2/verification_builder.py"
  "pal/bunshin/v2/work_items.py"
  "pal/bunshin/v2/worker_main.py"
  "pal/bunshin/v2/workflow_runtime.py"
  "pal/bunshin/v2/semantic_orchestration/architecture.py"
  "pal/bunshin/v2/semantic_orchestration/contracts.py"
  "pal/bunshin/v2/semantic_orchestration/implementation.py"
  "pal/bunshin/v2/semantic_orchestration/orchestrator.py"
  "pal/bunshin/v2/semantic_orchestration/review.py"
  "pal/bunshin/v2/semantic_orchestration/verification.py"
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
        'aliases=("channel_send_message",)',
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
        "_segment_text(spec.text",
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
        "pal/bunshin/v2/workers.py",
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

codex_harness_overlay_dir="$runtime_overlay_dir/$codex_harness_relative"
mkdir -p "$codex_harness_overlay_dir"
for harness_file in "${codex_harness_files[@]}"; do
  install -m 0644 \
    "$codex_harness_source/$harness_file" \
    "$codex_harness_overlay_dir/$harness_file"
  if ! cmp -s \
    "$codex_harness_source/$harness_file" \
    "$codex_harness_overlay_dir/$harness_file"; then
    echo "Runtime overlay differs from Codex harness source file: $harness_file" >&2
    exit 1
  fi
done
tar -czf "$runtime_overlay_path" -C "$runtime_overlay_dir" .

install -m 0755 "$installer_source" "$installer_path"
bash -n "$installer_path"

mkdir -p "$install_bundle_dir"
install -m 0644 "$wheel_path" "$install_bundle_dir/$(basename "$wheel_path")"
install -m 0644 "$runtime_overlay_path" "$install_bundle_dir/$(basename "$runtime_overlay_path")"
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
echo "Built $runtime_overlay_path"
echo "Built $installer_path"
echo "Built $install_bundle_path"
echo "Verified ${#required_wheel_paths[@]} semantic wheel contract files plus all package source/data files"
echo "Verified provider wheels under providers/ and runtime overlay at $codex_harness_relative/"
