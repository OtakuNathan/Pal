from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pal.execution import CapabilityResult
from pal.execution.git_tool import GIT_TOOL_CMD_DESCRIPTION, GIT_TOOL_DESCRIPTION, classify_git_command
from pal.execution.tool_search import ToolReadTool, ToolSearchTool
from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.memory import L3CommitRequest
from pal.minion.checklist import (
    build_acceptance_checklist,
    build_evidence_projection,
    compact_checklist,
    normalize_coverage_refs,
    resolve_evidence_ref,
)
from pal.minion.git_env import commit_milestone, inspect_milestone_checkpoint
from pal.minion.plan_builder import PLAN_BUILDER_CAPABILITIES, PLAN_BUILDER_TOOL_SPECS, plan_builder_tool_result
from pal.minion.profiles import filter_minion_allowed_capabilities, is_minion_capability_denied
from pal.minion.repository import MinionTaskingRepository
from pal.minion.review_gate_store import plan_target_key
from pal.minion.utils import coerce_int as _coerce_int
from pal.minion.workspace_file_tools import WORKSPACE_FILE_TOOL_SPECS, workspace_file_tool_result
from pal.minion.workspace_tools import _append_unique_artifact, _workspace_tool_result
from pal.plugins.l3 import MockL3Plugin
from pal.shared import (
    RUN_SHELL_SCOPE_HINT,
    RuntimeStatus,
    default_tool_result_text,
    format_dedicated_tool_route_hints,
    llm_tool_name,
    replace_internal_tool_names,
    replace_internal_tool_names_in_value,
)

MINION_DISCOVERY_TOOL_SURFACE = (
    "op_tool_search",
    "op_tool_read",
    "op_tool_call",
)


MINION_CODE_INTEL_TOOL_SURFACE = (
    "op_lsp_status",
    "op_lsp_doctor",
    "op_lsp_hover",
    "op_lsp_definition",
    "op_lsp_implementation",
    "op_lsp_references",
    "op_lsp_prepare_call_hierarchy",
    "op_lsp_incoming_calls",
    "op_lsp_outgoing_calls",
    "op_lsp_document_symbols",
    "op_lsp_workspace_symbols",
    "op_lsp_diagnostics",
)


MINION_DIRECT_WORK_TOOL_SURFACE = (
    "op_file_read",
    "op_file_edit",
    "op_file_write",
    "op_path_delete",
    "op_file_state",
    "op_git",
    "op_exec_shell",
    "op_minion_checkpoint_commit",
    "op_tree",
    "op_search",
    "op_minion_artifact_write",
    "op_minion_artifact_edit",
    *PLAN_BUILDER_CAPABILITIES,
    "op_minion_memory_candidate_write",
    "op_web_search",
    "op_web_read",
    "op_memory_recall",
    *MINION_CODE_INTEL_TOOL_SURFACE,
)


WORKSPACE_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_tree": {
        "name": "op_tree",
        "description": (
            "Use this first for structured directory listings under the current task repo; do not use op_exec_shell with ls/find/git ls-files "
            "for repo listing when this tool is visible. This does not modify anything."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative directory path. Use '.' for the repo root; do not use '/'."},
                "max_depth": {"type": "integer", "default": 2},
                "limit": {"type": "integer", "default": 200},
            },
        },
    },
    "op_search": {
        "name": "op_search",
        "description": (
            "Use this first for repository text search under the current task repo; do not use op_exec_shell with grep/rg/find text scans "
            "when this tool is visible. This does not modify anything."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string", "description": "Repo-relative directory path. Use '.' for the repo root; do not use '/'."},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["query"],
        },
    },
    "op_git": {
        "name": "op_git",
        "description": (
            GIT_TOOL_DESCRIPTION
            + " The working directory is fixed to the current task repo or reviewer scratch repo. "
            "Use this instead of op_exec_shell for git status, diff, log, show, changed-file evidence, "
            "and conservative audited git restore/revert mutations."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": GIT_TOOL_CMD_DESCRIPTION},
                "timeout_ms": {"type": "integer", "minimum": 1, "description": "Optional timeout in milliseconds."},
                "output_limit": {"type": "integer", "minimum": 256, "description": "Maximum stdout/stderr characters kept."},
            },
            "required": ["cmd"],
            "additionalProperties": False,
        },
    },
    "op_minion_artifact_write": {
        "name": "op_minion_artifact_write",
        "description": "Write one complete minion deliverable file under workspace.artifact_dir and register it as produced artifact evidence. Use this for planner/reviewer plans and long structured output; final chat should only point to the artifact. Duplicate paths get a numbered suffix unless overwrite=true.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "relative_path": {"type": "string", "description": "Artifact-dir-relative file path, for example plan.md."},
                "content": {"type": "string", "description": "UTF-8 text content to write."},
                "title": {"type": "string"},
                "role": {"type": "string", "description": "Artifact role such as primary, evidence, notes, or tests."},
                "mime_type": {"type": "string", "default": "text/markdown"},
                "overwrite": {"type": "boolean", "default": False, "description": "Overwrite an existing artifact path. Defaults to false; duplicate paths get a numbered suffix."},
            },
            "required": ["relative_path", "content"],
            "additionalProperties": False,
        },
    },
    "op_minion_artifact_edit": {
        "name": "op_minion_artifact_edit",
        "description": "Create, append to, or replace a text artifact under workspace.artifact_dir and register it as produced artifact evidence. Use append for long deliverables split into coherent sections; use replace only when rewriting the complete artifact.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "relative_path": {"type": "string", "description": "Artifact-dir-relative file path, for example plan.md."},
                "operation": {"type": "string", "enum": ["append", "replace"], "default": "append"},
                "content": {"type": "string", "description": "UTF-8 text content to append or replace with."},
                "create_if_missing": {"type": "boolean", "default": True},
                "title": {"type": "string"},
                "role": {"type": "string", "description": "Artifact role such as primary, evidence, notes, or tests."},
                "mime_type": {"type": "string", "default": "text/markdown"},
            },
            "required": ["relative_path", "content"],
            "additionalProperties": False,
        },
    },
    "op_minion_memory_candidate_write": {
        "name": "op_minion_memory_candidate_write",
        "description": "Write a reusable memory candidate to this minion run's ephemeral in-memory L3. Pal will ask the user before absorbing candidates into durable memory.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "Memory kind such as fact or case."},
                "scope": {"type": "string", "default": "task"},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "canonical_key": {"type": "string"},
                "topics": {"type": "array", "items": {"type": "string"}},
                "payload": {"type": "object"},
                "situation_text": {"type": "string"},
                "task_text": {"type": "string"},
                "action_text": {"type": "string"},
                "result_text": {"type": "string"},
            },
            "required": ["kind", "summary"],
        },
    },
    "op_minion_review_gate_submit": {
        "name": "op_minion_review_gate_submit",
        "description": (
            "Submit a structured reviewer gate result for a plan, checkpoint, or repair target. Reviewer minions must use this "
            "instead of reporting gate verdict only in prose. Pass gates that cite command or LSP evidence must be backed by "
            "actual op_exec_shell or op_lsp_* calls in this reviewer run. When the required evidence already proves pass, fail, "
            "or partial, submit this gate immediately instead of collecting optional extra evidence."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "gate_kind": {"type": "string", "enum": ["plan_acceptance", "checkpoint_verification", "repair_verification"]},
                "target": {"type": "object"},
                "verdict": {"type": "string", "enum": ["pass", "fail", "partial"]},
                "summary": {"type": "string"},
                "findings": {"type": "array", "items": {"type": "object"}},
                "required_fixes": {"type": "array", "items": {"type": "object"}},
                "evidence": {"type": "array", "items": {"type": "object"}},
                "commands_run": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "For checkpoint/repair pass verdicts, include at least one command/check entry. Prefer evidence_ref_id or call_id plus covers=[acceptance index or exact criterion]; Pal fills command, cwd, status/exit_code, and output_summary from the recorded op_exec_shell evidence. Non-skipped command entries must match an op_exec_shell call from this reviewer run.",
                },
                "api_evidence": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Source/docs/LSP/build evidence for API and call-shape claims. Prefer evidence_ref_id or call_id plus covers=[acceptance index or exact criterion]; Pal fills source/LSP details from recorded evidence when available. Pass verdicts require api_evidence or metadata.api_evidence_not_applicable=true with a reason. LSP entries must match an op_lsp_* call from this reviewer run.",
                },
                "residual_risk": {"type": "array", "items": {"type": "object"}},
                "report_artifact_ref": {"type": "object"},
                "reviewer_profile": {"type": "string"},
                "metadata": {
                    "type": "object",
                    "description": "Use metadata.api_evidence_not_applicable=true only when API evidence is genuinely not applicable. If no LSP evidence is used on a pass verdict, set metadata.lsp_evidence_not_applicable=true with a reason.",
                },
            },
            "required": ["gate_kind", "target", "verdict"],
        },
    },
    "op_minion_review_checkpoint": {
        "name": "op_minion_review_checkpoint",
        "description": (
            "Submit a checkpoint or repair review verdict using the current reviewer workspace target. Prefer this over "
            "op_minion_review_gate_submit for checkpoint/repair reviews: Pal fills target binding, gate kind, tool provenance, "
            "and metadata while preserving the same review gate validation. When evidence already covers the binding contract "
            "and verdict, call this tool immediately; do not keep probing for optional confidence."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "checkpoint_id": {"type": "string", "description": "Optional checkpoint id. Defaults to the current review target checkpoint."},
                "commit_sha": {"type": "string", "description": "Optional checkpoint commit SHA. Defaults to the current review target commit."},
                "gate_kind": {"type": "string", "enum": ["checkpoint_verification", "repair_verification"]},
                "verdict": {"type": "string", "enum": ["pass", "fail", "partial"]},
                "summary": {"type": "string"},
                "findings": {"type": "array", "items": {"type": "object"}},
                "required_fixes": {"type": "array", "items": {"type": "object"}},
                "checks": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Command/check evidence. Prefer call_id or evidence_ref_id plus covers=[acceptance index or exact criterion]; Pal fills command, status/exit_code, and output_summary from this reviewer run's op_exec_shell evidence.",
                },
                "api_checks": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Source/docs/LSP/build evidence for API and call-shape claims. Prefer call_id or evidence_ref_id plus covers=[acceptance index or exact criterion]; Pal fills source/LSP details from recorded evidence when available.",
                },
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Preferred compact evidence references. Use evidence_id such as EV-1 plus covers=[AC-1]. Pal maps EV-* to this reviewer run's recorded command/source/LSP evidence and fills legacy gate evidence fields.",
                },
                "residual_risk": {"type": "array", "items": {"type": "object"}},
                "api_evidence_not_applicable_reason": {"type": "string"},
                "lsp_evidence_not_applicable_reason": {"type": "string"},
            },
            "required": ["verdict", "summary"],
        },
    },
    "op_minion_checkpoint_commit": {
        "name": "op_minion_checkpoint_commit",
        "description": (
            "Create the current milestone checkpoint commit in the minion workspace git branch and return structured "
            "commit evidence. Use this after implementation and verification are complete. The tool stages source, "
            "tests, docs, and project config while excluding generated build/cache artifacts and minion_outputs."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short milestone title for the commit message."},
            },
        },
    },
}
WORKSPACE_TOOL_SPECS.update(WORKSPACE_FILE_TOOL_SPECS)
WORKSPACE_TOOL_SPECS.update(PLAN_BUILDER_TOOL_SPECS)

_REPAIR_EDIT_TOOL_NAMES = {
    "op_file_edit",
    "op_file_write",
    "op_path_delete",
}

_PRE_EDIT_VERIFICATION_COMMAND_MARKERS = (
    "--collect-only",
    "pytest",
    "unittest",
    "py_compile",
    "mypy",
    "pyright",
    "ruff",
    "cargo test",
    "go test",
    "ctest",
    "make test",
    "npm test",
    "pnpm test",
    "yarn test",
    "tox",
    "nox",
)


def _augment_minion_capability_spec(spec: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    spec = _scrub_minion_capability_spec(spec)
    name = str(spec.get("canonical_path") or spec.get("name") or "").strip()
    if name != "op_exec_shell":
        return spec
    guidance = _minion_shell_dedicated_tool_guidance(allowed)
    if not guidance:
        return spec
    augmented = deepcopy(spec)
    description = str(augmented.get("description") or "").strip()
    if guidance not in description:
        augmented["description"] = f"{description} {guidance}".strip()
    schema = deepcopy(augmented.get("parameters_schema") or {"type": "object", "properties": {}})
    properties = dict(schema.get("properties") or {})
    cmd_property = dict(properties.get("cmd") or {})
    cmd_description = str(cmd_property.get("description") or "").strip()
    if guidance not in cmd_description:
        cmd_property["description"] = f"{cmd_description} {guidance}".strip()
    properties["cmd"] = cmd_property
    schema["properties"] = properties
    augmented["parameters_schema"] = schema
    return augmented


def _scrub_minion_capability_spec(spec: dict[str, Any]) -> dict[str, Any]:
    scrubbed = dict(spec)
    canonical = str(scrubbed.get("canonical_path") or scrubbed.get("name") or "").strip()
    if canonical:
        scrubbed["canonical_path"] = canonical
        scrubbed["name"] = llm_tool_name(canonical)
    if "description" in scrubbed:
        scrubbed["description"] = replace_internal_tool_names(scrubbed.get("description"))
    if "parameters_schema" in scrubbed:
        scrubbed["parameters_schema"] = replace_internal_tool_names_in_value(
            dict(scrubbed.get("parameters_schema") or {"type": "object", "properties": {}})
        )
    if "result_schema" in scrubbed:
        scrubbed["result_schema"] = replace_internal_tool_names_in_value(
            dict(scrubbed.get("result_schema") or {"type": "object", "properties": {}})
        )
    return scrubbed


def _minion_shell_dedicated_tool_guidance(allowed: set[str]) -> str:
    hints = format_dedicated_tool_route_hints(allowed)
    if not hints:
        return ""
    return f"Minion tool choice: when visible, use {hints}. {RUN_SHELL_SCOPE_HINT}"


def _canonical_minion_capability_name(name: object) -> str:
    raw = str(name or "").strip()
    if not raw:
        return ""
    if raw in {"shell", "shell_exec", "run_shell"}:
        return "op_exec_shell"
    if raw in {"web_search", "search_web"}:
        return "op_web_search"
    if raw in {"web_read", "read_web"}:
        return "op_web_read"
    if raw in WORKSPACE_TOOL_SPECS:
        return raw
    candidates = tuple(dict.fromkeys((*MINION_DIRECT_WORK_TOOL_SURFACE, *MINION_DISCOVERY_TOOL_SURFACE)))
    for candidate in candidates:
        if llm_tool_name(candidate) == raw:
            return candidate
    return raw


@dataclass
class MinionScopedExecutionRuntime:
    base_runtime: Any
    allowed_capabilities: list[str]
    workspace: dict[str, Any] = field(default_factory=dict)
    produced_artifacts: list[dict[str, Any]] = field(default_factory=list)
    memory_l3: MockL3Plugin | None = None

    def __post_init__(self) -> None:
        normalized = [_canonical_minion_capability_name(item) for item in self.allowed_capabilities]
        self.allowed_capabilities = filter_minion_allowed_capabilities(normalized)

    def list_capability_specs(self) -> list[dict[str, Any]]:
        allowed = set(self.allowed_capabilities)
        specs = []
        for name, spec in WORKSPACE_TOOL_SPECS.items():
            if not self._tool_visible_for_workspace(name):
                continue
            if name in allowed and not is_minion_capability_denied(name):
                specs.append(_scrub_minion_capability_spec(spec))
        list_specs = getattr(self.base_runtime, "list_capability_specs", None)
        if callable(list_specs):
            for spec in list(list_specs()):
                name = str(spec.get("name") or "").strip()
                canonical = str(spec.get("canonical_path") or name).strip()
                if not self._tool_visible_for_workspace(canonical):
                    continue
                if canonical in WORKSPACE_TOOL_SPECS:
                    continue
                if canonical in allowed and not is_minion_capability_denied(canonical):
                    specs.append(_augment_minion_capability_spec(spec, allowed))
        return specs

    def get_capability_spec(self, name: str) -> dict[str, Any] | None:
        name = self._resolve_minion_capability_alias(name)
        if not self._tool_visible_for_workspace(name):
            return None
        if name in WORKSPACE_TOOL_SPECS:
            if name not in set(self.allowed_capabilities) or is_minion_capability_denied(name):
                return None
            return _scrub_minion_capability_spec(WORKSPACE_TOOL_SPECS[name])
        get_spec = getattr(self.base_runtime, "get_capability_spec", None)
        if not callable(get_spec):
            return None
        spec = get_spec(name)
        if spec is None:
            return None
        canonical = str(spec.get("canonical_path") or spec.get("name") or name).strip()
        if canonical not in set(self.allowed_capabilities) or is_minion_capability_denied(canonical):
            return None
        return _augment_minion_capability_spec(spec, set(self.allowed_capabilities))

    async def execute_tool_async(
        self,
        call: CanonicalToolCall,
        *,
        allow_tools: bool = True,
        turn_id: str | None = None,
    ) -> CanonicalToolResult:
        resolved_name = self._resolve_minion_capability_alias(call.name)
        if resolved_name != call.name:
            call = CanonicalToolCall(name=resolved_name, args=dict(call.args), call_id=call.call_id)
        if not self._tool_visible_for_workspace(call.name):
            text = "checkpoint and repair reviewers must use review_checkpoint; the generic review_gate_submit tool is hidden for this target"
            return CanonicalToolResult(
                name=call.name,
                ok=False,
                text=text,
                structured={"reason": "use_review_checkpoint_for_checkpoint_target", "required_tool": "review_checkpoint"},
                call_id=call.call_id,
                llm_text=text,
                status=RuntimeStatus.INVALID,
            )
        allowed = set(str(item) for item in self.allowed_capabilities)
        if call.name not in allowed or is_minion_capability_denied(call.name):
            return CanonicalToolResult(
                name=call.name,
                ok=False,
                text="capability is not allowed for this minion run",
                structured={"reason": "capability_not_allowed", "capability": call.name},
                call_id=call.call_id,
                llm_text="capability is not allowed for this minion run",
                status=RuntimeStatus.ERROR,
            )
        repair_guard = _repair_pre_edit_tool_guard(call, self.workspace)
        if repair_guard is not None:
            return repair_guard
        if call.name == "op_tool_search":
            return _capability_result_to_tool_result(
                call,
                ToolSearchTool(runtime=self).invoke(dict(call.args)),
            )
        if call.name == "op_tool_read":
            return _capability_result_to_tool_result(
                call,
                ToolReadTool(runtime=self).invoke(dict(call.args)),
            )
        if call.name == "op_tool_call":
            return await self._execute_scoped_tool_call(call, allow_tools=allow_tools, turn_id=turn_id)
        if call.name in WORKSPACE_TOOL_SPECS:
            if call.name in PLAN_BUILDER_CAPABILITIES:
                return plan_builder_tool_result(call, self.workspace, self.produced_artifacts)
            if call.name == "op_minion_memory_candidate_write":
                return _minion_memory_candidate_result(call, self.memory_l3)
            if call.name == "op_minion_review_gate_submit":
                return _minion_review_gate_submit_result(call, self.workspace)
            if call.name == "op_minion_review_checkpoint":
                return _minion_review_checkpoint_result(call, self.workspace)
            if call.name == "op_minion_checkpoint_commit":
                return _minion_checkpoint_commit_result(call, self.workspace)
            if call.name == "op_git":
                return await _minion_git_result(call, self.workspace, self.base_runtime, allow_tools=allow_tools, turn_id=turn_id)
            if call.name in WORKSPACE_FILE_TOOL_SPECS:
                result = await workspace_file_tool_result(
                    call,
                    self.workspace,
                    self.base_runtime,
                    allow_tools=allow_tools,
                    turn_id=turn_id,
                )
                _mark_repair_workspace_edit(self.workspace, call, result)
                return result
            result = _workspace_tool_result(call, self.workspace)
            if call.name in {"op_minion_artifact_write", "op_minion_artifact_edit"} and result.ok:
                artifact = dict((result.structured or {}).get("artifact") or result.structured or {})
                if artifact:
                    _append_unique_artifact(self.produced_artifacts, artifact)
            return result
        if _is_lsp_capability_name(call.name):
            blocked = _lsp_unavailable_for_turn_result(call, self.workspace)
            if blocked is not None:
                return blocked
            normalized = _normalize_minion_lsp_call(call, self.workspace)
            if isinstance(normalized, CanonicalToolResult):
                return normalized
            call = normalized
        result = await self.base_runtime.execute_tool_async(call, allow_tools=allow_tools, turn_id=turn_id)
        if _is_lsp_capability_name(call.name):
            _mark_lsp_unavailable_for_turn(self.workspace, call, result)
        return result

    async def _execute_scoped_tool_call(
        self,
        call: CanonicalToolCall,
        *,
        allow_tools: bool = True,
        turn_id: str | None = None,
    ) -> CanonicalToolResult:
        target_name = self._resolve_minion_capability_alias(
            str(call.args.get("name") or call.args.get("capability") or call.args.get("tool") or "").strip()
        )
        if not target_name:
            return CanonicalToolResult(
                name=call.name,
                ok=False,
                text="name is required",
                structured={"reason": "name_required"},
                call_id=call.call_id,
                llm_text="name is required",
                status=RuntimeStatus.INVALID,
            )
        spec = self.get_capability_spec(target_name)
        canonical = str((spec or {}).get("canonical_path") or (spec or {}).get("name") or target_name).strip()
        if spec is None or canonical == call.name:
            return CanonicalToolResult(
                name=call.name,
                ok=False,
                text=f"capability is not allowed for this minion: {target_name}",
                structured={"reason": "capability_not_allowed", "target": target_name},
                call_id=call.call_id,
                llm_text=f"capability is not allowed for this minion: {target_name}",
                status=RuntimeStatus.ERROR,
            )
        return await self.execute_tool_async(
            CanonicalToolCall(name=canonical, args=dict(call.args.get("args") or {}), call_id=call.call_id),
            allow_tools=allow_tools,
            turn_id=turn_id,
        )

    def _resolve_minion_capability_alias(self, name: object) -> str:
        raw = str(name or "").strip()
        if not raw:
            return ""
        canonical_raw = _canonical_minion_capability_name(raw)
        if canonical_raw != raw and canonical_raw in set(self.allowed_capabilities) and not is_minion_capability_denied(canonical_raw):
            return canonical_raw
        if raw in set(self.allowed_capabilities) or raw in WORKSPACE_TOOL_SPECS:
            return raw
        matches = [
            canonical
            for canonical in self.allowed_capabilities
            if llm_tool_name(canonical) == raw and not is_minion_capability_denied(canonical)
        ]
        return matches[0] if len(matches) == 1 else raw

    def _tool_visible_for_workspace(self, name: str) -> bool:
        canonical = str(name or "").strip()
        if canonical != "op_minion_review_gate_submit":
            return True
        gate_kind = str(self.workspace.get("review_target_gate_kind") or "").strip()
        if gate_kind in {"checkpoint_verification", "repair_verification"}:
            return False
        if str(self.workspace.get("review_target_checkpoint_id") or "").strip():
            return False
        return True


def _repair_pre_edit_tool_guard(call: CanonicalToolCall, workspace: dict[str, Any]) -> CanonicalToolResult | None:
    if not _is_checkpoint_repair_workspace(workspace) or _repair_workspace_has_successful_edit(workspace):
        return None
    if not _repair_requires_successful_edit_before_verification(workspace):
        return None
    target_name = _effective_capability_name(call)
    if not target_name:
        return None
    args = _effective_tool_args(call)
    if _is_lsp_capability_name(target_name):
        return _repair_requires_first_edit_result(call, target_name, workspace)
    if target_name == "op_minion_checkpoint_commit":
        return _repair_requires_first_edit_result(call, target_name, workspace)
    if target_name == "op_exec_shell" and _is_pre_edit_verification_shell_command(args):
        return _repair_requires_first_edit_result(call, target_name, workspace)
    return None


def _is_checkpoint_repair_workspace(workspace: dict[str, Any]) -> bool:
    if not isinstance(workspace, dict):
        return False
    return isinstance(workspace.get("current_repair_attempt"), dict) or isinstance(workspace.get("checkpoint_repair"), dict)


def _repair_workspace_has_successful_edit(workspace: dict[str, Any]) -> bool:
    if not isinstance(workspace, dict):
        return False
    return bool(workspace.get("repair_workspace_changed") or workspace.get("repair_first_edit_completed"))


def _is_pre_edit_verification_shell_command(args: dict[str, Any]) -> bool:
    command = " ".join(str(args.get("cmd") or args.get("command") or "").strip().lower().split())
    if not command:
        return False
    return any(marker in command for marker in _PRE_EDIT_VERIFICATION_COMMAND_MARKERS)


def _repair_requires_first_edit_result(call: CanonicalToolCall, target_name: str, workspace: dict[str, Any]) -> CanonicalToolResult:
    text = (
        "Repair checklist has not had a successful workspace edit yet. "
        "First complete a repair_checklist item with op_file_edit, "
        "op_file_write, or op_path_delete. LSP, broad verification, "
        "and checkpointing are available after the first successful repair edit."
    )
    return CanonicalToolResult(
        name=call.name,
        ok=False,
        text=text,
        structured={
            "reason": "repair_requires_checklist_edit_first",
            "target_capability": target_name,
            "allowed_first_tools": sorted(_REPAIR_EDIT_TOOL_NAMES),
            "repair_checklist": _active_repair_checklist(workspace),
        },
        call_id=call.call_id,
        llm_text=text,
        status=RuntimeStatus.INVALID,
    )


def _active_repair_checklist(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    payload = workspace.get("checkpoint_repair")
    if not isinstance(payload, dict):
        payload = {}
    checklist = payload.get("repair_checklist")
    if isinstance(checklist, list):
        return [dict(item) for item in checklist[:8] if isinstance(item, dict)]
    active_todo = workspace.get("active_gate_todo")
    if not isinstance(active_todo, dict):
        return []
    repair_items = active_todo.get("repair_items")
    if not isinstance(repair_items, list):
        repair_items = [
            item
            for item in list(active_todo.get("items") or [])
            if isinstance(item, dict) and str(item.get("kind") or "") == "repair"
        ]
    return [dict(item) for item in repair_items[:8] if isinstance(item, dict)]


def _repair_requires_successful_edit_before_verification(workspace: dict[str, Any]) -> bool:
    checklist = _active_repair_checklist(workspace)
    if not checklist:
        return True
    for item in checklist:
        metadata = dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), dict) else {}
        impact = " ".join(
            str((item.get(key) if key in item else metadata.get(key)) or "")
            for key in ("contract_impact", "impact")
        ).strip().lower()
        if impact and impact not in {"none", "no impact", "not applicable", "n/a", "na"}:
            return True
        text = " ".join(
            str(item.get(key) or "")
            for key in ("action", "title", "summary", "suggested_fix", "area")
        ).lower()
        if any(
            marker in text
            for marker in (
                "fix",
                "change",
                "correct",
                "edit",
                "enforce",
                "implement",
                "modify",
                "raise",
                "reject",
                "render",
                "replace",
                "return",
                "update",
                "validate",
            )
        ):
            return True
    return False


def _mark_repair_workspace_edit(workspace: dict[str, Any], call: CanonicalToolCall, result: CanonicalToolResult) -> None:
    if call.name not in _REPAIR_EDIT_TOOL_NAMES or not result.ok or not _is_checkpoint_repair_workspace(workspace):
        return
    workspace["repair_workspace_changed"] = True
    workspace["repair_first_edit_completed"] = True
    edits = workspace.get("repair_workspace_edits")
    if not isinstance(edits, list):
        edits = []
    entry = {
        "tool": call.name,
        "path": str((call.args or {}).get("path") or ""),
        "call_id": str(call.call_id or ""),
    }
    edits.append({key: value for key, value in entry.items() if value})
    workspace["repair_workspace_edits"] = edits[-20:]


def _normalize_minion_lsp_call(call: CanonicalToolCall, workspace: dict[str, Any]) -> CanonicalToolCall | CanonicalToolResult:
    root = _effective_lsp_workspace_root(workspace)
    if root is None:
        return call
    args = dict(call.args or {})
    args["workspace_root"] = str(root)
    if not any(key in args for key in ("workspace_languages", "languages", "language", "primary_language")):
        languages = _string_list((workspace.get("lsp_setup") or {}).get("languages") if isinstance(workspace.get("lsp_setup"), dict) else None)
        if not languages:
            languages = _string_list(workspace.get("languages"))
        if languages:
            args["workspace_languages"] = languages
    raw_file = str(args.get("file") or args.get("path") or "").strip()
    if raw_file:
        file_path = Path(raw_file).expanduser()
        if not file_path.is_absolute():
            file_path = root / file_path
        file_path = file_path.resolve()
        if not _path_is_relative_to(file_path, root):
            text = (
                "LSP file path must stay inside the minion workspace root "
                f"({root}); got {file_path}"
            )
            return CanonicalToolResult(
                name=call.name,
                ok=False,
                text=text,
                structured={
                    "reason": "lsp_path_outside_workspace",
                    "workspace_root": str(root),
                    "file": str(file_path),
                },
                call_id=call.call_id,
                llm_text=text,
                status=RuntimeStatus.ERROR,
            )
        args["file"] = str(file_path)
        if "path" in args:
            args["path"] = str(file_path)
    return CanonicalToolCall(name=call.name, args=args, call_id=call.call_id)


def _lsp_unavailable_for_turn_result(call: CanonicalToolCall, workspace: dict[str, Any]) -> CanonicalToolResult | None:
    if _lsp_operation_name(call.name) in {"status", "doctor"}:
        return None
    unavailable = workspace.get("lsp_unavailable_for_turn")
    if not isinstance(unavailable, dict):
        return None
    reason = str(unavailable.get("reason") or "LSP was marked unavailable earlier in this minion turn").strip()
    text = f"LSP skipped for this minion turn: {reason}"
    return CanonicalToolResult(
        name=call.name,
        ok=False,
        text=text,
        structured={
            "status": "skipped",
            "reason": "lsp_unavailable_for_turn",
            "unavailable_reason": reason,
            "operation": _lsp_operation_name(call.name),
        },
        call_id=call.call_id,
        llm_text=text,
        status=RuntimeStatus.SKIPPED,
    )


def _mark_lsp_unavailable_for_turn(workspace: dict[str, Any], call: CanonicalToolCall, result: CanonicalToolResult) -> None:
    if _lsp_operation_name(call.name) != "diagnostics":
        return
    structured = result.structured if isinstance(result.structured, dict) else {}
    result_payload = structured.get("result") if isinstance(structured.get("result"), dict) else {}
    evidence = structured.get("evidence") if isinstance(structured.get("evidence"), dict) else {}
    evidence_result = evidence.get("result") if isinstance(evidence.get("result"), dict) else {}
    diagnostics_state = str(result_payload.get("diagnostics_state") or evidence_result.get("diagnostics_state") or "").strip()
    if diagnostics_state != "timed_out":
        return
    file_text = str((call.args or {}).get("file") or (call.args or {}).get("path") or "").strip()
    server = structured.get("server") if isinstance(structured.get("server"), dict) else {}
    server_id = str(server.get("server_id") or "").strip()
    reason = "LSP diagnostics timed out"
    if file_text:
        reason += f" for {file_text}"
    if server_id:
        reason += f" on {server_id}"
    workspace["lsp_unavailable_for_turn"] = {
        "reason": reason,
        "operation": "diagnostics",
        "file": file_text,
        "server_id": server_id,
    }


async def _minion_git_result(
    call: CanonicalToolCall,
    workspace: dict[str, Any],
    base_runtime: Any,
    *,
    allow_tools: bool = True,
    turn_id: str | None = None,
) -> CanonicalToolResult:
    root = _effective_git_workspace_root(workspace)
    if root is None:
        text = "git workspace root is unavailable for this minion run"
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=text,
            structured={"reason": "git_workspace_root_missing"},
            call_id=call.call_id,
            llm_text=text,
            status=RuntimeStatus.ERROR,
        )
    policy = classify_git_command((call.args or {}).get("cmd"))
    workspace_policy = dict((workspace or {}).get("workspace_policy") or {})
    if str(workspace_policy.get("mode") or "").strip().lower() == "read_only_repo" and policy.is_mutation:
        text = "read_only_repo minions may use git for inspection only; git mutations are not allowed in reviewer scratch workspaces"
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=text,
            structured={"reason": "read_only_repo_git_mutation_forbidden", "classification": policy.to_dict()},
            call_id=call.call_id,
            llm_text=text,
            status=RuntimeStatus.FORBIDDEN,
        )
    if str(workspace_policy.get("mode") or "").strip().lower() == "read_only_repo" and not (root / ".git").exists():
        checkpoint_git = workspace.get("review_target_checkpoint_git")
        text = (
            "review scratch workspace has no .git directory; use review_target.checkpoint_git for checkpoint commit metadata, "
            "changed files, and stats instead of running git in the scratch tree"
        )
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=text,
            structured={
                "reason": "review_scratch_git_unavailable",
                "workspace_root": str(root),
                "checkpoint_git": dict(checkpoint_git or {}) if isinstance(checkpoint_git, dict) else {},
                "hint": "Use review_target.checkpoint_git for commit_sha, changed_files, parent_commit_sha, and stat; use file/search/read tools for scratch source inspection.",
            },
            call_id=call.call_id,
            llm_text=text,
            status=RuntimeStatus.NOT_FOUND,
        )
    args = dict(call.args or {})
    args["cwd"] = str(root)
    return await base_runtime.execute_tool_async(
        CanonicalToolCall(name="op_git", args=args, call_id=call.call_id),
        allow_tools=allow_tools,
        turn_id=turn_id,
    )


def _effective_git_workspace_root(workspace: dict[str, Any]) -> Path | None:
    workspace_policy = dict((workspace or {}).get("workspace_policy") or {})
    keys = ("review_scratch_repo_path", "review_scratch_dir") if str(workspace_policy.get("mode") or "").strip().lower() == "read_only_repo" else ("repo_path", "task_repo_path", "target_repo_path")
    for key in keys:
        raw = str((workspace or {}).get(key) or "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
    return None


def _effective_lsp_workspace_root(workspace: dict[str, Any]) -> Path | None:
    for key in ("review_scratch_repo_path", "repo_path"):
        raw = str((workspace or {}).get(key) or "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
    return None


def _capability_result_to_tool_result(call: CanonicalToolCall, result: CapabilityResult) -> CanonicalToolResult:
    return CanonicalToolResult(
        name=call.name,
        ok=result.status == RuntimeStatus.OK,
        text=result.text,
        structured=result.structured,
        call_id=call.call_id,
        llm_text=getattr(result, "llm_text", ""),
        status=result.status,
    )


def _minion_memory_candidate_result(call: CanonicalToolCall, memory_l3: MockL3Plugin | None) -> CanonicalToolResult:
    if memory_l3 is None:
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text="minion memory candidate store is not available",
            structured={"reason": "minion_memory_unavailable"},
            call_id=call.call_id,
            llm_text="minion memory candidate store is not available",
            status=RuntimeStatus.ERROR,
        )
    try:
        result = memory_l3.commit(
            L3CommitRequest(
                kind=str(call.args.get("kind") or "case"),
                scope=str(call.args.get("scope") or "task"),
                title=str(call.args.get("title") or ""),
                summary=str(call.args.get("summary") or ""),
                canonical_key=str(call.args.get("canonical_key")) if call.args.get("canonical_key") is not None else None,
                payload=dict(call.args.get("payload") or {}),
                topics=[str(value) for value in list(call.args.get("topics") or [])],
                situation_text=str(call.args.get("situation_text") or ""),
                task_text=str(call.args.get("task_text") or ""),
                action_text=str(call.args.get("action_text") or ""),
                result_text=str(call.args.get("result_text") or ""),
            )
        )
        payload = {"memory_candidate": result.hit or {"document_id": result.document_id}}
        return CanonicalToolResult(
            name=call.name,
            ok=result.status == RuntimeStatus.OK,
            text="memory candidate recorded",
            structured=payload,
            call_id=call.call_id,
            llm_text="memory candidate recorded",
            status=result.status,
        )
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=message,
            structured={"error": message, "error_type": exc.__class__.__name__},
            call_id=call.call_id,
            llm_text=message,
            status=RuntimeStatus.ERROR,
        )


def _minion_review_gate_submit_result(call: CanonicalToolCall, workspace: dict[str, Any]) -> CanonicalToolResult:
    runtime_root = str(workspace.get("runtime_root") or "").strip()
    if not runtime_root:
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text="runtime_root is missing from minion workspace",
            structured={"reason": "runtime_root_missing"},
            call_id=call.call_id,
            llm_text="runtime_root is missing from minion workspace",
            status=RuntimeStatus.ERROR,
        )
    try:
        args = dict(call.args or {})
        args.setdefault("reviewer_profile", str(workspace.get("minion_profile") or ""))
        target = dict(args.get("target") or {})
        target.setdefault("checkpoint_id", str(workspace.get("review_target_checkpoint_id") or ""))
        target.setdefault("commit_sha", str(workspace.get("review_target_commit_sha") or ""))
        target.setdefault("run_id", str(workspace.get("review_target_run_id") or ""))
        if not isinstance(target.get("source_contract"), dict) and isinstance(workspace.get("review_target_source_contract"), dict):
            target["source_contract"] = dict(workspace.get("review_target_source_contract") or {})
        if not isinstance(target.get("plan_ref"), dict) and isinstance(workspace.get("review_target_plan_ref"), dict):
            target["plan_ref"] = dict(workspace.get("review_target_plan_ref") or {})
        plan_ref_error = _bind_review_target_plan_ref(target, workspace)
        if plan_ref_error:
            return CanonicalToolResult(
                name=call.name,
                ok=False,
                text=plan_ref_error,
                structured={"reason": "review_gate_plan_ref_drift", "error": plan_ref_error},
                call_id=call.call_id,
                llm_text=plan_ref_error,
                status=RuntimeStatus.INVALID,
            )
        args["target"] = target
        repository = MinionTaskingRepository(runtime_root=Path(runtime_root))
        args = _with_review_tool_provenance(args, workspace, repository=repository)
        provenance_error = _review_gate_provenance_error(args, workspace)
        if provenance_error:
            return CanonicalToolResult(
                name=call.name,
                ok=False,
                text=provenance_error,
                structured={"reason": "review_gate_provenance_invalid", "error": provenance_error},
                call_id=call.call_id,
                llm_text=provenance_error,
                status=RuntimeStatus.INVALID,
            )
        payload = repository.submit_review_gate(
            args,
            reviewer_profile=str(args.get("reviewer_profile") or ""),
            work_order_id=str(workspace.get("review_source_work_order_id") or workspace.get("work_order_id") or ""),
            run_id=str(workspace.get("run_id") or ""),
        )
        return CanonicalToolResult(
            name=call.name,
            ok=True,
            text="minion review gate recorded",
            structured=payload,
            call_id=call.call_id,
            llm_text="minion review gate recorded",
            status=RuntimeStatus.OK,
        )
    except ValueError as exc:
        message = f"{exc.__class__.__name__}: {exc}"
        error = str(exc)
        structured = _review_gate_validation_error_payload(error, args if "args" in locals() else {}, workspace)
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=message,
            structured=structured,
            call_id=call.call_id,
            llm_text=_review_gate_validation_llm_text(error, structured),
            status=RuntimeStatus.INVALID,
        )
    except Exception as exc:
        message = f"{exc.__class__.__name__}: {exc}"
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=message,
            structured={"error": str(exc), "error_type": exc.__class__.__name__},
            call_id=call.call_id,
            llm_text=message,
            status=RuntimeStatus.ERROR,
        )


def _minion_review_checkpoint_result(call: CanonicalToolCall, workspace: dict[str, Any]) -> CanonicalToolResult:
    args = dict(call.args or {})
    gate_kind = str(args.get("gate_kind") or workspace.get("review_target_gate_kind") or "checkpoint_verification").strip()
    if gate_kind not in {"checkpoint_verification", "repair_verification"}:
        message = "op_minion_review_checkpoint gate_kind must be checkpoint_verification or repair_verification"
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=message,
            structured={"reason": "invalid_gate_kind"},
            call_id=call.call_id,
            llm_text=message,
            status=RuntimeStatus.INVALID,
        )
    ref_commands, ref_api_evidence, ref_error = _review_checkpoint_evidence_ref_checks(args.get("evidence_refs"), workspace)
    if ref_error:
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=ref_error,
            structured={
                "reason": "review_checkpoint_evidence_ref_invalid",
                "error": ref_error,
                "usable_evidence_refs": _usable_review_evidence_refs(workspace),
            },
            call_id=call.call_id,
            llm_text=ref_error,
            status=RuntimeStatus.INVALID,
        )
    target = {
        "checkpoint_id": str(args.get("checkpoint_id") or workspace.get("review_target_checkpoint_id") or ""),
        "commit_sha": str(args.get("commit_sha") or workspace.get("review_target_commit_sha") or ""),
        "run_id": str(workspace.get("review_target_run_id") or ""),
    }
    gate_args: dict[str, Any] = {
        "gate_kind": gate_kind,
        "target": target,
        "verdict": str(args.get("verdict") or "").strip(),
        "summary": str(args.get("summary") or "").strip(),
        "findings": list(args.get("findings") or []),
        "required_fixes": list(args.get("required_fixes") or []),
        "commands_run": [*_review_checkpoint_checks(args.get("checks")), *ref_commands],
        "api_evidence": [*_review_checkpoint_checks(args.get("api_checks")), *ref_api_evidence],
        "residual_risk": list(args.get("residual_risk") or []),
    }
    metadata: dict[str, Any] = {}
    api_reason = str(args.get("api_evidence_not_applicable_reason") or "").strip()
    if api_reason:
        metadata["api_evidence_not_applicable"] = True
        metadata["api_evidence_not_applicable_reason"] = api_reason
    lsp_reason = str(args.get("lsp_evidence_not_applicable_reason") or "").strip()
    if lsp_reason:
        metadata["lsp_evidence_not_applicable"] = True
        metadata["lsp_evidence_not_applicable_reason"] = lsp_reason
    if metadata:
        gate_args["metadata"] = metadata
    result = _minion_review_gate_submit_result(
        CanonicalToolCall(name="op_minion_review_gate_submit", args=gate_args, call_id=call.call_id),
        workspace,
    )
    if not result.ok:
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=result.text,
            structured=result.structured,
            call_id=call.call_id,
            llm_text=result.llm_text,
            status=result.status,
        )
    return CanonicalToolResult(
        name=call.name,
        ok=True,
        text="minion checkpoint review gate recorded",
        structured=result.structured,
        call_id=call.call_id,
        llm_text="minion checkpoint review gate recorded",
        status=RuntimeStatus.OK,
    )


def _review_checkpoint_checks(raw: Any) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for item in list(raw or []):
        if not isinstance(item, dict):
            continue
        check = dict(item)
        if "output_summary" not in check:
            for alias in ("output summary", "outputSummary", "summary", "message"):
                if str(check.get(alias) or "").strip():
                    check["output_summary"] = str(check.get(alias) or "")
                    break
        checks.append(check)
    return checks


def _review_checkpoint_evidence_ref_checks(raw: Any, workspace: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    commands: list[dict[str, Any]] = []
    api_evidence: list[dict[str, Any]] = []
    projection = build_evidence_projection(workspace.get("review_tool_evidence_refs"))
    for item in list(raw or []):
        if not isinstance(item, dict):
            continue
        requested = dict(item)
        resolved = resolve_evidence_ref(requested, projection)
        requested_id = str(requested.get("evidence_id") or requested.get("id") or requested.get("call_id") or requested.get("evidence_ref_id") or "").strip()
        if not resolved:
            return [], [], f"unknown review evidence ref: {requested_id or '<empty>'}"
        entry = dict(requested)
        entry.pop("id", None)
        entry["evidence_id"] = str(resolved.get("id") or requested_id)
        for key in (
            "evidence_ref_id",
            "call_id",
            "ledger_id",
            "kind",
            "tool_name",
            "operation",
            "command",
            "cwd",
            "exit_code",
            "status",
            "summary",
            "path",
            "query",
            "method",
            "server_id",
            "workspace_root",
            "file",
            "file_sha256",
            "freshness",
            "unavailable_reason",
        ):
            if key not in entry and resolved.get(key) not in (None, "", []):
                entry[key] = resolved.get(key)
        kind = str(entry.get("kind") or "").strip()
        if kind == "command":
            commands.append(entry)
        elif kind in {"lsp", "source"}:
            api_evidence.append(entry)
        else:
            return [], [], f"review evidence ref {requested_id or resolved.get('id')} has unsupported kind: {kind or '<missing>'}"
    return commands, api_evidence, ""


def _bind_review_target_plan_ref(target: dict[str, Any], workspace: dict[str, Any]) -> str:
    original = workspace.get("review_target_plan_ref")
    if not isinstance(original, dict):
        return ""
    submitted = target.get("plan_ref")
    if not isinstance(submitted, dict):
        target["plan_ref"] = dict(original)
        return ""
    if not submitted:
        target["plan_ref"] = dict(original)
        return ""
    submitted_ref = dict(submitted)
    original_ref = dict(original)
    for key in ("path", "sha256", "plan_id", "task_id", "plan_revision"):
        submitted_value = submitted_ref.get(key)
        original_value = original_ref.get(key)
        if submitted_value in (None, "") or original_value in (None, ""):
            continue
        if str(submitted_value) != str(original_value):
            return (
                "plan_acceptance target.plan_ref must match the immutable review target; "
                f"{key} drifted from {original_value!r} to {submitted_value!r}. "
                "Do not mutate planner artifacts in place; request a plan revision instead."
            )
    submitted_key = plan_target_key(submitted_ref)
    original_key = plan_target_key(original_ref)
    if submitted_ref.get("sha256") and original_ref.get("sha256") and submitted_key != original_key:
        return (
            "plan_acceptance target.plan_ref must match the immutable review target. "
            "Do not submit a gate for a modified planner artifact; request a plan revision instead."
        )
    target["plan_ref"] = original_ref
    return ""


def _review_gate_validation_error_payload(error: str, args: dict[str, Any], workspace: dict[str, Any]) -> dict[str, Any]:
    criteria = _validation_acceptance_criteria(args, workspace)
    checklist = build_acceptance_checklist(criteria)
    return {
        "reason": "review_gate_validation_failed",
        "error": str(error or ""),
        "missing_acceptance_criteria": _missing_acceptance_criteria(args, criteria),
        "acceptance_checklist": compact_checklist(checklist),
        "accepted_cover_refs": [
            {"id": item.get("id"), "ref": str(index), "criterion": item.get("source_text")}
            for index, item in enumerate(checklist, start=1)
        ],
        "usable_evidence_refs": _usable_review_evidence_refs(workspace),
        "next_payload_shape": _review_gate_next_payload_shape(args, workspace, criteria),
    }


def _review_gate_validation_llm_text(error: str, payload: dict[str, Any]) -> str:
    lines = [f"review gate validation failed: {error}"]
    missing = [str(item) for item in list(payload.get("missing_acceptance_criteria") or []) if str(item).strip()]
    if missing:
        lines.append("missing_acceptance_criteria: " + "; ".join(missing[:5]))
    refs = list(payload.get("usable_evidence_refs") or [])
    if refs:
        rendered = []
        for item in refs[:8]:
            if not isinstance(item, dict):
                continue
            ref = str(item.get("id") or item.get("evidence_id") or item.get("evidence_ref_id") or item.get("call_id") or "").strip()
            kind = str(item.get("kind") or "").strip()
            summary = str(item.get("summary") or item.get("command") or item.get("path") or "").strip()
            rendered.append(f"{ref} ({kind}: {summary})".strip())
        if rendered:
            lines.append("usable_evidence_refs: " + "; ".join(rendered))
    lines.append("Fix only the named invalid fields and resubmit the gate.")
    return "\n".join(lines)


def _validation_acceptance_criteria(args: dict[str, Any], workspace: dict[str, Any]) -> list[str]:
    current_sources: list[Any] = [
        dict(args.get("target") or {}).get("acceptance_criteria"),
        workspace.get("review_target_acceptance_criteria"),
    ]
    criteria = _deduped_criteria(current_sources)
    if criteria:
        return criteria
    sources: list[Any] = []
    for contract in (dict(args.get("target") or {}).get("source_contract"), workspace.get("review_target_source_contract")):
        if isinstance(contract, dict):
            sources.append(contract.get("acceptance_criteria"))
    return _deduped_criteria(sources)


def _deduped_criteria(sources: list[Any]) -> list[str]:
    criteria: list[str] = []
    seen: set[str] = set()
    for source in sources:
        for item in _string_list(source):
            token = _loose_coverage_token(item)
            if token in seen:
                continue
            seen.add(token)
            criteria.append(item)
    return criteria


def _missing_acceptance_criteria(args: dict[str, Any], criteria: list[str]) -> list[str]:
    if str(args.get("verdict") or "").strip().lower() != "pass":
        return []
    if str(args.get("gate_kind") or "").strip() not in {"checkpoint_verification", "repair_verification"}:
        return []
    covered = _gate_coverage_refs(args)
    missing: list[str] = []
    for index, criterion in enumerate(criteria, start=1):
        aliases = {str(index), f"#{index}", f"ac{index}", f"acceptance_{index}", _loose_coverage_token(criterion)}
        if not any(alias in covered for alias in aliases):
            missing.append(criterion)
    return missing


def _gate_coverage_refs(args: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for section in ("evidence", "commands_run", "api_evidence"):
        for item in list(args.get(section) or []):
            if not isinstance(item, dict):
                continue
            for key in ("covers", "coverage", "acceptance_criteria", "acceptance_criteria_refs", "acceptance_refs"):
                refs.update(_coverage_refs_from_value(item.get(key)))
    return {item for item in refs if item}


def _coverage_refs_from_value(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (int, float)):
        return {str(int(value))}
    if isinstance(value, str):
        text = value.strip()
        return {text.lower(), _loose_coverage_token(text)}
    if isinstance(value, dict):
        refs: set[str] = set()
        for nested in value.values():
            refs.update(_coverage_refs_from_value(nested))
        return refs
    if isinstance(value, (list, tuple, set)):
        refs: set[str] = set()
        for nested in value:
            refs.update(_coverage_refs_from_value(nested))
        return refs
    return {_loose_coverage_token(value)}


def _usable_review_evidence_refs(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    return build_evidence_projection(workspace.get("review_tool_evidence_refs"))[-20:]


def _review_gate_next_payload_shape(args: dict[str, Any], workspace: dict[str, Any], criteria: list[str]) -> dict[str, Any]:
    checkpoint_target = bool(
        str(dict(args.get("target") or {}).get("checkpoint_id") or workspace.get("review_target_checkpoint_id") or "").strip()
    )
    checklist = build_acceptance_checklist(criteria)
    coverage = ["AC-1"] if checklist else []
    command_ref = next((item for item in _usable_review_evidence_refs(workspace) if item.get("kind") == "command"), {})
    api_ref = next((item for item in _usable_review_evidence_refs(workspace) if item.get("kind") in {"source", "lsp"}), {})
    if checkpoint_target:
        return {
            "tool": "review_checkpoint",
            "args": {
                "verdict": str(args.get("verdict") or "pass"),
                "summary": "<concise verdict summary>",
                "evidence_refs": [
                    {
                        "evidence_id": command_ref.get("id") or "<EV command evidence id>",
                        "covers": coverage or ["<acceptance ref>"],
                    },
                    {
                        "evidence_id": api_ref.get("id") or "<EV source/lsp evidence id>",
                        "covers": coverage or ["<acceptance ref>"],
                    },
                ],
                "lsp_evidence_not_applicable_reason": "<reason only when LSP is unavailable or irrelevant>",
            },
        }
    return {
        "tool": "review_gate_submit",
        "args": {
            "gate_kind": str(args.get("gate_kind") or "<gate_kind>"),
            "target": dict(args.get("target") or {}),
            "verdict": str(args.get("verdict") or "pass"),
            "summary": "<concise verdict summary>",
            "evidence": [{"kind": "review", "summary": "<review evidence summary>"}],
            "api_evidence": [{"call_id": api_ref.get("call_id") or "<source/lsp evidence call_id>"}],
        },
    }


def _with_review_tool_provenance(args: dict[str, Any], workspace: dict[str, Any], *, repository: MinionTaskingRepository) -> dict[str, Any]:
    workspace_refs = workspace.get("review_tool_evidence_refs")
    refs = [dict(item) for item in list(workspace_refs or []) if isinstance(item, dict)]
    metadata = dict(args.get("metadata") or {})
    criteria = _review_gate_acceptance_criteria(args, workspace, repository)
    _normalize_review_gate_coverage(args, criteria)
    if refs:
        refs = repository.record_review_tool_evidence_refs(
            refs,
            work_order_id=str(workspace.get("work_order_id") or ""),
            run_id=str(workspace.get("run_id") or ""),
            reviewer_profile=str(workspace.get("minion_profile") or ""),
        )
        if isinstance(workspace_refs, list):
            workspace_refs[:] = refs
            refs = workspace_refs
        else:
            workspace["review_tool_evidence_refs"] = refs
        _bind_command_evidence_refs(args, refs)
        _bind_api_evidence_refs(args, refs)
        _normalize_review_gate_coverage(args, criteria)
        metadata["tool_evidence_refs"] = refs
        _default_lsp_not_applicable_from_status(args, refs, metadata)
    _append_checkpoint_commit_evidence(args, workspace, repository, criteria=criteria)
    _normalize_review_gate_coverage(args, criteria)
    _default_lsp_not_applicable_from_turn(args, workspace, metadata)
    _default_lsp_not_applicable_from_workspace_setup(args, workspace, metadata)
    if metadata:
        args["metadata"] = metadata
    return args


def _bind_command_evidence_refs(args: dict[str, Any], refs: list[dict[str, Any]]) -> None:
    command_refs = [dict(item) for item in refs if str(item.get("kind") or "") == "command"]
    command_refs_by_command = {
        _normalize_command_text(item.get("command") or ""): dict(item)
        for item in command_refs
        if _normalize_command_text(item.get("command") or "")
    }
    command_refs_by_id = _review_refs_by_id(command_refs)
    commands: list[dict[str, Any]] = []
    changed = False
    for item in list(args.get("commands_run") or []):
        if not isinstance(item, dict):
            commands.append(item)
            continue
        entry = dict(item)
        command = _normalize_command_text(entry.get("command") or entry.get("cmd") or entry.get("name") or "")
        ref = _matching_review_ref(entry, command_refs_by_id) or command_refs_by_command.get(command, {})
        if ref:
            hydrated = _hydrate_command_evidence_entry(entry, ref)
            if hydrated != entry:
                entry = hydrated
                changed = True
        commands.append(entry)
    if changed:
        args["commands_run"] = commands


def _bind_api_evidence_refs(args: dict[str, Any], refs: list[dict[str, Any]]) -> None:
    api_refs = [dict(item) for item in refs if str(item.get("kind") or "") in {"lsp", "source"}]
    if not api_refs:
        return
    refs_by_id = _review_refs_by_id(api_refs)
    api_evidence: list[dict[str, Any]] = []
    changed = False
    for item in list(args.get("api_evidence") or []):
        if not isinstance(item, dict):
            api_evidence.append(item)
            continue
        entry = dict(item)
        ref = _matching_review_ref(entry, refs_by_id) or _matching_lsp_ref(entry, api_refs)
        if ref:
            hydrated = _hydrate_api_evidence_entry(entry, ref)
            if hydrated != entry:
                entry = hydrated
                changed = True
        api_evidence.append(entry)
    if changed:
        args["api_evidence"] = api_evidence


def _review_refs_by_id(refs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for ref in refs:
        for key in ("evidence_ref_id", "call_id", "tool_call_id", "ledger_id", "ref_id"):
            value = str(ref.get(key) or "").strip()
            if value:
                result[value] = dict(ref)
    return result


def _matching_review_ref(entry: dict[str, Any], refs_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    for key in ("evidence_ref_id", "evidence_ref", "ref_id", "call_id", "tool_call_id", "ledger_id"):
        value = str(entry.get(key) or "").strip()
        if value and value in refs_by_id:
            return dict(refs_by_id[value])
    return {}


def _hydrate_command_evidence_entry(entry: dict[str, Any], ref: dict[str, Any]) -> dict[str, Any]:
    hydrated = dict(entry)
    if str(ref.get("evidence_ref_id") or "").strip():
        hydrated["evidence_ref_id"] = str(ref.get("evidence_ref_id") or "").strip()
    command = str(ref.get("command") or "").strip()
    if command:
        hydrated["command"] = command
    if str(ref.get("cwd") or "").strip() and not str(hydrated.get("cwd") or "").strip():
        hydrated["cwd"] = str(ref.get("cwd") or "").strip()
    if ref.get("exit_code") is not None:
        hydrated["exit_code"] = ref.get("exit_code")
    if str(ref.get("status") or "").strip():
        hydrated["status"] = str(ref.get("status") or "").strip()
    if not str(hydrated.get("output_summary") or "").strip():
        summary = _review_ref_summary(ref)
        if summary:
            hydrated["output_summary"] = summary
    return hydrated


def _hydrate_api_evidence_entry(entry: dict[str, Any], ref: dict[str, Any]) -> dict[str, Any]:
    hydrated = dict(entry)
    kind = str(ref.get("kind") or "").strip()
    if kind:
        hydrated["kind"] = kind
    if str(ref.get("evidence_ref_id") or "").strip():
        hydrated["evidence_ref_id"] = str(ref.get("evidence_ref_id") or "").strip()
    for key in (
        "tool_name",
        "operation",
        "method",
        "evidence_id",
        "server_id",
        "workspace_root",
        "file",
        "file_sha256",
        "freshness",
        "path",
        "query",
        "status",
    ):
        if ref.get(key) not in (None, "", []):
            hydrated.setdefault(key, ref.get(key))
    if not str(hydrated.get("summary") or hydrated.get("output_summary") or "").strip():
        summary = _review_ref_summary(ref)
        if summary:
            hydrated["summary"] = summary
    return hydrated


def _review_ref_summary(ref: dict[str, Any]) -> str:
    for key in ("output_summary", "summary", "message", "stdout_preview", "stderr_preview"):
        text = str(ref.get(key) or "").strip()
        if text:
            return text
    return ""


def _review_gate_acceptance_criteria(
    args: dict[str, Any],
    workspace: dict[str, Any],
    repository: MinionTaskingRepository,
) -> list[str]:
    target = dict(args.get("target") or {})
    current_sources: list[Any] = [
        target.get("acceptance_criteria"),
        workspace.get("review_target_acceptance_criteria"),
    ]
    checkpoint = _review_checkpoint_payload(args, workspace, repository)
    if checkpoint:
        current_sources.append(checkpoint.get("acceptance_criteria"))
    criteria = _deduped_criteria(current_sources)
    if criteria:
        return criteria
    sources: list[Any] = []
    for contract in (target.get("source_contract"), workspace.get("review_target_source_contract")):
        if isinstance(contract, dict):
            sources.append(contract.get("acceptance_criteria"))
    if checkpoint:
        source_contract = checkpoint.get("source_contract")
        if isinstance(source_contract, dict):
            sources.append(source_contract.get("acceptance_criteria"))
    return _deduped_criteria(sources)


def _review_checkpoint_payload(
    args: dict[str, Any],
    workspace: dict[str, Any],
    repository: MinionTaskingRepository,
) -> dict[str, Any]:
    target = dict(args.get("target") or {})
    checkpoint_id = str(target.get("checkpoint_id") or workspace.get("review_target_checkpoint_id") or "").strip()
    if not checkpoint_id:
        return {}
    loaded = repository.load_checkpoint(checkpoint_id)
    if str(loaded.get("status") or "") != "ok":
        return {}
    checkpoint = loaded.get("checkpoint")
    return dict(checkpoint or {}) if isinstance(checkpoint, dict) else {}


def _normalize_review_gate_coverage(args: dict[str, Any], criteria: list[str]) -> None:
    if not criteria:
        return
    checklist = build_acceptance_checklist(criteria)
    for section in ("evidence", "commands_run", "api_evidence"):
        normalized_items: list[Any] = []
        changed = False
        for item in list(args.get(section) or []):
            if not isinstance(item, dict):
                normalized_items.append(item)
                continue
            entry = dict(item)
            for key in ("covers", "coverage", "acceptance_criteria", "acceptance_criteria_refs", "acceptance_refs"):
                if key not in entry:
                    continue
                normalized = normalize_coverage_refs(entry.get(key), checklist, legacy_index=True)
                if normalized != entry.get(key):
                    entry[key] = normalized
                    changed = True
            normalized_items.append(entry)
        if changed:
            args[section] = normalized_items


def _normalize_coverage_value(value: Any, criteria: list[str]) -> Any:
    if isinstance(value, (list, tuple, set)):
        return [_coverage_ref_for_value(item, criteria) for item in value]
    if isinstance(value, dict):
        return {str(key): _normalize_coverage_value(nested, criteria) for key, nested in value.items()}
    return _coverage_ref_for_value(value, criteria)


def _coverage_ref_for_value(value: Any, criteria: list[str]) -> Any:
    if isinstance(value, (int, float)):
        return str(int(value))
    text = str(value or "").strip()
    if not text:
        return text
    index_ref = _coverage_index_ref(text)
    if index_ref:
        return index_ref
    loose = _loose_coverage_token(text)
    for index, criterion in enumerate(criteria, start=1):
        if loose == _loose_coverage_token(criterion):
            return str(index)
    return text


def _coverage_index_ref(text: str) -> str:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return ""
    compact = normalized.replace("_", " ").replace("-", " ").lstrip("#")
    parts = compact.split()
    if len(parts) == 1:
        token = parts[0]
        if token.isdigit():
            return str(int(token))
        if token.startswith("ac") and token[2:].isdigit():
            return str(int(token[2:]))
    if parts and parts[-1].isdigit() and " ".join(parts[:-1]) in {"ac", "acceptance", "criterion", "criteria", "acceptance criterion"}:
        return str(int(parts[-1]))
    return ""


def _loose_coverage_token(value: Any) -> str:
    words = []
    for word in str(value or "").strip().lower().split():
        stripped = word.strip(" \t\r\n.,;:!?()[]{}'\"`")
        if stripped:
            words.append(stripped)
    return " ".join(words)


def _append_checkpoint_commit_evidence(
    args: dict[str, Any],
    workspace: dict[str, Any],
    repository: MinionTaskingRepository,
    *,
    criteria: list[str],
) -> None:
    if str(args.get("verdict") or "").strip().lower() != "pass":
        return
    if str(args.get("gate_kind") or "").strip() not in {"checkpoint_verification", "repair_verification"}:
        return
    covers = _checkpoint_commit_coverage_refs(criteria)
    if not covers:
        return
    api_evidence = [dict(item) if isinstance(item, dict) else item for item in list(args.get("api_evidence") or [])]
    if any(isinstance(item, dict) and _is_checkpoint_commit_evidence(item) for item in api_evidence):
        return
    target = dict(args.get("target") or {})
    checkpoint = _review_checkpoint_payload(args, workspace, repository)
    checkpoint_git = _checkpoint_git_payload(target, workspace, checkpoint)
    checkpoint_id = str(target.get("checkpoint_id") or workspace.get("review_target_checkpoint_id") or checkpoint.get("checkpoint_id") or "").strip()
    commit_sha = str(
        target.get("commit_sha")
        or workspace.get("review_target_commit_sha")
        or checkpoint.get("commit_sha")
        or checkpoint_git.get("commit_sha")
        or ""
    ).strip()
    if not checkpoint_id and not commit_sha:
        return
    entry: dict[str, Any] = {
        "kind": "checkpoint_commit",
        "source": "runtime",
        "status": "passed",
        "checkpoint_id": checkpoint_id,
        "commit_sha": commit_sha,
        "covers": covers,
        "summary": _checkpoint_commit_summary(checkpoint_id, commit_sha, checkpoint_git),
    }
    changed_files = _string_list(checkpoint_git.get("changed_files"))
    if changed_files:
        entry["changed_files"] = changed_files[:40]
    api_evidence.append({key: value for key, value in entry.items() if value not in ("", [], None)})
    args["api_evidence"] = api_evidence


def _checkpoint_commit_coverage_refs(criteria: list[str]) -> list[str]:
    refs: list[str] = []
    for index, criterion in enumerate(criteria, start=1):
        token = _loose_coverage_token(criterion)
        raw = str(criterion or "").strip().lower()
        if "op_minion_checkpoint_commit" in raw or "checkpoint" in token or ("git" in token and "commit" in token):
            refs.append(str(index))
    return refs


def _is_checkpoint_commit_evidence(item: dict[str, Any]) -> bool:
    kind = str(item.get("kind") or item.get("source") or item.get("evidence_kind") or "").strip().lower()
    return kind in {"checkpoint_commit", "checkpoint", "git_commit"}


def _checkpoint_git_payload(
    target: dict[str, Any],
    workspace: dict[str, Any],
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    for candidate in (
        target.get("checkpoint_git"),
        workspace.get("review_target_checkpoint_git"),
        checkpoint.get("checkpoint_git"),
        checkpoint.get("git_commit"),
    ):
        if isinstance(candidate, dict):
            return dict(candidate)
    return {}


def _checkpoint_commit_summary(checkpoint_id: str, commit_sha: str, checkpoint_git: dict[str, Any]) -> str:
    parts = ["Runtime recorded checkpoint commit evidence"]
    if checkpoint_id:
        parts.append(f"checkpoint_id={checkpoint_id}")
    if commit_sha:
        parts.append(f"commit_sha={commit_sha}")
    changed_files = _string_list(checkpoint_git.get("changed_files"))
    if changed_files:
        parts.append("changed_files=" + ", ".join(changed_files[:8]))
    return "; ".join(parts)


def _default_lsp_not_applicable_from_status(args: dict[str, Any], refs: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
    if str(args.get("verdict") or "").strip().lower() != "pass":
        return
    if _metadata_bool(metadata, "lsp_evidence_not_applicable"):
        return
    if _has_lsp_api_evidence(list(args.get("api_evidence") or [])):
        return
    for ref in refs:
        if str(ref.get("kind") or "") != "lsp":
            continue
        reason = str(ref.get("unavailable_reason") or "").strip()
        if not reason:
            continue
        metadata["lsp_evidence_not_applicable"] = True
        metadata["lsp_evidence_not_applicable_reason"] = reason
        return


def _default_lsp_not_applicable_from_turn(args: dict[str, Any], workspace: dict[str, Any], metadata: dict[str, Any]) -> None:
    if str(args.get("verdict") or "").strip().lower() != "pass":
        return
    if _metadata_bool(metadata, "lsp_evidence_not_applicable"):
        return
    if _has_lsp_api_evidence(list(args.get("api_evidence") or [])):
        return
    unavailable = workspace.get("lsp_unavailable_for_turn")
    if not isinstance(unavailable, dict):
        return
    reason = str(unavailable.get("reason") or "").strip()
    if not reason:
        return
    metadata["lsp_evidence_not_applicable"] = True
    metadata["lsp_evidence_not_applicable_reason"] = reason


def _default_lsp_not_applicable_from_workspace_setup(args: dict[str, Any], workspace: dict[str, Any], metadata: dict[str, Any]) -> None:
    if str(args.get("verdict") or "").strip().lower() != "pass":
        return
    if _metadata_bool(metadata, "lsp_evidence_not_applicable"):
        return
    if _has_lsp_api_evidence(list(args.get("api_evidence") or [])):
        return
    lsp_setup = workspace.get("lsp_setup")
    if not isinstance(lsp_setup, dict):
        return
    languages = _string_list(lsp_setup.get("languages") or workspace.get("languages"))
    servers = _string_list(lsp_setup.get("servers"))
    skipped = _string_list(lsp_setup.get("skipped"))
    if not languages or servers or not skipped:
        return
    metadata["lsp_evidence_not_applicable"] = True
    metadata["lsp_evidence_not_applicable_reason"] = (
        "workspace LSP setup did not prepare a language server for "
        + ", ".join(languages)
        + ": "
        + "; ".join(skipped[:3])
    )


def _metadata_bool(metadata: dict[str, Any], key: str) -> bool:
    value = dict(metadata or {}).get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _has_lsp_api_evidence(api_evidence: list[Any]) -> bool:
    for item in api_evidence:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or item.get("source") or item.get("evidence_kind") or "").strip().lower()
        method = str(item.get("method") or "").strip()
        evidence_id = str(item.get("evidence_id") or "").strip().lower()
        if kind == "lsp" or method.startswith("textDocument/") or method.startswith("callHierarchy/") or evidence_id.startswith("lsp_"):
            return True
    return False


def _matching_lsp_ref(entry: dict[str, Any], refs: list[dict[str, Any]]) -> dict[str, Any]:
    evidence_id = str(entry.get("evidence_id") or "").strip()
    method = str(entry.get("method") or "").strip()
    operation = str(entry.get("operation") or "").strip()
    for ref in refs:
        if evidence_id and evidence_id == str(ref.get("evidence_id") or "").strip():
            return dict(ref)
        if method and method == str(ref.get("method") or "").strip():
            return dict(ref)
        if operation and operation == str(ref.get("operation") or "").strip():
            return dict(ref)
    return {}


def _review_gate_provenance_error(args: dict[str, Any], workspace: dict[str, Any]) -> str:
    verdict = str(args.get("verdict") or "").strip().lower()
    if verdict != "pass":
        return ""
    shell_violations = [dict(item) for item in list(workspace.get("shell_mutation_violations") or []) if isinstance(item, dict)]
    if shell_violations:
        return "reviewer shell mutated the audited workspace; pass verdict is blocked until the review is rerun from a clean read-only workspace"
    evidence_refs = [dict(item) for item in list(workspace.get("review_tool_evidence_refs") or []) if isinstance(item, dict)]
    command_refs = [item for item in evidence_refs if str(item.get("kind") or "") == "command"]
    lsp_refs = [item for item in evidence_refs if str(item.get("kind") or "") == "lsp"]
    command_error = _command_evidence_provenance_error(list(args.get("commands_run") or []), command_refs)
    if command_error:
        return command_error
    lsp_error = _lsp_evidence_provenance_error(list(args.get("api_evidence") or []), lsp_refs)
    if lsp_error:
        return lsp_error
    return ""


def _command_evidence_provenance_error(commands_run: list[Any], command_refs: list[dict[str, Any]]) -> str:
    if not commands_run:
        return ""
    normalized_refs = {_normalize_command_text(item.get("command") or "") for item in command_refs if item.get("command")}
    for item in commands_run:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or item.get("result") or "").strip().lower()
        command = _normalize_command_text(item.get("command") or item.get("cmd") or item.get("name") or "")
        if status in {"skipped", "not_run", "blocked"}:
            continue
        if item.get("exit_code") is not None or status in {"passed", "pass", "ok", "failed", "fail"}:
            if command and command not in normalized_refs:
                return f"commands_run entry lacks matching shell evidence: {command}"
    return ""


def _lsp_evidence_provenance_error(api_evidence: list[Any], lsp_refs: list[dict[str, Any]]) -> str:
    if not api_evidence:
        return ""
    evidence_ids = {str(item.get("evidence_id") or "").strip() for item in lsp_refs if str(item.get("evidence_id") or "").strip()}
    methods = {str(item.get("method") or "").strip() for item in lsp_refs if str(item.get("method") or "").strip()}
    operations = {str(item.get("operation") or "").strip() for item in lsp_refs if str(item.get("operation") or "").strip()}
    for item in api_evidence:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or item.get("source") or item.get("evidence_kind") or "").strip().lower()
        method = str(item.get("method") or "").strip()
        evidence_id = str(item.get("evidence_id") or "").strip()
        operation = str(item.get("operation") or "").strip()
        is_lsp_claim = kind == "lsp" or method.startswith("textDocument/") or method.startswith("callHierarchy/") or evidence_id.startswith("lsp_")
        if not is_lsp_claim:
            continue
        if evidence_id and evidence_id in evidence_ids:
            continue
        if method and method in methods:
            continue
        if operation and operation in operations:
            continue
        return "api_evidence contains LSP evidence that lacks matching op_lsp_* tool provenance"
    return ""


def _normalize_command_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _minion_checkpoint_commit_result(call: CanonicalToolCall, workspace: dict[str, Any]) -> CanonicalToolResult:
    try:
        repo_path = str((workspace or {}).get("repo_path") or "").strip()
        if not repo_path:
            raise ValueError("current task repo is not available")
        repo = Path(repo_path)
        title = str(call.args.get("title") or workspace.get("current_milestone_title") or "").strip()
        result = commit_milestone(
            repo,
            work_order_id=str(workspace.get("work_order_id") or "work_order"),
            milestone_index=_coerce_int(workspace.get("current_milestone_index"), default=0),
            title=title,
        )
        status = str(result.get("status") or "").strip()
        if status == "no_changes":
            inspected = inspect_milestone_checkpoint(repo, base_sha=str(workspace.get("base_sha") or ""))
            if inspected.get("status") == "committed":
                repair_attempt = dict(workspace.get("current_repair_attempt") or {})
                failed_commit_sha = str(repair_attempt.get("failed_commit_sha") or "").strip()
                commit_sha = str(inspected.get("commit_sha") or "").strip()
                if failed_commit_sha and commit_sha == failed_commit_sha:
                    text = (
                        "Repair checkpoint is unchanged: create a new commit that addresses the reviewer finding "
                        f"before resubmitting failed commit {failed_commit_sha}."
                    )
                    return CanonicalToolResult(
                        name=call.name,
                        ok=False,
                        text=text,
                        structured={
                            **inspected,
                            "reason": "repair_reused_failed_checkpoint",
                            "repair_attempt": repair_attempt,
                        },
                        call_id=call.call_id,
                        llm_text=text,
                        status=RuntimeStatus.ERROR,
                    )
                payload = {**inspected, "already_committed": True}
                return CanonicalToolResult(
                    name=call.name,
                    ok=True,
                    text=f"Milestone checkpoint already committed: {payload.get('commit_sha')}",
                    structured=payload,
                    call_id=call.call_id,
                    llm_text=f"Milestone checkpoint already committed: {payload.get('commit_sha')}",
                    status=RuntimeStatus.OK,
                )
            return CanonicalToolResult(
                name=call.name,
                ok=False,
                text="No milestone changes to commit.",
                structured={**result, **inspected},
                call_id=call.call_id,
                llm_text="No milestone changes to commit.",
                status=RuntimeStatus.ERROR,
            )
        ok = status == "committed"
        text = (
            f"Milestone checkpoint committed: {result.get('commit_sha')}"
            if ok
            else str(result.get("error") or f"Milestone checkpoint commit failed: {status}")
        )
        return CanonicalToolResult(
            name=call.name,
            ok=ok,
            text=text,
            structured=result,
            call_id=call.call_id,
            llm_text=text,
            status=RuntimeStatus.OK if ok else RuntimeStatus.ERROR,
        )
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=message,
            structured={"error": message, "error_type": exc.__class__.__name__},
            call_id=call.call_id,
            llm_text=message,
            status=RuntimeStatus.ERROR,
        )


def _effective_capability_name(tool_call: CanonicalToolCall) -> str:
    if tool_call.name in {"op_tool_call", "call_tool"}:
        args = dict(tool_call.args or {})
        return str(args.get("name") or args.get("capability") or args.get("tool") or tool_call.name).strip()
    return tool_call.name


def _effective_tool_args(tool_call: CanonicalToolCall) -> dict[str, Any]:
    if tool_call.name in {"op_tool_call", "call_tool"} and isinstance((tool_call.args or {}).get("args"), dict):
        return dict((tool_call.args or {}).get("args") or {})
    return dict(tool_call.args or {})


def _review_tool_evidence_ref(target_name: str, tool_call: CanonicalToolCall, result: CanonicalToolResult) -> dict[str, Any]:
    normalized_target = str(target_name or "").strip()
    kind = _review_tool_evidence_kind(normalized_target)
    if not kind:
        return {}
    args = _effective_tool_args(tool_call)
    structured = result.structured if isinstance(result.structured, dict) else {}
    evidence = structured.get("evidence") if isinstance(structured.get("evidence"), dict) else {}
    ref = {
        "evidence_ref_id": f"tev_{uuid4().hex[:12]}",
        "kind": kind,
        "tool_name": normalized_target,
        "call_id": str(tool_call.call_id or ""),
        "ok": bool(result.ok),
        "status": str(result.status or ""),
        "summary": _preview_text(_tool_result_text(result), limit=240),
    }
    if kind == "command":
        ref["command"] = str(args.get("cmd") or args.get("command") or "").strip()
        ref["cwd"] = str(args.get("cwd") or args.get("workdir") or "")
        if isinstance(structured, dict):
            for key in ("exit_code", "stdout_preview", "stderr_preview"):
                if key in structured:
                    ref[key] = structured.get(key)
    if kind == "lsp":
        ref["operation"] = _lsp_operation_name(normalized_target)
        if ref["operation"] == "status":
            for key in ("attached_count", "server_count"):
                if structured.get(key) is not None:
                    ref[key] = structured.get(key)
            unavailable_reason = _lsp_status_unavailable_reason(structured)
            if unavailable_reason:
                ref["unavailable_reason"] = unavailable_reason
        if ref["operation"] == "diagnostics":
            unavailable_reason = _lsp_diagnostics_unavailable_reason(structured)
            if unavailable_reason:
                ref["unavailable_reason"] = unavailable_reason
        if evidence:
            for key in ("evidence_id", "method", "server_id", "workspace_root", "file", "file_sha256", "freshness"):
                if evidence.get(key) is not None:
                    ref[key] = evidence.get(key)
    if kind == "source":
        if "path" in args:
            ref["path"] = str(args.get("path") or "")
        if "query" in args:
            ref["query"] = str(args.get("query") or "")
    return {key: value for key, value in ref.items() if value not in ("", None)}


def _review_tool_evidence_kind(target_name: str) -> str:
    if target_name in {"op_exec_shell", "run_shell", "shell", "shell_exec"}:
        return "command"
    if _is_lsp_capability_name(target_name):
        return "lsp"
    if target_name in {"op_tree", "op_search", "op_file_read"}:
        return "source"
    return ""


def _lsp_status_unavailable_reason(structured: dict[str, Any]) -> str:
    attached = _coerce_int(structured.get("attached_count"), default=-1)
    if attached != 0:
        return ""
    server_count = _coerce_int(structured.get("server_count"), default=0)
    statuses: set[str] = set()
    for item in list(structured.get("servers") or []):
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or item.get("binary_status") or "").strip()
        if status:
            statuses.add(status)
    if statuses:
        rendered = ", ".join(sorted(statuses)[:4])
        return f"op_lsp_status reported no attached language server ({server_count} configured; statuses: {rendered})"
    return f"op_lsp_status reported no attached language server ({server_count} configured)"


def _lsp_diagnostics_unavailable_reason(structured: dict[str, Any]) -> str:
    result_payload = structured.get("result") if isinstance(structured.get("result"), dict) else {}
    evidence = structured.get("evidence") if isinstance(structured.get("evidence"), dict) else {}
    evidence_result = evidence.get("result") if isinstance(evidence.get("result"), dict) else {}
    diagnostics_state = str(result_payload.get("diagnostics_state") or evidence_result.get("diagnostics_state") or "").strip()
    if diagnostics_state != "timed_out":
        return ""
    file_text = str(evidence.get("file") or "").strip()
    server = structured.get("server") if isinstance(structured.get("server"), dict) else {}
    server_id = str(server.get("server_id") or evidence.get("server_id") or "").strip()
    reason = "op_lsp_diagnostics timed out"
    if file_text:
        reason += f" for {file_text}"
    if server_id:
        reason += f" on {server_id}"
    return reason


def _is_lsp_capability_name(name: str) -> bool:
    value = str(name or "").strip()
    return value.startswith("op_lsp_") or value.startswith("lsp_")


def _lsp_operation_name(name: str) -> str:
    value = str(name or "").strip()
    if value.startswith("op_lsp_"):
        return value.removeprefix("op_lsp_")
    return value.removeprefix("lsp_")

def _tool_result_text(result: CanonicalToolResult) -> str:
    return default_tool_result_text(result, fallback_ok="tool completed", fallback_error="tool failed")


def _preview_text(value: Any, *, limit: int = 400) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
