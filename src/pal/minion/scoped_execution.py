from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pal.execution import CapabilityResult
from pal.execution.tool_search import ToolReadTool, ToolSearchTool
from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.memory import L3CommitRequest
from pal.minion.git_env import commit_milestone, inspect_milestone_checkpoint
from pal.minion.profiles import filter_minion_allowed_capabilities, is_minion_capability_denied
from pal.minion.repository import MinionTaskingRepository
from pal.minion.utils import coerce_int as _coerce_int
from pal.minion.workspace_tools import _append_unique_artifact, _workspace_tool_result
from pal.plugins.l3 import MockL3Plugin
from pal.shared import RuntimeStatus, default_tool_result_text

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
    "op_exec_shell",
    "op_minion_checkpoint_commit",
    "op_workspace_tree",
    "op_workspace_search",
    "op_workspace_read",
    "op_minion_artifact_write",
    "op_minion_artifact_edit",
    "op_minion_memory_candidate_write",
    "op_web_search",
    "op_web_read",
    "op_memory_recall",
    *MINION_CODE_INTEL_TOOL_SURFACE,
)


WORKSPACE_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_workspace_tree": {
        "name": "op_workspace_tree",
        "description": "List files under the minion workspace repo_path without modifying anything.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative directory path. Use '.' for the repo root; do not use '/'."},
                "max_depth": {"type": "integer", "default": 2},
                "limit": {"type": "integer", "default": 200},
            },
        },
    },
    "op_workspace_search": {
        "name": "op_workspace_search",
        "description": "Search text files under the minion workspace repo_path without modifying anything.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string", "description": "Workspace-relative directory path. Use '.' for the repo root; do not use '/'."},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["query"],
        },
    },
    "op_workspace_read": {
        "name": "op_workspace_read",
        "description": "Read a workspace-relative text file without modifying anything. This is for inspection only; if you intend to edit the file with op_file_edit, call op_file_read on the absolute file path first so file_edit has the required file state.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path. Use paths like 'src/app.py', not absolute paths."},
                "start_line": {"type": "integer", "default": 1},
                "limit_lines": {"type": "integer", "default": 200},
            },
            "required": ["path"],
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
            "actual op_exec_shell or op_lsp_* calls in this reviewer run."
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
                    "description": "For checkpoint/repair pass verdicts, include at least one command/check entry with command, cwd, status or exit_code, output summary, and covers=[exact acceptance criteria or refs]. Non-skipped command entries must match an op_exec_shell call from this reviewer run.",
                },
                "api_evidence": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Source/docs/LSP/build evidence for API and call-shape claims. Pass verdicts require api_evidence or metadata.api_evidence_not_applicable=true with a reason. Include covers=[exact acceptance criteria or refs] when evidence proves milestone acceptance. LSP entries must match an op_lsp_* call from this reviewer run.",
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


@dataclass
class MinionScopedExecutionRuntime:
    base_runtime: Any
    allowed_capabilities: list[str]
    workspace: dict[str, Any] = field(default_factory=dict)
    produced_artifacts: list[dict[str, Any]] = field(default_factory=list)
    memory_l3: MockL3Plugin | None = None

    def __post_init__(self) -> None:
        self.allowed_capabilities = filter_minion_allowed_capabilities(self.allowed_capabilities)

    def list_capability_specs(self) -> list[dict[str, Any]]:
        allowed = set(self.allowed_capabilities)
        specs = []
        for name, spec in WORKSPACE_TOOL_SPECS.items():
            if name in allowed and not is_minion_capability_denied(name):
                specs.append(dict(spec))
        list_specs = getattr(self.base_runtime, "list_capability_specs", None)
        if callable(list_specs):
            for spec in list(list_specs()):
                name = str(spec.get("name") or "").strip()
                if name in allowed and not is_minion_capability_denied(name):
                    specs.append(spec)
        return specs

    def get_capability_spec(self, name: str) -> dict[str, Any] | None:
        if name in WORKSPACE_TOOL_SPECS:
            if name not in set(self.allowed_capabilities) or is_minion_capability_denied(name):
                return None
            return dict(WORKSPACE_TOOL_SPECS[name])
        get_spec = getattr(self.base_runtime, "get_capability_spec", None)
        if not callable(get_spec):
            return None
        spec = get_spec(name)
        if spec is None:
            return None
        canonical = str(spec.get("name") or spec.get("canonical_path") or name).strip()
        if canonical not in set(self.allowed_capabilities) or is_minion_capability_denied(canonical):
            return None
        return spec

    async def execute_tool_async(
        self,
        call: CanonicalToolCall,
        *,
        allow_tools: bool = True,
        turn_id: str | None = None,
    ) -> CanonicalToolResult:
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
            if call.name == "op_minion_memory_candidate_write":
                return _minion_memory_candidate_result(call, self.memory_l3)
            if call.name == "op_minion_review_gate_submit":
                return _minion_review_gate_submit_result(call, self.workspace)
            if call.name == "op_minion_checkpoint_commit":
                return _minion_checkpoint_commit_result(call, self.workspace)
            result = _workspace_tool_result(call, self.workspace)
            if call.name in {"op_minion_artifact_write", "op_minion_artifact_edit"} and result.ok:
                artifact = dict((result.structured or {}).get("artifact") or result.structured or {})
                if artifact:
                    _append_unique_artifact(self.produced_artifacts, artifact)
            return result
        return await self.base_runtime.execute_tool_async(call, allow_tools=allow_tools, turn_id=turn_id)

    async def _execute_scoped_tool_call(
        self,
        call: CanonicalToolCall,
        *,
        allow_tools: bool = True,
        turn_id: str | None = None,
    ) -> CanonicalToolResult:
        target_name = str(call.args.get("name") or call.args.get("capability") or call.args.get("tool") or "").strip()
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
        canonical = str((spec or {}).get("name") or (spec or {}).get("canonical_path") or target_name).strip()
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
        if not isinstance(target.get("plan_ref"), dict) and isinstance(workspace.get("review_target_plan_ref"), dict):
            target["plan_ref"] = dict(workspace.get("review_target_plan_ref") or {})
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
            work_order_id=str(workspace.get("work_order_id") or ""),
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


def _with_review_tool_provenance(args: dict[str, Any], workspace: dict[str, Any], *, repository: MinionTaskingRepository) -> dict[str, Any]:
    refs = [dict(item) for item in list(workspace.get("review_tool_evidence_refs") or []) if isinstance(item, dict)]
    if not refs:
        return args
    refs = repository.record_review_tool_evidence_refs(
        refs,
        work_order_id=str(workspace.get("work_order_id") or ""),
        run_id=str(workspace.get("run_id") or ""),
        reviewer_profile=str(workspace.get("minion_profile") or ""),
    )
    workspace["review_tool_evidence_refs"] = refs
    _bind_command_evidence_refs(args, refs)
    _bind_lsp_evidence_refs(args, refs)
    metadata = dict(args.get("metadata") or {})
    metadata["tool_evidence_refs"] = refs
    args["metadata"] = metadata
    return args


def _bind_command_evidence_refs(args: dict[str, Any], refs: list[dict[str, Any]]) -> None:
    command_refs = {
        _normalize_command_text(item.get("command") or ""): dict(item)
        for item in refs
        if str(item.get("kind") or "") == "command" and _normalize_command_text(item.get("command") or "")
    }
    commands: list[dict[str, Any]] = []
    changed = False
    for item in list(args.get("commands_run") or []):
        if not isinstance(item, dict):
            commands.append(item)
            continue
        entry = dict(item)
        command = _normalize_command_text(entry.get("command") or entry.get("cmd") or entry.get("name") or "")
        ref = command_refs.get(command)
        if ref and not entry.get("evidence_ref_id"):
            entry["evidence_ref_id"] = ref.get("evidence_ref_id")
            changed = True
        commands.append(entry)
    if changed:
        args["commands_run"] = commands


def _bind_lsp_evidence_refs(args: dict[str, Any], refs: list[dict[str, Any]]) -> None:
    lsp_refs = [dict(item) for item in refs if str(item.get("kind") or "") == "lsp"]
    if not lsp_refs:
        return
    api_evidence: list[dict[str, Any]] = []
    changed = False
    for item in list(args.get("api_evidence") or []):
        if not isinstance(item, dict):
            api_evidence.append(item)
            continue
        entry = dict(item)
        ref = _matching_lsp_ref(entry, lsp_refs)
        if ref and not entry.get("evidence_ref_id"):
            entry["evidence_ref_id"] = ref.get("evidence_ref_id")
            changed = True
        api_evidence.append(entry)
    if changed:
        args["api_evidence"] = api_evidence


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
        return "reviewer shell mutated the source repository; pass verdict is blocked until the review is rerun from a clean read-only workspace"
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
                return f"commands_run entry lacks matching op_exec_shell evidence: {command}"
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
            raise ValueError("workspace.repo_path is not available")
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
    if tool_call.name == "op_tool_call":
        return str(tool_call.args.get("name") or tool_call.name).strip()
    return tool_call.name


def _effective_tool_args(tool_call: CanonicalToolCall) -> dict[str, Any]:
    if tool_call.name == "op_tool_call" and isinstance((tool_call.args or {}).get("args"), dict):
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
        ref["operation"] = normalized_target.removeprefix("op_lsp_")
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
    if target_name == "op_exec_shell":
        return "command"
    if target_name.startswith("op_lsp_"):
        return "lsp"
    if target_name in {"op_workspace_tree", "op_workspace_search", "op_workspace_read"}:
        return "source"
    return ""

def _tool_result_text(result: CanonicalToolResult) -> str:
    return default_tool_result_text(result, fallback_ok="tool completed", fallback_error="tool failed")


def _preview_text(value: Any, *, limit: int = 400) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
