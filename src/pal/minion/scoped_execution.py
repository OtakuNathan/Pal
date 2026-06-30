from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import subprocess
from typing import Any
from uuid import uuid4

from pal.execution import CapabilityResult
from pal.execution.git_tool import GIT_TOOL_CMD_DESCRIPTION, GIT_TOOL_DESCRIPTION, classify_git_command
from pal.foundation import utc_now
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
from pal.minion.plan_builder import (
    PLAN_BUILDER_CAPABILITIES,
    PLAN_BUILDER_TOOL_SPECS,
    enrich_plan_review_gate_node_refs,
    normalize_gate_contract_payload,
    plan_builder_tool_result,
)
from pal.minion.profiles import filter_minion_allowed_capabilities, is_minion_capability_denied
from pal.minion.repair_bill_builder import (
    REPAIR_BILL_BUILDER_CAPABILITIES,
    REPAIR_BILL_BUILDER_TOOL_SPECS,
    repair_bill_builder_tool_result,
)
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
    "op_minion_gate_contract_submit",
    "op_tree",
    "op_search",
    "op_minion_artifact_write",
    "op_minion_artifact_edit",
    *PLAN_BUILDER_CAPABILITIES,
    *REPAIR_BILL_BUILDER_CAPABILITIES,
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
            "Use this first for structured directory listings under the current project repo; do not use op_exec_shell with ls/find/git ls-files "
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
            "Use this first for repository text search under the current project repo; do not use op_exec_shell with grep/rg/find text scans "
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
            + " The working directory is fixed to the current project repo or reviewed repo. "
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
    "op_minion_gate_contract_submit": {
        "name": "op_minion_gate_contract_submit",
        "description": (
            "Submit the pre-plan source contract as a normalized gate_contract for the original work order. "
            "Use this only for source_contract review targets before planner work starts. Include every hard user/work-order "
            "requirement as a gate check; use mechanical_check only for finite count/bound predicates."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "source_work_order_id": {"type": "string", "description": "Original planner work order id. Defaults to workspace binding."},
                "gate_contract": {
                    "type": "object",
                    "description": "Object with checks=[{claim, priority, kind, source_ref, rationale, mechanical_check?}]. Pal assigns refs gate:N.",
                },
                "checks": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Compatibility shortcut when gate_contract is omitted.",
                },
            },
            "required": ["summary"],
            "additionalProperties": False,
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
                    "description": "For checkpoint/repair pass verdicts, include at least one command/check entry. Prefer op_minion_review_checkpoint evidence_selectors for checkpoint reviews. If using commands_run directly, entries must describe real op_exec_shell/op_git evidence from this reviewer run and include covers=[acceptance index or exact criterion].",
                },
                "api_evidence": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Source/docs/LSP/build evidence for API and call-shape claims. Prefer op_minion_review_checkpoint evidence_selectors for checkpoint reviews. If using api_evidence directly, entries must describe real source/LSP evidence and include covers=[acceptance index or exact criterion]. LSP entries must match an op_lsp_* call from this reviewer run.",
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
                    "description": "Compatibility fallback for command/check evidence. Prefer evidence_selectors. If used, write semantic command intent and covers; Pal resolves matching recorded op_exec_shell/op_git evidence.",
                },
                "api_checks": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Compatibility fallback for source/docs/LSP/build evidence. Prefer evidence_selectors. If used, write semantic source/LSP intent and covers; Pal resolves matching recorded evidence.",
                },
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Legacy compact evidence references. Prefer evidence_selectors unless Pal explicitly returned a usable EV-* id in the current tool result.",
                },
                "evidence_selectors": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Preferred semantic evidence selectors. Examples: "
                        "{\"kind\":\"command\",\"contains\":\"pytest tests/test_catalog.py\",\"latest_success\":true,\"covers\":[\"AC-1\"]}; "
                        "{\"kind\":\"source\",\"path_contains\":\"src/pal_dogfood/catalog.py\",\"covers\":[\"AC-1\"]}; "
                        "{\"kind\":\"lsp\",\"operation\":\"diagnostics\",\"latest_success\":true,\"covers\":[\"AC-1\"]}. "
                        "Do not invent EV-* ids; Pal resolves selectors to recorded tool evidence."
                    ),
                },
                "residual_risk": {"type": "array", "items": {"type": "object"}},
                "api_evidence_not_applicable_reason": {"type": "string"},
                "lsp_evidence_not_applicable_reason": {"type": "string"},
                "warning_clean_not_applicable_reason": {"type": "string"},
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
WORKSPACE_TOOL_SPECS.update(REPAIR_BILL_BUILDER_TOOL_SPECS)

_REPAIR_EDIT_TOOL_NAMES = {
    "op_file_edit",
    "op_file_write",
    "op_path_delete",
}

_WORKSPACE_MUTATION_TOOL_NAMES = {
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

_WARNING_CLEAN_DEFAULT_LANGUAGES = (
    "c",
    "cpp",
    "objc",
    "objcpp",
    "python",
    "javascript",
    "typescript",
    "rust",
    "go",
    "java",
    "kotlin",
    "swift",
    "csharp",
    "ruby",
    "php",
    "shell",
)

_WARNING_CLEAN_LANGUAGE_ALIASES = {
    "bash": "shell",
    "c++": "cpp",
    "cc": "cpp",
    "cxx": "cpp",
    "c#": "csharp",
    "cs": "csharp",
    "golang": "go",
    "js": "javascript",
    "objc": "objc",
    "objective-c": "objc",
    "objective-c++": "objcpp",
    "objective-cpp": "objcpp",
    "py": "python",
    "rs": "rust",
    "sh": "shell",
    "ts": "typescript",
}

_WARNING_CLEAN_COMMAND_MARKERS = (
    "-werror",
    "/wx",
    "warnings=error",
    "warnings-as-errors",
    "warnings as errors",
    "pythonwarnings=error",
    "-w error",
    "-d warnings",
    "-dwarnings",
    "--deny warnings",
    "--max-warnings=0",
    "compileall",
    "py_compile",
    "tsc --noemit",
    "cargo clippy",
    "go vet",
    "shellcheck",
    "bash -n",
    "ruby -c",
    "php -l",
    "javac",
    "dotnet build",
    "swift build",
    "kotlinc",
    "warning-clean",
    "warning clean",
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
            if call.name == "op_minion_gate_contract_submit":
                text = "gate_contract_submit is only available for source_contract pre-plan review targets"
                structured = {"reason": "gate_contract_submit_target_mismatch", "required_gate_kind": "source_contract"}
            elif call.name == "op_minion_review_gate_submit" and str(self.workspace.get("review_target_gate_kind") or "").strip() == "source_contract":
                text = "source_contract reviewers must use gate_contract_submit; the generic review_gate_submit tool is hidden for this target"
                structured = {"reason": "use_gate_contract_submit_for_source_contract", "required_tool": "gate_contract_submit"}
            else:
                text = "checkpoint and repair reviewers must use review_checkpoint; the generic review_gate_submit tool is hidden for this target"
                structured = {"reason": "use_review_checkpoint_for_checkpoint_target", "required_tool": "review_checkpoint"}
            return CanonicalToolResult(
                name=call.name,
                ok=False,
                text=text,
                structured=structured,
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
        read_only_file_guard = _read_only_repo_file_mutation_guard(call, self.workspace)
        if read_only_file_guard is not None:
            return read_only_file_guard
        plan_review_shell_guard = _plan_review_plan_artifact_shell_guard(call, self.workspace)
        if plan_review_shell_guard is not None:
            return plan_review_shell_guard
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
            if call.name in REPAIR_BILL_BUILDER_CAPABILITIES:
                return await repair_bill_builder_tool_result(call, self.workspace, self.produced_artifacts)
            if call.name == "op_minion_memory_candidate_write":
                return _minion_memory_candidate_result(call, self.memory_l3)
            if call.name == "op_minion_gate_contract_submit":
                return _minion_gate_contract_submit_result(call, self.workspace)
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
        if canonical == "op_minion_gate_contract_submit":
            gate_kind = str(self.workspace.get("review_target_gate_kind") or "").strip()
            return gate_kind == "source_contract" or bool(str(self.workspace.get("pre_plan_contract_source_work_order_id") or "").strip())
        if canonical != "op_minion_review_gate_submit":
            return True
        gate_kind = str(self.workspace.get("review_target_gate_kind") or "").strip()
        if gate_kind == "source_contract":
            return False
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


def _read_only_repo_file_mutation_guard(call: CanonicalToolCall, workspace: dict[str, Any]) -> CanonicalToolResult | None:
    workspace_policy = dict((workspace or {}).get("workspace_policy") or {})
    if str(workspace_policy.get("mode") or "").strip().lower() != "read_only_repo":
        return None
    if call.name not in _WORKSPACE_MUTATION_TOOL_NAMES:
        return None
    text = (
        "read_only_repo reviewers cannot mutate files with file_edit, file_write, or delete_path. "
        "Run verification commands in the target repo and submit blocking findings for the repair loop instead."
    )
    return CanonicalToolResult(
        name=call.name,
        ok=False,
        text=text,
        structured={
            "reason": "read_only_repo_file_mutation_forbidden",
            "capability": call.name,
            "repo_path": str((workspace or {}).get("repo_path") or ""),
        },
        call_id=call.call_id,
        llm_text=text,
        status=RuntimeStatus.FORBIDDEN,
    )


def _plan_review_plan_artifact_shell_guard(call: CanonicalToolCall, workspace: dict[str, Any]) -> CanonicalToolResult | None:
    if call.name not in {"op_exec_shell", "run_shell", "shell", "shell_exec"}:
        return None
    if str(workspace.get("review_target_gate_kind") or "").strip() != "plan_acceptance":
        return None
    args = dict(call.args or {})
    command = " ".join(str(args.get("cmd") or args.get("command") or "").strip().split())
    if not command:
        return None
    if not _plan_review_command_reads_plan_artifact(command, workspace):
        return None
    text = (
        "Plan review route: inspect submitted plans with plan_read, plan_validate, plan_find, and plan_get. "
        "Do not use run_shell to cat/grep/probe plan.draft.json or plan.json. "
        "If those plan tools already prove a blocker or a pass, submit review_gate_submit next."
    )
    return CanonicalToolResult(
        name=call.name,
        ok=False,
        text=text,
        structured={
            "reason": "plan_review_requires_plan_tools",
            "required_tools": ["plan_read", "plan_validate", "plan_find", "plan_get"],
            "submit_tool": "review_gate_submit",
            "blocked_command": command,
        },
        call_id=call.call_id,
        llm_text=text,
        status=RuntimeStatus.INVALID,
    )


def _plan_review_command_reads_plan_artifact(command: str, workspace: dict[str, Any]) -> bool:
    lowered = command.lower()
    artifact_markers = {"plan.draft.json", "plan.json"}
    plan_ref = workspace.get("review_target_plan_ref")
    if isinstance(plan_ref, dict):
        for key in ("path", "relative_path"):
            value = str(plan_ref.get(key) or "").strip()
            if value:
                artifact_markers.add(value.lower())
                artifact_markers.add(Path(value).name.lower())
    artifact_dir = str(workspace.get("artifact_dir") or "").strip()
    if artifact_dir:
        artifact_markers.add(artifact_dir.lower())
    if not any(marker and marker in lowered for marker in artifact_markers):
        return False
    plan_reader_commands = (
        "cat",
        "grep",
        "rg",
        "sed",
        "awk",
        "head",
        "tail",
        "less",
        "more",
        "jq",
        "python",
        "python3",
        "node",
        "ruby",
        "perl",
    )
    return any(f" {name}" in f" {lowered}" for name in plan_reader_commands)


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
        text = "read_only_repo minions may use git for inspection only; submit a blocking finding or use a dedicated repair workspace for mutations"
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
            "review workspace has no .git directory; use review_target.checkpoint_git for checkpoint commit metadata, "
            "changed files, and stats instead of running git in this workspace"
        )
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=text,
            structured={
                "reason": "review_scratch_git_unavailable",
                "workspace_root": str(root),
                "checkpoint_git": dict(checkpoint_git or {}) if isinstance(checkpoint_git, dict) else {},
                "hint": "Use review_target.checkpoint_git for commit_sha, changed_files, parent_commit_sha, and stat; use file/search/read tools for source inspection.",
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
    if str(workspace_policy.get("mode") or "").strip().lower() == "read_only_repo":
        keys = ("repo_path", "review_scratch_dir", "review_scratch_repo_path")
    else:
        keys = ("repo_path", "task_repo_path", "target_repo_path")
    for key in keys:
        raw = str((workspace or {}).get(key) or "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
    return None


def _effective_lsp_workspace_root(workspace: dict[str, Any]) -> Path | None:
    for key in ("repo_path", "review_scratch_repo_path"):
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


def _minion_gate_contract_submit_result(call: CanonicalToolCall, workspace: dict[str, Any]) -> CanonicalToolResult:
    runtime_root = str(workspace.get("runtime_root") or "").strip()
    if not runtime_root:
        text = "runtime_root is missing from minion workspace"
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=text,
            structured={"reason": "runtime_root_missing"},
            call_id=call.call_id,
            llm_text=text,
            status=RuntimeStatus.ERROR,
        )
    try:
        args = dict(call.args or {})
        source_work_order_id = str(
            args.get("source_work_order_id")
            or workspace.get("pre_plan_contract_source_work_order_id")
            or workspace.get("review_source_work_order_id")
            or workspace.get("work_order_id")
            or ""
        ).strip()
        if not source_work_order_id:
            raise ValueError("source_work_order_id is required")
        contract_input: Any = args.get("gate_contract")
        if contract_input is None:
            contract_input = {"checks": list(args.get("checks") or [])}
        gate_contract = normalize_gate_contract_payload(contract_input)
        active_checks = [
            dict(item)
            for item in list(gate_contract.get("checks") or [])
            if isinstance(item, dict) and not bool(item.get("deleted"))
        ]
        if not active_checks:
            raise ValueError("gate_contract requires at least one active check")
        summary = str(args.get("summary") or "").strip() or f"compiled {len(active_checks)} source contract checks"
        repository = MinionTaskingRepository(runtime_root=Path(runtime_root))
        state = {
            "status": "compiled",
            "summary": summary,
            "gate_contract": gate_contract,
            "compiler_work_order_id": str(workspace.get("work_order_id") or ""),
            "compiler_run_id": str(workspace.get("run_id") or ""),
            "compiler_minion_id": str(workspace.get("minion_id") or ""),
            "updated_at": utc_now(),
        }
        repository.merge_work_order_metadata(
            source_work_order_id,
            {
                "gate_contract": gate_contract,
                "pre_plan_contract": state,
            },
        )
        event = {
            "event_kind": "pre_plan_contract_compiled",
            "minion_id": str(workspace.get("minion_id") or ""),
            "run_id": str(workspace.get("run_id") or ""),
            "work_order_id": source_work_order_id,
            "minion_profile": str(workspace.get("minion_profile") or ""),
            "payload": {
                "status": "compiled",
                "summary": summary,
                "gate_contract": gate_contract,
                "compiler_work_order_id": state["compiler_work_order_id"],
                "compiler_run_id": state["compiler_run_id"],
            },
            "created_at": utc_now(),
        }
        repository.record_minion_event(event)
        return CanonicalToolResult(
            name=call.name,
            ok=True,
            text="pre-plan gate contract recorded",
            structured={"status": "compiled", "work_order_id": source_work_order_id, "gate_contract": gate_contract},
            call_id=call.call_id,
            llm_text="pre-plan gate contract recorded; finish now unless you need to correct the submitted contract",
            status=RuntimeStatus.OK,
        )
    except ValueError as exc:
        message = str(exc)
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=message,
            structured={"reason": "gate_contract_invalid", "error": message},
            call_id=call.call_id,
            llm_text=f"gate contract invalid: {message}. Fix only the invalid fields and resubmit.",
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
        args = enrich_plan_review_gate_node_refs(args, workspace)
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
        warning_clean_error = _review_gate_warning_clean_error(args, workspace)
        if warning_clean_error:
            structured = _review_gate_validation_error_payload(warning_clean_error, args, workspace)
            return CanonicalToolResult(
                name=call.name,
                ok=False,
                text=warning_clean_error,
                structured=structured,
                call_id=call.call_id,
                llm_text=_review_gate_validation_llm_text(warning_clean_error, structured),
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
    selector_commands, selector_api_evidence, selector_error = _review_checkpoint_selector_checks(args.get("evidence_selectors"), workspace)
    target = {
        "checkpoint_id": str(args.get("checkpoint_id") or workspace.get("review_target_checkpoint_id") or ""),
        "commit_sha": str(args.get("commit_sha") or workspace.get("review_target_commit_sha") or ""),
        "run_id": str(workspace.get("review_target_run_id") or ""),
    }
    manual_commands = _review_checkpoint_checks(args.get("checks"))
    manual_api_evidence = _review_checkpoint_checks(args.get("api_checks"))
    if _has_review_checkpoint_evidence_refs(args.get("evidence_refs")):
        commands_run = ref_commands if ref_commands else manual_commands
        api_evidence = ref_api_evidence if ref_api_evidence else manual_api_evidence
    else:
        commands_run = [*manual_commands, *ref_commands]
        api_evidence = [*manual_api_evidence, *ref_api_evidence]
    if selector_commands:
        commands_run = [*commands_run, *selector_commands]
    if selector_api_evidence:
        api_evidence = [*api_evidence, *selector_api_evidence]
    evidence_error = selector_error or ref_error
    if evidence_error and not (commands_run or api_evidence):
        structured = {
            "reason": "review_checkpoint_evidence_ref_invalid",
            "error": evidence_error,
            "usable_evidence_refs": _usable_review_evidence_refs(workspace),
            "next_payload_shape": _review_gate_next_payload_shape({}, workspace, _string_list(workspace.get("review_target_acceptance_criteria"))),
        }
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=evidence_error,
            structured=structured,
            call_id=call.call_id,
            llm_text=_review_gate_validation_llm_text(evidence_error, structured),
            status=RuntimeStatus.INVALID,
        )
    gate_args: dict[str, Any] = {
        "gate_kind": gate_kind,
        "target": target,
        "verdict": str(args.get("verdict") or "").strip(),
        "summary": str(args.get("summary") or "").strip(),
        "findings": list(args.get("findings") or []),
        "required_fixes": list(args.get("required_fixes") or []),
        "commands_run": commands_run,
        "api_evidence": api_evidence,
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
    warning_clean_reason = str(args.get("warning_clean_not_applicable_reason") or "").strip()
    if warning_clean_reason:
        metadata["warning_clean_not_applicable"] = True
        metadata["warning_clean_not_applicable_reason"] = warning_clean_reason
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


def _has_review_checkpoint_evidence_refs(raw: Any) -> bool:
    if isinstance(raw, dict):
        return bool(raw)
    if isinstance(raw, str):
        return bool(raw.strip())
    try:
        items = list(raw or [])
    except TypeError:
        return bool(str(raw or "").strip())
    return any(bool(item) for item in items)


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
        if _is_command_review_evidence_kind(kind):
            commands.append(entry)
        elif kind in {"lsp", "source"}:
            api_evidence.append(entry)
        else:
            return [], [], f"review evidence ref {requested_id or resolved.get('id')} has unsupported kind: {kind or '<missing>'}"
    return commands, api_evidence, ""


def _review_checkpoint_selector_checks(raw: Any, workspace: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    commands: list[dict[str, Any]] = []
    api_evidence: list[dict[str, Any]] = []
    projection = build_evidence_projection(workspace.get("review_tool_evidence_refs"))
    for item in list(raw or []):
        if isinstance(item, str):
            requested: dict[str, Any] = {"contains": item}
        elif isinstance(item, dict):
            requested = dict(item)
        else:
            continue
        resolved = _match_review_evidence_selector(requested, projection)
        if not resolved:
            description = _selector_description(requested)
            return [], [], f"no review evidence matched selector: {description or '<empty>'}"
        entry = _review_checkpoint_entry_from_resolved_evidence(requested, resolved)
        kind = str(entry.get("kind") or "").strip()
        if _is_command_review_evidence_kind(kind):
            commands.append(entry)
        elif kind in {"lsp", "source"}:
            api_evidence.append(entry)
        else:
            return [], [], f"review evidence selector matched unsupported kind: {kind or '<missing>'}"
    return commands, api_evidence, ""


def _review_checkpoint_entry_from_resolved_evidence(requested: dict[str, Any], resolved: dict[str, Any]) -> dict[str, Any]:
    entry = dict(requested)
    for key in (
        "id",
        "contains",
        "query",
        "command_contains",
        "path_contains",
        "summary_contains",
        "latest_success",
    ):
        entry.pop(key, None)
    entry["evidence_id"] = str(resolved.get("id") or resolved.get("evidence_id") or "")
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
        "stdout_preview",
        "stderr_preview",
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
    return {key: value for key, value in entry.items() if value not in ("", None, [])}


def _match_review_evidence_selector(selector: dict[str, Any], projection: list[dict[str, Any]]) -> dict[str, Any]:
    requested_kind = _selector_kind(selector)
    candidates = [dict(item) for item in projection if _selector_kind_matches(requested_kind, str(item.get("kind") or ""))]
    if not candidates:
        return {}
    if _selector_bool(selector, "latest_success"):
        candidates = [item for item in candidates if _review_evidence_ref_success(item)]
        if not candidates:
            return {}
    exact = resolve_evidence_ref(selector, candidates)
    if exact:
        return exact
    query = _selector_query(selector)
    best: dict[str, Any] = {}
    best_score = 0
    for index, candidate in enumerate(candidates):
        score = _review_evidence_selector_score(selector, candidate, query)
        if score <= 0:
            continue
        score = score * 1000 + index
        if score > best_score:
            best_score = score
            best = dict(candidate)
    if best and best_score >= 5000:
        return best
    if not query and len(candidates) == 1:
        return candidates[0]
    return {}


def _selector_kind(selector: dict[str, Any]) -> str:
    raw = str(
        selector.get("kind")
        or selector.get("evidence_kind")
        or selector.get("type")
        or selector.get("tool_kind")
        or ""
    ).strip().lower()
    if raw in {"shell", "test", "tests", "pytest", "build", "command_check"}:
        return "command"
    if raw in {"git", "op_git"}:
        return "git"
    if raw in {"diagnostics", "language_server", "typecheck", "type_check"}:
        return "lsp"
    if raw in {"file", "read", "code", "doc", "docs"}:
        return "source"
    return raw


def _selector_kind_matches(requested: str, actual: str) -> bool:
    actual = str(actual or "").strip().lower()
    requested = str(requested or "").strip().lower()
    if not requested:
        return actual in {"command", "git", "source", "lsp"}
    if requested == actual:
        return True
    if requested == "command":
        return _is_command_review_evidence_kind(actual)
    if requested == "api":
        return actual in {"source", "lsp"}
    return False


def _is_command_review_evidence_kind(kind: str) -> bool:
    return str(kind or "").strip().lower() in {"command", "git"}


def _selector_bool(selector: dict[str, Any], key: str) -> bool:
    value = selector.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _selector_query(selector: dict[str, Any]) -> str:
    for key in ("contains", "query", "command_contains", "path_contains", "summary_contains", "tool_name", "operation", "method"):
        text = str(selector.get(key) or "").strip()
        if text:
            return text
    return ""


def _review_evidence_selector_score(selector: dict[str, Any], candidate: dict[str, Any], query: str) -> int:
    score = 0
    for selector_key, candidate_keys in (
        ("tool_name", ("tool_name",)),
        ("operation", ("operation", "method")),
        ("method", ("method", "operation")),
        ("path_contains", ("path", "file", "summary", "query")),
        ("command_contains", ("command",)),
        ("summary_contains", ("summary", "command", "path", "query")),
    ):
        expected = str(selector.get(selector_key) or "").strip()
        if not expected:
            continue
        actual = " ".join(str(candidate.get(key) or "") for key in candidate_keys)
        if _text_contains_semantically(actual, expected):
            score += 25
        else:
            return 0
    if query:
        haystack = " ".join(
            str(candidate.get(key) or "")
            for key in (
                "summary",
                "command",
                "stdout_preview",
                "stderr_preview",
                "path",
                "query",
                "operation",
                "method",
                "tool_name",
                "file",
            )
        )
        if _text_contains_semantically(haystack, query):
            score += 20
        else:
            overlap = _token_overlap_score(query, haystack)
            if overlap <= 0:
                return 0
            score += overlap
    if _review_evidence_ref_success(candidate):
        score += 5
    return score


def _text_contains_semantically(haystack: Any, needle: Any) -> bool:
    normalized_haystack = _normalize_semantic_text(haystack)
    normalized_needle = _normalize_semantic_text(needle)
    if not normalized_haystack or not normalized_needle:
        return False
    if normalized_needle in normalized_haystack or normalized_haystack in normalized_needle:
        return True
    return _token_overlap_score(normalized_needle, normalized_haystack) >= 8


def _token_overlap_score(query: Any, text: Any) -> int:
    query_tokens = _semantic_tokens(query)
    text_tokens = _semantic_tokens(text)
    if not query_tokens or not text_tokens:
        return 0
    overlap = query_tokens & text_tokens
    score = len(overlap)
    if {"pytest", "test"} & query_tokens and {"pytest", "test"} & text_tokens:
        score += 4
    path_tokens = {token for token in query_tokens if "/" in token or token.endswith(".py")}
    score += len(path_tokens & text_tokens) * 3
    return score


def _semantic_tokens(value: Any) -> set[str]:
    text = _normalize_semantic_text(value)
    return {token for token in re.split(r"[^a-z0-9_./+-]+", text) if token and token not in {"cd", "and", "or", "the"}}


def _normalize_semantic_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace('"', " ").replace("'", " ").split())


def _selector_description(selector: dict[str, Any]) -> str:
    parts = []
    for key in ("kind", "contains", "query", "command_contains", "path_contains", "summary_contains", "tool_name", "operation", "method"):
        text = str(selector.get(key) or "").strip()
        if text:
            parts.append(f"{key}={text}")
    return ", ".join(parts)


def _review_evidence_ref_success(ref: dict[str, Any]) -> bool:
    if ref.get("ok") is True:
        return True
    if ref.get("exit_code") == 0:
        return True
    status = str(ref.get("status") or "").strip().lower()
    return status in {"ok", "pass", "passed", "success", "succeeded"}


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
    payload = {
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
    warning_clean = _missing_warning_clean_verification(args, workspace)
    if warning_clean:
        payload["missing_warning_clean_verification"] = warning_clean
    return payload


def _missing_warning_clean_verification(args: dict[str, Any], workspace: dict[str, Any]) -> dict[str, Any]:
    requirement = _warning_clean_requirement(args, workspace)
    if not requirement:
        return {}
    metadata = dict(args.get("metadata") or {})
    if _warning_clean_not_applicable_reason(metadata):
        return {}
    if _has_warning_clean_command_evidence(list(args.get("commands_run") or []), requirement["languages"]):
        return {}
    policy = dict(requirement.get("policy") or {})
    return {
        "languages": list(requirement["languages"]),
        "gate_scope": str(requirement.get("scope") or ""),
        "not_applicable_reason_field": str(policy.get("not_applicable_reason_field") or "warning_clean_not_applicable_reason"),
    }


def _review_gate_validation_llm_text(error: str, payload: dict[str, Any]) -> str:
    reason = str(payload.get("reason") or "").strip()
    label = "review checkpoint evidence selection failed" if reason == "review_checkpoint_evidence_ref_invalid" else "review gate validation failed"
    lines = [f"{label}: {error}"]
    missing = [str(item) for item in list(payload.get("missing_acceptance_criteria") or []) if str(item).strip()]
    if missing:
        lines.append("missing_acceptance_criteria: " + "; ".join(missing[:5]))
    warning_clean = payload.get("missing_warning_clean_verification")
    if isinstance(warning_clean, dict) and warning_clean:
        languages = ", ".join(str(item) for item in list(warning_clean.get("languages") or []) if str(item).strip())
        reason_field = str(warning_clean.get("not_applicable_reason_field") or "warning_clean_not_applicable_reason")
        lines.append(
            "missing_warning_clean_verification: "
            + (f"languages={languages}; " if languages else "")
            + f"run/select warning-clean command evidence or provide {reason_field}"
        )
    missing_refs = [
        str(item.get("id") or item.get("ref") or "").strip()
        for item in list((payload.get("next_payload_shape") or {}).get("missing_cover_refs") or [])
        if isinstance(item, dict) and str(item.get("id") or item.get("ref") or "").strip()
    ]
    if missing_refs:
        lines.append("missing_cover_refs: " + ", ".join(missing_refs[:10]))
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
    next_shape = payload.get("next_payload_shape")
    if isinstance(next_shape, dict) and next_shape:
        lines.append("next_payload_shape:\n```json\n" + _json_preview(next_shape, limit=2400) + "\n```")
        if isinstance(warning_clean, dict) and warning_clean:
            lines.append(
                "Run a warning-clean verification command if no listed command evidence already proves it, or resubmit "
                "with the concrete warning-clean not-applicable reason."
            )
        else:
            lines.append(
                "Resubmit the review gate using this shape now; preserve already-valid evidence and add the missing covers. "
                "Do not run more tools unless no listed evidence can prove a missing acceptance criterion."
            )
    else:
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
    return [str(item.get("criterion") or "") for item in _missing_acceptance_items(args, criteria)]


def _missing_acceptance_items(args: dict[str, Any], criteria: list[str]) -> list[dict[str, str]]:
    if str(args.get("verdict") or "").strip().lower() != "pass":
        return []
    if str(args.get("gate_kind") or "").strip() not in {"checkpoint_verification", "repair_verification"}:
        return []
    covered = _gate_coverage_refs(args)
    missing: list[dict[str, str]] = []
    for index, criterion in enumerate(criteria, start=1):
        if not _acceptance_criterion_covered(index, criterion, covered):
            missing.append({"id": f"AC-{index}", "ref": str(index), "criterion": criterion})
    return missing


def _acceptance_criterion_covered(index: int, criterion: str, covered: set[str]) -> bool:
    aliases = {
        str(index),
        f"#{index}",
        f"ac{index}",
        f"ac-{index}",
        f"acceptance_{index}",
        f"acceptance-{index}",
        _loose_coverage_token(criterion),
    }
    return any(alias in covered for alias in aliases)


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
    missing_cover_refs = _missing_acceptance_items(args, criteria)
    coverage = [str(item.get("id") or "") for item in missing_cover_refs if str(item.get("id") or "").strip()]
    if not coverage and checklist:
        coverage = [str(item.get("id") or f"AC-{index}") for index, item in enumerate(checklist, start=1)]
    missing_warning_clean = _missing_warning_clean_verification(args, workspace)
    warning_clean_selector: dict[str, Any] = {}
    if missing_warning_clean:
        languages = [str(item) for item in list(missing_warning_clean.get("languages") or []) if str(item).strip()]
        warning_ref = _latest_successful_warning_clean_ref(_usable_review_evidence_refs(workspace), languages)
        warning_clean_selector = {
            "kind": str(warning_ref.get("kind") or "command"),
            "contains": _selector_hint_for_ref(warning_ref)
            or "<warning-clean command substring, e.g. -Werror build or PYTHONWARNINGS=error pytest>",
            "latest_success": True,
            "covers": coverage or ["<acceptance ref>"],
        }
    command_ref = next(
        (item for item in _usable_review_evidence_refs(workspace) if _is_command_review_evidence_kind(str(item.get("kind") or ""))),
        {},
    )
    api_ref = next((item for item in _usable_review_evidence_refs(workspace) if item.get("kind") in {"source", "lsp"}), {})
    if checkpoint_target:
        evidence_selectors = [
            {
                "kind": str(command_ref.get("kind") or "command"),
                "contains": _selector_hint_for_ref(command_ref) or "<pytest/build command substring>",
                "latest_success": True,
                "covers": coverage or ["<acceptance ref>"],
            },
            {
                "kind": str(api_ref.get("kind") or "source"),
                "contains": _selector_hint_for_ref(api_ref) or "<source path or LSP operation>",
                "latest_success": True,
                "covers": coverage or ["<acceptance ref>"],
            },
        ]
        if warning_clean_selector:
            evidence_selectors.insert(0, warning_clean_selector)
        payload_args = {
            "verdict": str(args.get("verdict") or "pass"),
            "summary": "<concise verdict summary>",
            "evidence_selectors": evidence_selectors,
            "lsp_evidence_not_applicable_reason": "<reason only when LSP is unavailable or irrelevant>",
        }
        if missing_warning_clean:
            payload_args[str(missing_warning_clean.get("not_applicable_reason_field") or "warning_clean_not_applicable_reason")] = (
                "<reason only when warning-clean verification is impossible or explicitly out of scope>"
            )
        return {
            "tool": "review_checkpoint",
            "missing_cover_refs": missing_cover_refs,
            "args": payload_args,
        }
    return {
        "tool": "review_gate_submit",
        "missing_cover_refs": missing_cover_refs,
        "args": {
            "gate_kind": str(args.get("gate_kind") or "<gate_kind>"),
            "target": dict(args.get("target") or {}),
            "verdict": str(args.get("verdict") or "pass"),
            "summary": "<concise verdict summary>",
            "evidence": [{"kind": "review", "summary": "<review evidence summary>"}],
            "api_evidence": [{"call_id": api_ref.get("call_id") or "<source/lsp evidence call_id>"}],
        },
    }


def _selector_hint_for_ref(ref: dict[str, Any]) -> str:
    for key in ("command", "path", "file", "operation", "method", "summary", "query"):
        text = str(ref.get(key) or "").strip()
        if text:
            return text[:160]
    return ""


def _json_preview(value: Any, *, limit: int) -> str:
    try:
        text = json.dumps(value, ensure_ascii=True, indent=2, default=str)
    except TypeError:
        text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


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
        _default_review_checkpoint_runtime_evidence(args, refs, criteria)
        _default_warning_clean_runtime_evidence(args, refs, workspace, criteria)
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
    command_refs = [dict(item) for item in refs if _is_command_review_evidence_kind(str(item.get("kind") or ""))]
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
        ref = (
            _matching_review_ref(entry, command_refs_by_id)
            or command_refs_by_command.get(command, {})
            or _matching_semantic_command_ref(entry, command_refs)
        )
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
        ref = _matching_review_ref(entry, refs_by_id) or _matching_lsp_ref(entry, api_refs) or _matching_semantic_api_ref(entry, api_refs)
        if ref:
            hydrated = _hydrate_api_evidence_entry(entry, ref)
            if hydrated != entry:
                entry = hydrated
                changed = True
        api_evidence.append(entry)
    if changed:
        args["api_evidence"] = api_evidence


def _default_review_checkpoint_runtime_evidence(args: dict[str, Any], refs: list[dict[str, Any]], criteria: list[str]) -> None:
    if str(args.get("verdict") or "").strip().lower() != "pass":
        return
    if str(args.get("gate_kind") or "").strip() not in {"checkpoint_verification", "repair_verification"}:
        return
    covers = _all_acceptance_coverage_refs(criteria)
    commands = [dict(item) if isinstance(item, dict) else item for item in list(args.get("commands_run") or [])]
    api_evidence = [dict(item) if isinstance(item, dict) else item for item in list(args.get("api_evidence") or [])]
    command_refs = [dict(item) for item in refs if _is_command_review_evidence_kind(str(item.get("kind") or ""))]
    api_refs = [dict(item) for item in refs if str(item.get("kind") or "") in {"source", "lsp"}]
    if not _has_backed_command_evidence(commands):
        command_ref = _latest_successful_ref(command_refs) or (command_refs[-1] if command_refs else {})
        if command_ref:
            commands.append(_runtime_evidence_entry(command_ref, covers=covers))
    if not _has_source_api_evidence(api_evidence):
        source_ref = _latest_successful_ref([item for item in api_refs if str(item.get("kind") or "") == "source"])
        if source_ref:
            api_evidence.append(_runtime_evidence_entry(source_ref, covers=covers))
    if not _has_lsp_api_evidence(api_evidence):
        lsp_ref = _latest_successful_ref([item for item in api_refs if str(item.get("kind") or "") == "lsp"])
        if lsp_ref:
            api_evidence.append(_runtime_evidence_entry(lsp_ref, covers=covers))
    if commands:
        args["commands_run"] = commands
    if api_evidence:
        args["api_evidence"] = api_evidence


def _default_warning_clean_runtime_evidence(
    args: dict[str, Any],
    refs: list[dict[str, Any]],
    workspace: dict[str, Any],
    criteria: list[str],
) -> None:
    requirement = _warning_clean_requirement(args, workspace)
    if not requirement:
        return
    if _warning_clean_not_applicable_reason(dict(args.get("metadata") or {})):
        return
    commands = [dict(item) if isinstance(item, dict) else item for item in list(args.get("commands_run") or [])]
    if _has_warning_clean_command_evidence(commands, requirement["languages"]):
        return
    command_ref = _latest_successful_warning_clean_ref(
        [dict(item) for item in refs if _is_command_review_evidence_kind(str(item.get("kind") or ""))],
        requirement["languages"],
    )
    if not command_ref:
        return
    commands.append(_runtime_evidence_entry(command_ref, covers=_all_acceptance_coverage_refs(criteria)))
    args["commands_run"] = commands


def _review_gate_warning_clean_error(args: dict[str, Any], workspace: dict[str, Any]) -> str:
    requirement = _warning_clean_requirement(args, workspace)
    if not requirement:
        return ""
    metadata = dict(args.get("metadata") or {})
    if _warning_clean_not_applicable_reason(metadata):
        return ""
    if _has_warning_clean_command_evidence(list(args.get("commands_run") or []), requirement["languages"]):
        return ""
    return (
        "checkpoint pass review gate lacks warning-clean verification evidence for "
        + ", ".join(requirement["languages"])
        + "; run/select a successful compile/static/test diagnostic with warnings treated as failures, "
        + "or provide warning_clean_not_applicable_reason"
    )


def _warning_clean_requirement(args: dict[str, Any], workspace: dict[str, Any]) -> dict[str, Any]:
    if str(args.get("verdict") or "").strip().lower() != "pass":
        return {}
    if str(args.get("gate_kind") or "").strip() not in {"checkpoint_verification", "repair_verification"}:
        return {}
    policy = _warning_clean_policy(workspace)
    if not _warning_clean_policy_enabled(policy):
        return {}
    scope = _review_target_gate_scope(workspace)
    required_scopes = _string_list(policy.get("required_gate_scopes") or policy.get("gate_scopes"))
    if required_scopes:
        if not scope or scope not in set(required_scopes):
            return {}
    languages = _warning_clean_applicable_languages(workspace, policy)
    if not languages:
        return {}
    return {"languages": languages, "policy": policy, "scope": scope}


def _warning_clean_policy(workspace: dict[str, Any]) -> dict[str, Any]:
    gate_policy = dict(workspace.get("gate_policy") or workspace.get("effective_gate_policy") or {})
    raw = gate_policy.get("warning_clean_verification")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, bool):
        return {"enabled": raw, "required_for_checkpoint_pass": raw}
    if bool(gate_policy.get("require_warning_clean_verification_for_checkpoint_pass")):
        return {
            "enabled": True,
            "required_for_checkpoint_pass": True,
            "required_gate_scopes": _string_list(gate_policy.get("warning_clean_required_gate_scopes")),
        }
    return {}


def _warning_clean_policy_enabled(policy: dict[str, Any]) -> bool:
    if not policy:
        return False
    if policy.get("enabled") is False or str(policy.get("enabled") or "").strip().lower() in {"false", "0", "no", "off"}:
        return False
    value = policy.get("required_for_checkpoint_pass")
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip():
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(policy.get("enabled"))


def _review_target_gate_scope(workspace: dict[str, Any]) -> str:
    spec = workspace.get("review_target_gate_spec")
    if isinstance(spec, dict):
        for key in ("gate", "id", "name"):
            value = str(spec.get(key) or "").strip()
            if value:
                return value
    target = workspace.get("review_target")
    if isinstance(target, dict):
        spec = target.get("gate_spec")
        if isinstance(spec, dict):
            value = str(spec.get("gate") or spec.get("id") or spec.get("name") or "").strip()
            if value:
                return value
    return ""


def _warning_clean_applicable_languages(workspace: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    allowed = {
        _normalize_warning_clean_language(item)
        for item in (_string_list(policy.get("languages")) or list(_WARNING_CLEAN_DEFAULT_LANGUAGES))
    }
    allowed.discard("")
    detected = _workspace_warning_clean_languages(workspace)
    if "*" in allowed:
        return detected
    return [language for language in detected if language in allowed]


def _workspace_warning_clean_languages(workspace: dict[str, Any]) -> list[str]:
    languages: list[str] = []

    def add(value: Any) -> None:
        for item in _string_list(value):
            normalized = _normalize_warning_clean_language(item)
            if normalized and normalized not in languages:
                languages.append(normalized)

    add(workspace.get("languages"))
    lsp_setup = workspace.get("lsp_setup")
    if isinstance(lsp_setup, dict):
        add(lsp_setup.get("languages"))
    for key in ("review_target_source_contract", "review_target_module_contract"):
        contract = workspace.get(key)
        if not isinstance(contract, dict):
            continue
        add(contract.get("languages") or contract.get("language") or contract.get("implementation_languages"))
        metadata = contract.get("metadata")
        if isinstance(metadata, dict):
            add(metadata.get("languages") or metadata.get("language") or metadata.get("primary_language"))
    return languages


def _normalize_warning_clean_language(value: Any) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    if not text:
        return ""
    return _WARNING_CLEAN_LANGUAGE_ALIASES.get(text) or "".join(ch for ch in text if ch.isalnum() or ch in {"-", "+", "#", "."})


def _warning_clean_not_applicable_reason(metadata: dict[str, Any]) -> str:
    for key in (
        "warning_clean_not_applicable_reason",
        "warning_clean_verification_not_applicable_reason",
        "warning_clean_evidence_not_applicable_reason",
    ):
        reason = str(metadata.get(key) or "").strip()
        if reason:
            return reason
    if _metadata_bool(metadata, "warning_clean_not_applicable"):
        return str(metadata.get("warning_clean_reason") or "").strip()
    return ""


def _latest_successful_warning_clean_ref(refs: list[dict[str, Any]], languages: list[str]) -> dict[str, Any]:
    for ref in reversed(refs):
        if _review_evidence_ref_success(ref) and _is_warning_clean_command_evidence(ref, languages):
            return dict(ref)
    return {}


def _has_warning_clean_command_evidence(items: list[Any], languages: list[str]) -> bool:
    for item in items:
        if isinstance(item, dict) and _is_warning_clean_command_evidence(item, languages):
            return True
    return False


def _is_warning_clean_command_evidence(item: dict[str, Any], languages: list[str]) -> bool:
    if not _review_evidence_ref_success(item):
        return False
    text = _warning_clean_evidence_text(item)
    if not text:
        return False
    if re.search(r"\b0 warnings?\b", text) or re.search(r"\bno warnings?\b", text):
        return True
    for marker in _WARNING_CLEAN_COMMAND_MARKERS:
        if marker and marker in text:
            return True
    if any(language in {"typescript", "javascript"} for language in languages) and "tsc" in text and "noemit" in text:
        return True
    if any(language in {"c", "cpp", "objc", "objcpp"} for language in languages) and "werror" in text:
        return True
    if "python" in languages and ("pythonwarnings=error" in text or "-w error" in text):
        return True
    return False


def _warning_clean_evidence_text(item: dict[str, Any]) -> str:
    parts = [
        item.get("command"),
        item.get("cmd"),
        item.get("name"),
        item.get("summary"),
        item.get("output_summary"),
        item.get("stdout_preview"),
        item.get("stderr_preview"),
    ]
    normalized = _normalize_semantic_text(" ".join(str(part or "") for part in parts))
    return normalized.replace(" --no emit", " --noemit").replace("--no emit", "--noemit")


def _has_backed_command_evidence(items: list[Any]) -> bool:
    for item in items:
        if not isinstance(item, dict):
            continue
        command = str(item.get("command") or item.get("cmd") or item.get("name") or "").strip()
        if command and (item.get("exit_code") is not None or str(item.get("status") or "").strip()):
            return True
    return False


def _has_source_api_evidence(items: list[Any]) -> bool:
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or item.get("source") or item.get("evidence_kind") or "").strip().lower()
        if kind == "source":
            return True
    return False


def _all_acceptance_coverage_refs(criteria: list[str]) -> list[str]:
    return [str(index) for index, _criterion in enumerate(criteria, start=1)]


def _runtime_evidence_entry(ref: dict[str, Any], *, covers: list[str]) -> dict[str, Any]:
    entry = _review_checkpoint_entry_from_resolved_evidence({"covers": covers}, ref)
    if covers and not entry.get("covers"):
        entry["covers"] = list(covers)
    if not str(entry.get("summary") or entry.get("output_summary") or "").strip():
        summary = _review_ref_summary(ref)
        if summary:
            if str(entry.get("kind") or "") == "command":
                entry["output_summary"] = summary
            else:
                entry["summary"] = summary
    return entry


def _latest_successful_ref(refs: list[dict[str, Any]]) -> dict[str, Any]:
    for ref in reversed(refs):
        if _review_evidence_ref_success(ref):
            return dict(ref)
    return {}


def _matching_semantic_command_ref(entry: dict[str, Any], refs: list[dict[str, Any]]) -> dict[str, Any]:
    if not refs:
        return {}
    selector = dict(entry)
    command = str(entry.get("command") or entry.get("cmd") or entry.get("name") or "").strip()
    if command:
        selector.setdefault("kind", "command")
        selector.setdefault("command_contains", command)
        selector.setdefault("latest_success", _entry_claims_success(entry))
        matched = _match_review_evidence_selector(selector, build_evidence_projection(refs))
        if matched:
            return _source_ref_for_projection_match(matched, refs)
    if _entry_claims_success(entry):
        latest = _latest_successful_ref(refs)
        if latest and _looks_like_test_or_build_command(command or latest.get("command") or latest.get("summary")):
            return latest
    return {}


def _matching_semantic_api_ref(entry: dict[str, Any], refs: list[dict[str, Any]]) -> dict[str, Any]:
    if not refs:
        return {}
    selector = dict(entry)
    source_ref = str(entry.get("source_ref") or entry.get("path") or entry.get("file") or "").strip()
    if source_ref:
        selector.setdefault("path_contains", source_ref.split(":", 1)[0])
    if not _selector_kind(selector):
        selector.setdefault("kind", "api")
    matched = _match_review_evidence_selector(selector, build_evidence_projection(refs))
    if matched:
        return _source_ref_for_projection_match(matched, refs)
    return {}


def _source_ref_for_projection_match(match: dict[str, Any], refs: list[dict[str, Any]]) -> dict[str, Any]:
    keys = {
        str(match.get("evidence_ref_id") or "").strip(),
        str(match.get("call_id") or "").strip(),
        str(match.get("ledger_id") or "").strip(),
        str(match.get("evidence_id") or "").strip(),
    }
    for ref in refs:
        ref_keys = {
            str(ref.get("evidence_ref_id") or "").strip(),
            str(ref.get("call_id") or "").strip(),
            str(ref.get("ledger_id") or "").strip(),
            str(ref.get("evidence_id") or "").strip(),
        }
        if any(key and key in ref_keys for key in keys):
            return dict(ref)
    return dict(match)


def _entry_claims_success(entry: dict[str, Any]) -> bool:
    if entry.get("exit_code") == 0:
        return True
    status = str(entry.get("status") or entry.get("result") or "").strip().lower()
    return status in {"ok", "pass", "passed", "success", "succeeded"}


def _looks_like_test_or_build_command(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return any(token in text for token in ("pytest", " test", "tests/", "mypy", "pyright", "ruff", "cargo test", "npm test", "build"))


def _review_refs_by_id(refs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for ref in refs:
        for key in ("id", "evidence_id", "evidence_ref_id", "call_id", "tool_call_id", "ledger_id", "ref_id"):
            value = str(ref.get(key) or "").strip()
            if value:
                result[value] = dict(ref)
    return result


def _matching_review_ref(entry: dict[str, Any], refs_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    for key in ("id", "evidence_id", "evidence_ref_id", "evidence_ref", "ref_id", "call_id", "tool_call_id", "ledger_id"):
        value = str(entry.get(key) or "").strip()
        if value and value in refs_by_id:
            return dict(refs_by_id[value])
    return {}


def _hydrate_command_evidence_entry(entry: dict[str, Any], ref: dict[str, Any]) -> dict[str, Any]:
    hydrated = dict(entry)
    if str(ref.get("evidence_ref_id") or "").strip():
        hydrated["evidence_ref_id"] = str(ref.get("evidence_ref_id") or "").strip()
    if str(ref.get("call_id") or "").strip():
        hydrated["call_id"] = str(ref.get("call_id") or "").strip()
    if str(ref.get("ledger_id") or "").strip():
        hydrated["ledger_id"] = str(ref.get("ledger_id") or "").strip()
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
    if str(ref.get("call_id") or "").strip():
        hydrated["call_id"] = str(ref.get("call_id") or "").strip()
    if str(ref.get("ledger_id") or "").strip():
        hydrated["ledger_id"] = str(ref.get("ledger_id") or "").strip()
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
    gate_kind = str(args.get("gate_kind") or "").strip()
    shell_violations = [dict(item) for item in list(workspace.get("shell_mutation_violations") or []) if isinstance(item, dict)]
    if shell_violations:
        return "reviewer shell mutated the audited workspace; pass verdict is blocked until the review is rerun from a clean read-only workspace"
    evidence_refs = [dict(item) for item in list(workspace.get("review_tool_evidence_refs") or []) if isinstance(item, dict)]
    command_refs = [item for item in evidence_refs if _is_command_review_evidence_kind(str(item.get("kind") or ""))]
    lsp_refs = [item for item in evidence_refs if str(item.get("kind") or "") == "lsp"]
    if gate_kind in {"checkpoint_verification", "repair_verification"}:
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
                return f"commands_run entry lacks matching command evidence: {command}"
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
            raise ValueError("current project repo is not available")
        repo = Path(repo_path)
        copy_violations = _cross_module_source_copy_violations(repo, workspace)
        if copy_violations:
            text = (
                "Checkpoint blocked: changed files duplicate a dependency module contract/source file. "
                "Import/include the declared shared contract instead of copying it into this module."
            )
            return CanonicalToolResult(
                name=call.name,
                ok=False,
                text=text,
                structured={
                    "status": "blocked",
                    "reason": "cross_module_source_copy_violation",
                    "violations": copy_violations,
                },
                call_id=call.call_id,
                llm_text=text,
                status=RuntimeStatus.ERROR,
            )
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


def _cross_module_source_copy_violations(repo: Path, workspace: dict[str, Any]) -> list[dict[str, Any]]:
    changed_files = _git_changed_files(repo)
    if not changed_files:
        return []
    source_refs = _dependency_source_refs(workspace)
    if not source_refs:
        return []
    current_owned_area = _current_module_owned_area(workspace)
    violations: list[dict[str, Any]] = []
    for changed_path in changed_files:
        normalized_changed = _normalize_repo_path(changed_path)
        if not normalized_changed:
            continue
        if current_owned_area and not _path_matches_owned_area(normalized_changed, current_owned_area):
            continue
        changed_file = repo / normalized_changed
        if not changed_file.is_file():
            continue
        try:
            changed_bytes = changed_file.read_bytes()
        except OSError:
            continue
        if not changed_bytes:
            continue
        for ref in source_refs:
            if str(ref.get("copy_policy") or "import_only").strip() == "copy_allowed":
                continue
            source_path = _normalize_repo_path(ref.get("source_path"))
            if not source_path or source_path == normalized_changed:
                continue
            source_file = repo / source_path
            if not source_file.is_file():
                continue
            try:
                source_bytes = source_file.read_bytes()
            except OSError:
                continue
            if changed_bytes != source_bytes:
                continue
            violations.append(
                {
                    "changed_path": normalized_changed,
                    "source_path": source_path,
                    "producer_module_id": str(ref.get("producer_module_id") or ref.get("module_id") or ""),
                    "interface": str(ref.get("name") or ref.get("interface") or ""),
                    "import_path": str(ref.get("import_path") or ref.get("public_entrypoint") or ""),
                    "copy_policy": str(ref.get("copy_policy") or "import_only"),
                    "summary": "changed file is a byte-for-byte copy of a dependency-owned contract/source file",
                }
            )
            break
    return violations


def _git_changed_files(repo: Path) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "-uall", "--", "."],
            cwd=str(repo),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return []
    if completed.returncode != 0:
        return []
    result: list[str] = []
    for line in completed.stdout.splitlines():
        if not line:
            continue
        path = line[3:].strip() if len(line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        normalized = _normalize_repo_path(path)
        if normalized:
            result.append(normalized)
    return result


def _dependency_source_refs(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []

    def add_contract(contract: Any, *, producer_module_id: str = "", producer_owned_area: list[str] | None = None) -> None:
        if not isinstance(contract, dict):
            return
        item = dict(contract)
        if producer_module_id and not str(item.get("producer_module_id") or item.get("module_id") or "").strip():
            item["producer_module_id"] = producer_module_id
        if producer_owned_area and not list(item.get("producer_owned_area") or []):
            item["producer_owned_area"] = list(producer_owned_area)
        source_path = _normalize_repo_path(item.get("source_path"))
        if source_path:
            item["source_path"] = source_path
            refs.append(item)
            return
        for owned_path in list(item.get("producer_owned_area") or []):
            normalized = _normalize_repo_path(owned_path)
            if _looks_like_contract_source_path(normalized):
                refs.append({**item, "source_path": normalized, "copy_policy": str(item.get("copy_policy") or "import_only")})

    def add_dependency(dep: Any) -> None:
        if not isinstance(dep, dict):
            return
        producer = str(dep.get("module_id") or dep.get("producer_module_id") or "").strip()
        owned_area = [_normalize_repo_path(item) for item in list(dep.get("owned_area") or []) if _normalize_repo_path(item)]
        for interface in list(dep.get("provided_interfaces") or []):
            add_contract(interface, producer_module_id=producer, producer_owned_area=owned_area)
        for owned_path in owned_area:
            if _looks_like_contract_source_path(owned_path):
                add_contract({"source_path": owned_path, "producer_module_id": producer, "copy_policy": "import_only"})

    prompt_view = workspace.get("prompt_view")
    if isinstance(prompt_view, dict):
        for contract in list(prompt_view.get("relevant_contracts") or []):
            add_contract(contract)
        module = prompt_view.get("module")
        if isinstance(module, dict):
            for dep in list(module.get("dependency_context") or []):
                add_dependency(dep)
    coder_work_order = workspace.get("coder_work_order")
    if isinstance(coder_work_order, dict):
        for contract in list(coder_work_order.get("relevant_contracts") or []):
            add_contract(contract)
        metadata = coder_work_order.get("metadata")
        if isinstance(metadata, dict):
            for dep in list(metadata.get("module_dependency_context") or []):
                add_dependency(dep)
    for dep in list(workspace.get("module_dependency_context") or []):
        add_dependency(dep)
    return _dedupe_source_refs(refs)


def _current_module_owned_area(workspace: dict[str, Any]) -> list[str]:
    prompt_view = workspace.get("prompt_view")
    if isinstance(prompt_view, dict):
        module = prompt_view.get("module")
        if isinstance(module, dict):
            owned = [_normalize_repo_path(item) for item in list(module.get("owned_area") or []) if _normalize_repo_path(item)]
            if owned:
                return owned
    coder_work_order = workspace.get("coder_work_order")
    if isinstance(coder_work_order, dict):
        owned = [_normalize_repo_path(item) for item in list(coder_work_order.get("owned_area") or []) if _normalize_repo_path(item)]
        if owned:
            return owned
    return [_normalize_repo_path(item) for item in list(workspace.get("owned_area") or []) if _normalize_repo_path(item)]


def _path_matches_owned_area(path: str, owned_area: list[str]) -> bool:
    normalized = _normalize_repo_path(path)
    for raw_area in owned_area:
        area = _normalize_repo_path(raw_area)
        if not area:
            continue
        if normalized == area or normalized.startswith(area.rstrip("/") + "/"):
            return True
    return False


def _looks_like_contract_source_path(path: str) -> bool:
    normalized = _normalize_repo_path(path)
    if not normalized or normalized.endswith("/__init__.py"):
        return False
    name = normalized.rsplit("/", 1)[-1].lower()
    stem = name.rsplit(".", 1)[0]
    if stem in {"contract", "contracts", "schema", "schemas", "types", "dto", "dtos", "protocol", "protocols", "interface", "interfaces", "api"}:
        return True
    return name.endswith((".h", ".hh", ".hpp", ".hxx", ".pyi", ".d.ts"))


def _normalize_repo_path(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/").strip("/")


def _dedupe_source_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        source_path = _normalize_repo_path(ref.get("source_path"))
        if not source_path:
            continue
        key = (source_path, str(ref.get("producer_module_id") or ref.get("module_id") or ""))
        if key in seen:
            continue
        seen.add(key)
        item = dict(ref)
        item["source_path"] = source_path
        result.append(item)
    return result


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
    if _is_command_review_evidence_kind(kind):
        command = str(args.get("cmd") or args.get("command") or "").strip()
        if kind == "git" and command and not command.lstrip().startswith("git "):
            command = f"git {command}"
        ref["command"] = command
        ref["cwd"] = str(args.get("cwd") or args.get("workdir") or structured.get("cwd") or "")
        if isinstance(structured, dict):
            if kind == "git" and "returncode" in structured:
                ref["exit_code"] = structured.get("returncode")
            for key in ("exit_code", "stdout_preview", "stderr_preview"):
                if key in structured:
                    ref[key] = structured.get(key)
            if kind == "git":
                for source_key, target_key in (("stdout", "stdout_preview"), ("stderr", "stderr_preview")):
                    if str(structured.get(source_key) or "").strip():
                        ref[target_key] = _preview_text(str(structured.get(source_key) or ""), limit=2000)
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
    if target_name in {"op_git", "git"}:
        return "git"
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
