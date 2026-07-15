from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.v2.artifacts import ContentAddressedArtifactStore
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.semantic_evidence import (
    record_unavailable_evidence,
    recorded_cases,
    run_lsp_evidence,
    run_shell_evidence,
    scratch_fingerprint,
)
from pal.minion.v2.submission_drafts import (
    SubmissionDraftContext,
    SubmissionDraftStore,
    assert_authoring_schema_budget,
)
from pal.minion.v2.submission_preflight import (
    bound_reference_payload,
    requirement_refs_from_view,
    validate_submission_requirement_refs,
)
from pal.minion.v2.verification import validate_verification_case_order
from pal.minion.workspace_tools import _append_unique_artifact, _write_minion_artifact
from pal.shared import RuntimeStatus


_RUN_TO_KIND_TAG = {
    "op_minion_verification_run_historical_regression": ("historical_regression", "historical_regressions"),
    "op_minion_verification_run_adversarial_case": ("contract_adversarial", "focused_tests"),
    "op_minion_verification_run_focused_test": ("unit", "focused_tests"),
    "op_minion_verification_run_compile_check": ("compile", "compile"),
    "op_minion_verification_run_warning_check": ("compile", "warning_clean"),
    "op_minion_verification_run_consumer_probe": ("consumer_probe", "consumer_probe"),
    "op_minion_verification_run_dogfood": ("consumer_probe", "public_surface_dogfood"),
    "op_minion_verification_run_platform_probe": ("platform_assumption", "platform_probe"),
}

_COMMON_VERIFICATION_CAPABILITIES = frozenset(
    {
        "op_minion_verification_scratch_write",
        "op_minion_verification_run_historical_regression",
        "op_minion_verification_run_adversarial_case",
        "op_minion_verification_run_focused_test",
        "op_minion_verification_run_compile_check",
        "op_minion_verification_run_warning_check",
        "op_minion_verification_run_lsp_check",
        "op_minion_verification_check_unavailable",
        "op_minion_verification_report_module_defect",
        "op_minion_verification_report_dependency_defect",
        "op_minion_verification_report_contract_defect",
        "op_minion_verification_report_architecture_defect",
        "op_minion_verification_report_integration_defect",
        "op_minion_verification_propose_requirement_patch",
        "op_minion_verification_set_summary",
        "op_minion_verification_draft_status",
        "op_minion_verification_remove_case",
        "op_minion_verification_remove_finding",
        "op_minion_verification_submit",
    }
)

_EXECUTION_CAPABILITIES = (
    "op_minion_verification_scratch_write",
    *_RUN_TO_KIND_TAG,
    "op_minion_verification_run_lsp_check",
    "op_minion_verification_check_unavailable",
)
_FINDING_CAPABILITIES = (
    "op_minion_verification_report_module_defect",
    "op_minion_verification_report_dependency_defect",
    "op_minion_verification_report_contract_defect",
    "op_minion_verification_report_architecture_defect",
    "op_minion_verification_report_integration_defect",
    "op_minion_verification_propose_requirement_patch",
    "op_minion_verification_set_summary",
)
VERIFICATION_BUILDER_CAPABILITIES = (
    *_EXECUTION_CAPABILITIES,
    *_FINDING_CAPABILITIES,
    "op_minion_verification_draft_status",
    "op_minion_verification_remove_case",
    "op_minion_verification_remove_finding",
    "op_minion_verification_submit",
)
STANDALONE_REVIEW_BUILDER_CAPABILITIES = (
    *_EXECUTION_CAPABILITIES,
    *_FINDING_CAPABILITIES,
    "op_minion_verification_draft_status",
    "op_minion_verification_remove_case",
    "op_minion_verification_remove_finding",
    "op_minion_review_surface",
    "op_minion_review_conclusion",
    "op_minion_standalone_review_submit",
)
VERIFICATION_TOOL_CAPABILITIES = tuple(dict.fromkeys((*VERIFICATION_BUILDER_CAPABILITIES, *STANDALONE_REVIEW_BUILDER_CAPABILITIES)))

_RUN_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "command": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
        "expected_exit_codes": {"type": "array", "items": {"type": "integer"}},
        "timeout_seconds": {"type": "integer", "minimum": 1},
        "requirement_section": {"type": "string"},
        "requirement": {"type": "string"},
        "path": {"type": "string"},
        "symbol": {"type": "string"},
        "contract_section": {"type": "string"},
        "invariants": {"type": "array", "items": {"type": "string"}},
        "probe_path": {"type": "string"},
    },
    "required": ["name", "command"],
    "additionalProperties": False,
}
_LSP_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "file": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
        "requirement_section": {"type": "string"},
        "requirement": {"type": "string"},
    },
    "required": ["name", "file"],
    "additionalProperties": False,
}
_SCRATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "content": {"type": "string"},
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}
_UNAVAILABLE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "obligation": {
            "type": "string",
            "enum": ["focused_tests", "warning_clean", "consumer_probe", "public_surface_dogfood", "lsp", "historical_regressions", "platform_probe"],
        },
        "reason": {"type": "string", "minLength": 1},
        "requirement_section": {"type": "string"},
        "requirement": {"type": "string"},
        "path": {"type": "string"},
    },
    "required": ["name", "obligation", "reason"],
    "additionalProperties": False,
}
_FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "case": {"type": "string", "minLength": 1},
        "finding_section": {
            "type": "string",
            "enum": ["ownership", "lifecycle", "state_machine", "invariant", "interface", "compatibility", "delivery", "implementation"],
        },
        "summary": {"type": "string", "minLength": 1},
        "failure_reason": {"type": "string", "minLength": 1},
        "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
        "suggested_repair_boundary": {"type": "array", "items": {"type": "string"}},
        "target_module": {"type": "string"},
        "path": {"type": "string"},
        "symbol": {"type": "string"},
        "contract_section": {"type": "string"},
        "invariants": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["case", "finding_section", "summary", "failure_reason", "severity"],
    "additionalProperties": False,
}
_PATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "patch_kind": {"type": "string", "enum": ["clarification", "derived_constraint", "regression_obligation"]},
        "section": {"type": "string", "minLength": 1},
        "requirement": {"type": "string", "minLength": 1},
        "strength": {"type": "string", "enum": ["hard", "soft"]},
        "reason": {"type": "string", "minLength": 1},
        "affected_modules": {"type": "array", "items": {"type": "string"}},
        "contract_path": {"type": "string"},
        "contract_symbol": {"type": "string"},
    },
    "required": ["patch_kind", "section", "requirement", "strength", "reason", "affected_modules"],
    "additionalProperties": False,
}
_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string", "minLength": 1}},
    "required": ["summary"],
    "additionalProperties": False,
}
_SURFACE_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["reviewed", "test_gap", "unreviewed", "residual_risk"]},
        "text": {"type": "string", "minLength": 1},
    },
    "required": ["kind", "text"],
    "additionalProperties": False,
}
_CONCLUSION_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approved", "changes_requested", "blocked"]},
        "summary": {"type": "string", "minLength": 1},
        "scope": {"type": "string"},
    },
    "required": ["verdict", "summary"],
    "additionalProperties": False,
}
_NO_ARGS_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
_NAMED_CASE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "reason": {"type": "string", "minLength": 1},
    },
    "required": ["name", "reason"],
    "additionalProperties": False,
}
_FINDING_CASE_SCHEMA = {
    "type": "object",
    "properties": {
        "case": {"type": "string", "minLength": 1},
        "summary": {"type": "string", "minLength": 1},
        "reason": {"type": "string", "minLength": 1},
    },
    "required": ["case", "summary", "reason"],
    "additionalProperties": False,
}

_DEFECT_PRECEDENCE = {
    "module_defect": 0,
    "integration_defect": 1,
    "dependency_defect": 2,
    "contract_defect": 3,
    "architecture_defect": 4,
}

VERIFICATION_BUILDER_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_minion_verification_scratch_write": {
        "name": "op_verification_scratch_write",
        "description": "Write one verifier-owned test/probe file under the bound scratch directory. This cannot modify product source.",
        "parameters_schema": _SCRATCH_SCHEMA,
    },
    **{
        name: {
            "name": "op_" + name.removeprefix("op_minion_"),
            "description": (
                "Run and durably register one semantic verification case. Cite an exact Requirement, source location, or invariant; "
                "the Manager owns stdout/stderr Artifacts and does not ask you to construct a report object."
            ),
            "parameters_schema": _RUN_SCHEMA,
        }
        for name in _RUN_TO_KIND_TAG
    },
    "op_minion_verification_run_lsp_check": {
        "name": "op_verification_run_lsp_check",
        "description": "Run and durably register LSP diagnostics for one source file when a matching server is available.",
        "parameters_schema": _LSP_SCHEMA,
    },
    "op_minion_verification_check_unavailable": {
        "name": "op_verification_check_unavailable",
        "description": "Record focused_tests, warning_clean, consumer_probe, public_surface_dogfood, lsp, historical_regressions, or platform_probe as UNKNOWN with a concrete environmental reason. This never manufactures PASS evidence.",
        "parameters_schema": _UNAVAILABLE_SCHEMA,
    },
    **{
        name: {
            "name": "op_" + name.removeprefix("op_minion_"),
            "description": "Report one evidence-backed typed defect against an already recorded case. finding_section is ownership, lifecycle, state_machine, invariant, interface, compatibility, delivery, or implementation; severity is blocker, major, or minor. One call records one semantic finding.",
            "parameters_schema": _FINDING_SCHEMA,
        }
        for name in (
            "op_minion_verification_report_module_defect",
            "op_minion_verification_report_dependency_defect",
            "op_minion_verification_report_contract_defect",
            "op_minion_verification_report_architecture_defect",
            "op_minion_verification_report_integration_defect",
        )
    },
    "op_minion_verification_propose_requirement_patch": {
        "name": "op_verification_propose_requirement_patch",
        "description": "Propose one timestamped Requirement patch only after a reproduced contract or architecture defect reveals new product semantics. patch_kind is clarification, derived_constraint, or regression_obligation; strength is hard or soft.",
        "parameters_schema": _PATCH_SCHEMA,
    },
    "op_minion_verification_set_summary": {
        "name": "op_verification_set_summary",
        "description": "Set the concise verifier summary after running cases.",
        "parameters_schema": _SUMMARY_SCHEMA,
    },
    "op_minion_verification_draft_status": {
        "name": "op_verification_draft_status",
        "description": "Read a compact status of the current verification Draft, including case names, statuses, active findings, and remaining policy obligations.",
        "parameters_schema": _NO_ARGS_SCHEMA,
    },
    "op_minion_verification_remove_case": {
        "name": "op_verification_remove_case",
        "description": "Explicitly withdraw one recorded case by semantic name and give an audit reason. All findings attached to it are withdrawn with it. Do not use this to hide a failing case; rerun the case after a real fix instead.",
        "parameters_schema": _NAMED_CASE_SCHEMA,
    },
    "op_minion_verification_remove_finding": {
        "name": "op_verification_remove_finding",
        "description": "Explicitly withdraw exactly one finding by case name and exact finding summary, with an audit reason. The case evidence and any other findings remain. Do not withdraw a finding merely to make submission pass.",
        "parameters_schema": _FINDING_CASE_SCHEMA,
    },
    "op_minion_verification_submit": {
        "name": "op_verification_submit",
        "description": "Submit recorded verification evidence and findings. Takes no arguments; Manager infers verdict and routing from immutable results.",
        "parameters_schema": _NO_ARGS_SCHEMA,
    },
    "op_minion_review_surface": {
        "name": "op_review_surface",
        "description": "Record one standalone-review surface. kind is reviewed, test_gap, unreviewed, or residual_risk.",
        "parameters_schema": _SURFACE_SCHEMA,
    },
    "op_minion_review_conclusion": {
        "name": "op_review_conclusion",
        "description": "Set the standalone review conclusion. verdict is approved, changes_requested, or blocked.",
        "parameters_schema": _CONCLUSION_SCHEMA,
    },
    "op_minion_standalone_review_submit": {
        "name": "op_review_submit",
        "description": "Submit the standalone review. Takes no arguments; Manager compiles recorded cases, findings, surfaces, and conclusion.",
        "parameters_schema": _NO_ARGS_SCHEMA,
    },
}


def effective_verification_policy(
    *,
    work_view: Mapping[str, Any],
    verification_policy: Mapping[str, Any],
    standalone: bool = False,
) -> dict[str, Any]:
    """Compile family defaults into obligations owned by this exact review node."""

    source = dict(verification_policy or {})
    scenario = bool(work_view.get("verification_name")) and not standalone
    mode = "standalone" if standalone else "scenario" if scenario else "module"
    kind = str(work_view.get("kind") or "").strip()
    entrypoints = [dict(item) for item in list(work_view.get("entrypoints") or []) if isinstance(item, Mapping)]
    entrypoint_kinds = {str(item.get("kind") or "").strip() for item in entrypoints}
    require_consumer_probe = scenario and kind == "consumer_probe"
    require_dogfood = scenario and kind == "dogfood"
    require_platform_probe = scenario and "platform_probe" in entrypoint_kinds
    if standalone:
        require_dogfood = bool(source.get("require_public_surface_dogfood", False))
        require_platform_probe = False

    allowed_obligations = {
        "compile",
        "focused_tests",
        "historical_regressions",
        "lsp",
        "warning_clean",
    }
    if mode == "module" or require_consumer_probe:
        allowed_obligations.add("consumer_probe")
    if require_dogfood:
        allowed_obligations.add("public_surface_dogfood")
    if require_platform_probe:
        allowed_obligations.add("platform_probe")
    if standalone:
        allowed_obligations.update(
            {"consumer_probe", "public_surface_dogfood", "platform_probe"}
        )

    return {
        "mode": mode,
        "require_focused_tests": bool(source.get("require_focused_tests", False)),
        "require_warning_clean": bool(source.get("require_warning_clean", False)),
        "require_consumer_probe": require_consumer_probe,
        "require_public_surface_dogfood": require_dogfood,
        "require_platform_probe": require_platform_probe,
        "require_historical_regressions": bool(
            source.get("require_historical_regressions", False)
        ),
        "lsp_policy": str(source.get("lsp_policy") or ""),
        "unknown_policy": str(source.get("unknown_policy") or "strict"),
        "case_timeout_seconds": int(source.get("case_timeout_seconds") or 300),
        "allowed_obligations": sorted(allowed_obligations),
    }


def compile_verification_invocation_tool_contract(
    *,
    work_view: Mapping[str, Any],
    verification_policy: Mapping[str, Any],
    standalone: bool = False,
) -> dict[str, Any]:
    """Compile one stable, invocation-local description contract from bound inputs."""

    requirements: dict[str, list[str]] = {}
    for section, requirement in sorted(requirement_refs_from_view(work_view)):
        requirements.setdefault(section, []).append(requirement)
    module_name = str(
        work_view.get("module_name")
        or work_view.get("verification_name")
        or ("standalone_review" if standalone else "")
    ).strip()
    accepted_modules = sorted(
        {
            str(dict(item or {}).get("module_name") or "").strip()
            for item in list(work_view.get("accepted_modules") or [])
            if str(dict(item or {}).get("module_name") or "").strip()
        }
    )
    dependencies = sorted(
        {
            str(item).strip()
            for item in list(
                work_view.get("construction_dependencies")
                or work_view.get("depends_on")
                or []
            )
            if str(item).strip()
        }
    )
    consumption = sorted(
        (
            {
                key: str(dict(item or {}).get(key) or "").strip()
                for key in ("module", "path", "symbol")
                if str(dict(item or {}).get(key) or "").strip()
            }
            for item in list(work_view.get("contract_consumption") or [])
            if isinstance(item, Mapping)
        ),
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
    )
    consumed_modules = sorted(
        {str(item.get("module") or "") for item in consumption if item.get("module")}
    )
    contract_paths = sorted(
        {
            str(item).strip()
            for item in list(work_view.get("contract_paths") or [])
            if str(item).strip()
        }
    )
    entrypoints = [
        dict(item) if isinstance(item, Mapping) else {"target": str(item).strip()}
        for item in list(work_view.get("entrypoints") or [])
        if isinstance(item, Mapping) or str(item).strip()
    ]
    implementation_targets = accepted_modules or ([module_name] if module_name else [])
    all_targets = sorted(set(implementation_targets + dependencies + consumed_modules))
    allowed_targets = {
        "module_defect": implementation_targets,
        "dependency_defect": sorted(set(dependencies + consumed_modules)),
        "contract_defect": all_targets,
        "architecture_defect": all_targets,
        "integration_defect": ["integration"] if module_name == "integration" else [],
    }
    policy = effective_verification_policy(
        work_view=work_view,
        verification_policy=verification_policy,
        standalone=standalone,
    )
    allowed_capabilities = set(
        STANDALONE_REVIEW_BUILDER_CAPABILITIES
        if standalone
        else _COMMON_VERIFICATION_CAPABILITIES
    )
    if "consumer_probe" in set(policy["allowed_obligations"]):
        allowed_capabilities.add("op_minion_verification_run_consumer_probe")
    if "public_surface_dogfood" in set(policy["allowed_obligations"]):
        allowed_capabilities.add("op_minion_verification_run_dogfood")
    if "platform_probe" in set(policy["allowed_obligations"]):
        allowed_capabilities.add("op_minion_verification_run_platform_probe")
    if not allowed_targets["integration_defect"]:
        allowed_capabilities.discard("op_minion_verification_report_integration_defect")
    contract: dict[str, Any] = {
        "contract_version": "1",
        "requirements": requirements,
        "module_name": module_name,
        "contract_paths": contract_paths,
        "contract_consumption": consumption,
        "entrypoints": entrypoints,
        "verification_policy": policy,
        "allowed_capabilities": sorted(allowed_capabilities),
        "allowed_obligations": list(policy["allowed_obligations"]),
        "allowed_defect_targets": allowed_targets,
    }
    requirement_text = json.dumps(requirements, ensure_ascii=False, sort_keys=True)
    boundary_text = json.dumps(
        {
            "contract_paths": contract_paths,
            "contract_consumption": consumption,
            "entrypoints": entrypoints,
            "policy": policy,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    overrides: dict[str, str] = {}
    for capability in (*_RUN_TO_KIND_TAG, "op_minion_verification_run_lsp_check", "op_minion_verification_check_unavailable"):
        overrides[capability] = (
            "Record one semantic verification case. requirement_section must name a section in the bound catalog below. "
            "If that section has one Requirement, Manager binds it automatically and any requirement hint is ignored; "
            "if it has several, requirement must exactly equal one candidate. Validation occurs before execution or Draft mutation. "
            "Reusing a case name updates that case. description may use any language. Set probe_path whenever the command "
            "consumes a verifier scratch file so exact evidence reuse is invalidated only by that file. "
            f"Bound Requirements: {requirement_text}. Bound verification context: {boundary_text}."
        )
    for capability in (
        "op_minion_verification_report_module_defect",
        "op_minion_verification_report_dependency_defect",
        "op_minion_verification_report_contract_defect",
        "op_minion_verification_report_architecture_defect",
        "op_minion_verification_report_integration_defect",
    ):
        defect_kind = capability.removeprefix("op_minion_verification_report_")
        overrides[capability] = (
            "Record one independently actionable finding for an existing case. A case may expose several findings; record each "
            "material defect separately. The finding inherits the case's canonical Requirement, locations, and invariants unless "
            "you supply a narrower path/symbol/contract section. Exact semantic duplicates are ignored. Findings may cite only "
            "FAIL or UNKNOWN cases. "
            f"Defect kind: {defect_kind}. Allowed target_module values: "
            f"{json.dumps(allowed_targets.get(defect_kind) or [], ensure_ascii=False)}."
        )
    contract["description_overrides"] = overrides
    contract["fingerprint"] = hashlib.sha256(
        json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return contract


def dominant_verification_defect_kind(findings: list[Mapping[str, Any]]) -> str:
    kinds = {
        str(item.get("defect_kind") or "").strip()
        for item in findings
        if str(item.get("defect_kind") or "").strip()
    }
    return max(kinds, key=lambda item: _DEFECT_PRECEDENCE.get(item, -1)) if kinds else ""

for _tool_name, _tool_spec in VERIFICATION_BUILDER_TOOL_SPECS.items():
    assert_authoring_schema_budget(_tool_spec["parameters_schema"], owner=_tool_name)


def is_verification_builder_capability(name: str) -> bool:
    return str(name or "") in VERIFICATION_BUILDER_TOOL_SPECS


async def verification_builder_tool_result(
    call: CanonicalToolCall,
    workspace: dict[str, Any],
    produced_artifacts: list[dict[str, Any]],
    *,
    original_adapter: Any | None = None,
    turn_id: str | None = None,
) -> CanonicalToolResult:
    name = str(call.name or "")
    draft_kind = _draft_kind(workspace)
    try:
        _assert_tool_contract_allows(workspace, name=name, args=dict(call.args or {}))
    except Exception as exc:
        return _error(call, exc)
    if name in _RUN_TO_KIND_TAG:
        case_kind, obligation = _RUN_TO_KIND_TAG[name]
        try:
            _preflight_verification_case_execution(
                workspace,
                draft_kind=draft_kind,
                requested_case_kind=case_kind,
            )
        except Exception as exc:
            return _error(call, exc)
        return await run_shell_evidence(
            call,
            workspace=workspace,
            original_adapter=_require_adapter(original_adapter),
            draft_kind=draft_kind,
            case_kind=case_kind,
            obligation_tag=obligation,
            turn_id=turn_id,
        )
    if name == "op_minion_verification_run_lsp_check":
        return await run_lsp_evidence(
            call,
            workspace=workspace,
            original_adapter=_require_adapter(original_adapter),
            draft_kind=draft_kind,
            turn_id=turn_id,
        )
    if name == "op_minion_verification_check_unavailable":
        return record_unavailable_evidence(call, workspace=workspace, draft_kind=draft_kind)
    try:
        if name == "op_minion_verification_scratch_write":
            return _scratch_write(call, workspace, draft_kind=draft_kind)
        if name.startswith("op_minion_verification_report_"):
            return _record_finding(call, workspace, draft_kind=draft_kind)
        if name == "op_minion_verification_propose_requirement_patch":
            return _set_requirement_patch(call, workspace, draft_kind=draft_kind)
        if name == "op_minion_verification_set_summary":
            return _set_summary(call, workspace, draft_kind=draft_kind)
        if name == "op_minion_verification_draft_status":
            return _draft_status(call, workspace, draft_kind=draft_kind)
        if name == "op_minion_verification_remove_case":
            return _remove_case(call, workspace, draft_kind=draft_kind)
        if name == "op_minion_verification_remove_finding":
            return _remove_finding(call, workspace, draft_kind=draft_kind)
        if name == "op_minion_review_surface":
            return _record_review_surface(call, workspace)
        if name == "op_minion_review_conclusion":
            return _set_review_conclusion(call, workspace)
        if name == "op_minion_verification_submit":
            return _submit(call, workspace, produced_artifacts, standalone=False)
        if name == "op_minion_standalone_review_submit":
            return _submit(call, workspace, produced_artifacts, standalone=True)
        raise ValueError(f"unknown verification authoring capability: {name}")
    except Exception as exc:
        return _error(call, exc)


def _scratch_write(call: CanonicalToolCall, workspace: Mapping[str, Any], *, draft_kind: str) -> CanonicalToolResult:
    args = dict(call.args or {})
    relative = PurePosixPath(str(args.get("path") or ""))
    if not str(relative) or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("scratch path must be a safe relative path")
    context, store = _store_context(workspace, draft_kind=draft_kind)
    store.read(context, seed=_empty_payload())
    root = Path(str(workspace.get("review_scratch_dir") or ""))
    if not root:
        raise ValueError("review scratch directory is not bound")
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(args.get("content") or ""), encoding="utf-8")

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        definitions = dict(payload.get("definitions") or {})
        files = list(definitions.get("scratch_files") or [])
        if str(relative) not in files:
            files.append(str(relative))
        definitions["scratch_files"] = files
        payload["definitions"] = definitions
        return payload, {"written": str(relative), "scratch_fingerprint": scratch_fingerprint(workspace)}

    result = store.mutate(
        context,
        operation_key=str(call.call_id or f"scratch:{relative}"),
        request=args,
        reducer=reducer,
        seed=_empty_payload(),
    )
    return _ok(call, f"scratch file written: {relative}", result)


def _record_finding(call: CanonicalToolCall, workspace: Mapping[str, Any], *, draft_kind: str) -> CanonicalToolResult:
    args = dict(call.args or {})
    case_name = str(args.get("case") or "").strip()
    context, store = _store_context(workspace, draft_kind=draft_kind)
    snapshot = store.read(context, seed=_empty_payload())
    cases_by_name = {
        str(item.get("name")): dict(item) for item in recorded_cases(snapshot.payload)
    }
    if case_name not in cases_by_name:
        raise ValueError(
            "finding must cite an exact recorded case name; known cases: "
            + ", ".join(sorted(cases_by_name))
        )
    defect_kind = call.name.removeprefix("op_minion_verification_report_")
    target_module = _resolve_finding_target(
        workspace,
        defect_kind=defect_kind,
        requested=str(args.get("target_module") or "").strip(),
    )
    case = cases_by_name[case_name]
    if str(case.get("status") or "") not in {"FAIL", "UNKNOWN"}:
        raise ValueError("finding may cite only a FAIL or UNKNOWN verification case")
    locations = [dict(item) for item in list(case.get("locations") or [])]
    explicit_location = _single_location(args)
    if explicit_location:
        locations = _unique_mappings([*locations, *explicit_location])
    invariants = [str(item) for item in list(case.get("invariants") or []) if str(item).strip()]
    explicit_invariants = [
        str(item).strip()
        for item in list(args.get("invariants") or [])
        if str(item).strip()
    ]
    invariants = list(dict.fromkeys([*invariants, *explicit_invariants]))
    finding = {
        "case": case_name,
        "finding_section": str(args.get("finding_section") or "implementation"),
        "summary": str(args.get("summary") or "").strip(),
        "failure_reason": str(args.get("failure_reason") or "").strip(),
        "requirements": [dict(item) for item in list(case.get("requirements") or [])],
        "locations": locations,
        "invariants": invariants,
        "severity": str(args.get("severity") or "major"),
        "suggested_repair_boundary": [str(item) for item in list(args.get("suggested_repair_boundary") or [])],
        "defect_kind": defect_kind,
        "target_module": target_module,
    }
    if not finding["summary"] or not finding["failure_reason"]:
        raise ValueError("finding requires summary and failure_reason")
    finding_fingerprint = _semantic_finding_fingerprint(finding)
    finding["_finding_fingerprint"] = finding_fingerprint

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        findings = [dict(item) for item in list(payload.get("findings") or [])]
        if any(
            str(item.get("_finding_fingerprint") or _semantic_finding_fingerprint(item))
            == finding_fingerprint
            for item in findings
        ):
            return payload, {
                "recorded": False,
                "deduplicated": True,
                "finding": _public_finding(finding),
            }
        findings.append(finding)
        payload["findings"] = findings
        return payload, {
            "recorded": True,
            "deduplicated": False,
            "finding": _public_finding(finding),
        }

    result = store.mutate(
        context,
        operation_key=str(call.call_id or f"finding:{case_name}:{len(snapshot.payload.get('findings') or [])}"),
        request=args,
        reducer=reducer,
        seed=_empty_payload(),
    )
    return _ok(call, "verification finding recorded", result)


def _draft_status(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
    *,
    draft_kind: str,
) -> CanonicalToolResult:
    if dict(call.args or {}):
        raise ValueError(f"{call.name} takes no arguments")
    context, store = _store_context(workspace, draft_kind=draft_kind)
    snapshot = store.read(context, seed=_empty_payload())
    cases = recorded_cases(snapshot.payload)
    findings = [dict(item) for item in list(snapshot.payload.get("findings") or [])]
    tags = {
        str(tag)
        for item in cases
        for tag in list(item.get("obligation_tags") or [])
    }
    policy = bound_reference_payload(workspace, "verification_policy", required=False)
    required_tags = {
        tag
        for key, tag in (
            ("require_focused_tests", "focused_tests"),
            ("require_warning_clean", "warning_clean"),
            ("require_consumer_probe", "consumer_probe"),
            ("require_public_surface_dogfood", "public_surface_dogfood"),
            ("require_platform_probe", "platform_probe"),
            ("require_historical_regressions", "historical_regressions"),
        )
        if bool(policy.get(key, False))
    }
    if str(policy.get("lsp_policy") or "") == "when_available":
        required_tags.add("lsp")
    result = {
        "draft_version": snapshot.version,
        "status": snapshot.status,
        "cases": [
            {"name": str(item.get("name") or ""), "status": str(item.get("status") or "")}
            for item in cases
        ],
        "findings": [
            {
                "case": str(item.get("case") or ""),
                "defect_kind": str(item.get("defect_kind") or ""),
                "summary": str(item.get("summary") or ""),
            }
            for item in findings
        ],
        "remaining_policy_obligations": sorted(required_tags - tags),
    }
    return _ok(call, "verification Draft status", result)


def _remove_case(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
    *,
    draft_kind: str,
) -> CanonicalToolResult:
    args = dict(call.args or {})
    name = str(args.get("name") or "").strip()
    reason = str(args.get("reason") or "").strip()
    if not reason:
        raise ValueError("removing a verification case requires an audit reason")
    context, store = _store_context(workspace, draft_kind=draft_kind)

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        evidence = dict(payload.get("evidence") or {})
        cases = dict(evidence.get("cases") or {})
        removed = cases.pop(name, None) is not None
        evidence["cases"] = cases
        payload["evidence"] = evidence
        payload["findings"] = [
            dict(item)
            for item in list(payload.get("findings") or [])
            if str(dict(item).get("case") or "") != name
        ]
        return payload, {"removed": removed, "case": name, "reason": reason}

    result = store.mutate(
        context,
        operation_key=str(call.call_id or f"remove-case:{name}"),
        request=args,
        reducer=reducer,
        seed=_empty_payload(),
    )
    return _ok(call, f"verification case removed: {name}", result)


def _remove_finding(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
    *,
    draft_kind: str,
) -> CanonicalToolResult:
    args = dict(call.args or {})
    case_name = str(args.get("case") or "").strip()
    summary = str(args.get("summary") or "").strip()
    reason = str(args.get("reason") or "").strip()
    if not case_name or not summary:
        raise ValueError("removing a finding requires case and exact summary")
    if not reason:
        raise ValueError("removing a verification finding requires an audit reason")
    context, store = _store_context(workspace, draft_kind=draft_kind)

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        findings = [dict(item) for item in list(payload.get("findings") or [])]
        matches = [
            item
            for item in findings
            if str(item.get("case") or "") == case_name
            and str(item.get("summary") or "") == summary
        ]
        if not matches:
            candidates = [
                str(item.get("summary") or "")
                for item in findings
                if str(item.get("case") or "") == case_name
            ]
            raise ValueError(
                "no finding matches the exact case and summary; candidate summaries: "
                + json.dumps(candidates, ensure_ascii=False)
            )
        retained = [item for item in findings if item is not matches[0]]
        payload["findings"] = retained
        return payload, {
            "removed": True,
            "case": case_name,
            "summary": summary,
            "reason": reason,
        }

    result = store.mutate(
        context,
        operation_key=str(call.call_id or f"remove-finding:{case_name}:{hashlib.sha256(summary.encode('utf-8')).hexdigest()[:12]}"),
        request=args,
        reducer=reducer,
        seed=_empty_payload(),
    )
    return _ok(call, f"verification finding removed for case: {case_name}", result)


def _set_requirement_patch(call: CanonicalToolCall, workspace: Mapping[str, Any], *, draft_kind: str) -> CanonicalToolResult:
    args = dict(call.args or {})
    affected_modules = [str(item).strip() for item in list(args.get("affected_modules") or []) if str(item).strip()]
    if not affected_modules:
        raise ValueError("Requirement patch requires at least one affected module")
    contract_path = str(args.get("contract_path") or "").strip()
    patch = {
        "patch_kind": str(args.get("patch_kind") or ""),
        "section": str(args.get("section") or "").strip(),
        "requirement": str(args.get("requirement") or "").strip(),
        "strength": str(args.get("strength") or ""),
        "reason": str(args.get("reason") or "").strip(),
        "affected_modules": affected_modules,
        "affected_contracts": (
            [{"module": affected_modules[0], "path": contract_path, **({"symbol": str(args.get("contract_symbol"))} if args.get("contract_symbol") else {})}]
            if contract_path
            else []
        ),
    }
    context, store = _store_context(workspace, draft_kind=draft_kind)
    snapshot = store.read(context, seed=_empty_payload())
    cases = {str(item.get("name") or ""): dict(item) for item in recorded_cases(snapshot.payload)}
    eligible = [
        dict(item)
        for item in list(snapshot.payload.get("findings") or [])
        if str(dict(item).get("defect_kind") or "") in {"contract_defect", "architecture_defect"}
        and str(cases.get(str(dict(item).get("case") or ""), {}).get("status") or "") == "FAIL"
    ]
    if not eligible:
        raise ValueError(
            "Requirement patch requires a reproduced FAIL already recorded as contract_defect or architecture_defect"
        )

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        summary = dict(payload.get("summary") or {})
        summary["requirement_patch"] = patch
        payload["summary"] = summary
        return payload, {"recorded": True, "requirement_patch": patch}

    result = store.mutate(context, operation_key=str(call.call_id or "requirement-patch"), request=args, reducer=reducer, seed=_empty_payload())
    return _ok(call, "Requirement patch proposed", result)


def _set_summary(call: CanonicalToolCall, workspace: Mapping[str, Any], *, draft_kind: str) -> CanonicalToolResult:
    args = dict(call.args or {})
    summary_text = str(args.get("summary") or "").strip()
    if not summary_text:
        raise ValueError("summary is required")
    context, store = _store_context(workspace, draft_kind=draft_kind)

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        summary = dict(payload.get("summary") or {})
        summary["reviewer_summary"] = summary_text
        payload["summary"] = summary
        return payload, {"recorded": True}

    result = store.mutate(context, operation_key=str(call.call_id or "summary"), request=args, reducer=reducer, seed=_empty_payload())
    return _ok(call, "verification summary recorded", result)


def _record_review_surface(call: CanonicalToolCall, workspace: Mapping[str, Any]) -> CanonicalToolResult:
    args = dict(call.args or {})
    kind = str(args.get("kind") or "")
    text = str(args.get("text") or "").strip()
    context, store = _store_context(workspace, draft_kind="standalone_review")

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        summary = dict(payload.get("summary") or {})
        values = list(summary.get(kind) or [])
        if text not in values:
            values.append(text)
        summary[kind] = values
        payload["summary"] = summary
        return payload, {"recorded": True, "kind": kind}

    result = store.mutate(context, operation_key=str(call.call_id or f"surface:{kind}:{text}"), request=args, reducer=reducer, seed=_empty_payload())
    return _ok(call, "review surface recorded", result)


def _set_review_conclusion(call: CanonicalToolCall, workspace: Mapping[str, Any]) -> CanonicalToolResult:
    args = dict(call.args or {})
    context, store = _store_context(workspace, draft_kind="standalone_review")

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        summary = dict(payload.get("summary") or {})
        summary["conclusion"] = {
            "verdict": str(args.get("verdict") or ""),
            "summary": str(args.get("summary") or "").strip(),
            "scope": str(args.get("scope") or "").strip(),
        }
        payload["summary"] = summary
        return payload, {"recorded": True}

    result = store.mutate(context, operation_key=str(call.call_id or "review-conclusion"), request=args, reducer=reducer, seed=_empty_payload())
    return _ok(call, "review conclusion recorded", result)


def _submit(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
    produced_artifacts: list[dict[str, Any]],
    *,
    standalone: bool,
) -> CanonicalToolResult:
    if dict(call.args or {}):
        raise ValueError(f"{call.name} takes no arguments")
    draft_kind = "standalone_review" if standalone else "verification"
    context, store = _store_context(workspace, draft_kind=draft_kind)
    snapshot = store.read(context, seed=_empty_payload())
    cases = recorded_cases(snapshot.payload)
    if not cases:
        raise ValueError("submit requires at least one recorded verification case")
    findings = [dict(item) for item in list(snapshot.payload.get("findings") or [])]
    summary = dict(snapshot.payload.get("summary") or {})
    _validate_case_references(cases, workspace=workspace, standalone=standalone)
    if standalone:
        conclusion = dict(summary.get("conclusion") or {})
        if not conclusion:
            raise ValueError("standalone review requires review_conclusion before submit")
        output = {
            "verdict": str(conclusion.get("verdict") or ""),
            "scope": {"description": str(conclusion.get("scope") or "")},
            "reviewed_surfaces": list(summary.get("reviewed") or []),
            "cases": [_case_declaration(item) for item in cases],
            "findings": [_public_finding(item) for item in findings],
            "commands_or_lsp_evidence": [str(item.get("name")) for item in cases],
            "test_gaps": list(summary.get("test_gap") or []),
            "unreviewed_surfaces": list(summary.get("unreviewed") or []),
            "residual_risk": list(summary.get("residual_risk") or []),
            "reviewer_summary": str(conclusion.get("summary") or ""),
            "recorded_results": cases,
            "internal_context": _internal_context(context, workspace),
        }
        filename = "standalone_review.json"
        title = "Standalone review submission"
    else:
        defect_kind = dominant_verification_defect_kind(findings)
        output = {
            "cases": [_case_declaration(item) for item in cases],
            "findings": [_public_finding(item) for item in findings],
            "reviewer_summary": _default_summary(cases, findings),
            **(
                {"reviewer_notes": str(summary.get("reviewer_summary") or "")}
                if str(summary.get("reviewer_summary") or "").strip()
                else {}
            ),
            "recorded_results": cases,
            "internal_context": _internal_context(context, workspace),
        }
        if defect_kind:
            output["defect_kind"] = defect_kind
            dominant_findings = [
                item for item in findings if str(item.get("defect_kind") or "") == defect_kind
            ]
            target = next(
                (
                    str(item.get("target_module") or "")
                    for item in dominant_findings
                    if item.get("target_module")
                ),
                "",
            )
            if defect_kind == "dependency_defect" and target:
                output["dependency_module"] = target
            elif defect_kind == "module_defect" and target:
                output["affected_module"] = target
            first = dominant_findings[0]
            output["severity"] = str(first.get("severity") or "major")
            output["suggested_repair_boundary"] = list(first.get("suggested_repair_boundary") or [])
        if summary.get("requirement_patch"):
            output["requirement_patch"] = dict(summary["requirement_patch"])
        output["policy_exceptions"] = _policy_exceptions(cases)
        filename = "verification_plan.json"
        title = "V2 semantic verification plan"
    validate_semantic_verification_plan_shape(output, standalone=standalone, require_complete=True)
    if not standalone:
        _preflight_verification_submission(output, workspace)
    if store.uses_worker_gateway:
        store.mark_submitted(
            context,
            expected_version=snapshot.version,
            submission_payload=output,
        )
    else:
        runtime_root = Path(str(workspace["runtime_root"]))
        submission_store = ContentAddressedArtifactStore(
            runtime_root,
            MinionV2Repository(runtime_root),
        )
        submission_ref = submission_store.put_json(
            output,
            artifact_type=(
                "StandaloneReviewSubmissionArtifact"
                if standalone
                else "VerifierSubmissionArtifact"
            ),
            provenance={
                "workflow_id": context.workflow_id,
                "invocation_id": context.invocation_id,
                "role": context.role,
                "draft_key": context.draft_key,
            },
        )
        submission_payload_hash = hashlib.sha256(
            json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        store.mark_submitted(
            context,
            expected_version=snapshot.version,
            submission_artifact_ref=submission_ref.to_dict(),
            submission_payload_hash=submission_payload_hash,
            submission_payload=output,
        )
    artifact = _write_minion_artifact(
        dict(workspace),
        {
            "relative_path": filename,
            "title": title,
            "role": "primary",
            "mime_type": "application/json",
            "overwrite": True,
            "content": json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        },
    )
    _append_unique_artifact(produced_artifacts, artifact)
    return _ok(
        call,
        f"{title} submitted. Stop now.",
        {
            "submitted": True,
            "artifact": artifact,
            "submission_receipt": submission_ref.to_dict(),
        },
    )


def validate_semantic_verification_plan_shape(value: Mapping[str, Any], *, standalone: bool, require_complete: bool = True) -> None:
    required = (
        {"verdict", "scope", "reviewed_surfaces", "cases", "findings", "commands_or_lsp_evidence", "test_gaps", "unreviewed_surfaces", "residual_risk", "reviewer_summary", "recorded_results", "internal_context"}
        if standalone
        else {"cases", "findings", "reviewer_summary", "recorded_results", "internal_context"}
    )
    missing = required - set(value) if require_complete else set()
    if missing:
        raise ValueError("compiled review artifact is missing Manager fields: " + ", ".join(sorted(missing)))
    if not isinstance(value.get("cases"), list) or not value.get("cases"):
        raise ValueError("compiled review artifact requires cases")
    if not isinstance(value.get("findings"), list):
        raise ValueError("compiled review findings must be an array")
    if not isinstance(value.get("recorded_results"), list) or len(value["recorded_results"]) != len(value["cases"]):
        raise ValueError("every case requires one Manager-recorded result")
    if standalone and str(value.get("verdict") or "") not in {"approved", "changes_requested", "blocked"}:
        raise ValueError("standalone verdict is invalid")


def _preflight_verification_submission(value: Mapping[str, Any], workspace: Mapping[str, Any]) -> None:
    work_view = bound_reference_payload(workspace, "module_work_view", required=False)
    if work_view:
        validate_submission_requirement_refs(value, work_view=work_view, owner="Verifier submission")
    historical = list(work_view.get("historical_repair_bills") or []) or list(
        work_view.get("historical_repair_bill_refs") or []
    )
    validate_verification_case_order(
        [str(dict(item).get("case_kind") or "") for item in list(value.get("recorded_results") or [])],
        historical_required=bool(historical),
    )
    policy = bound_reference_payload(workspace, "verification_policy", required=False)
    if not policy:
        return
    tags = {str(tag) for item in list(value.get("recorded_results") or []) for tag in list(dict(item).get("obligation_tags") or [])}
    exceptions = dict(value.get("policy_exceptions") or {})
    obligations = (
        ("require_focused_tests", "focused_tests"),
        ("require_warning_clean", "warning_clean"),
        ("require_consumer_probe", "consumer_probe"),
        ("require_public_surface_dogfood", "public_surface_dogfood"),
        ("require_platform_probe", "platform_probe"),
    )
    for policy_key, tag in obligations:
        if bool(policy.get(policy_key, False)) and tag not in tags and not str(exceptions.get(tag) or "").strip():
            raise ValueError(f"VerificationPolicy requires {tag} evidence or an explicit UNKNOWN reason")
    if bool(policy.get("require_historical_regressions", False)) and historical and "historical_regressions" not in tags:
        raise ValueError("VerificationPolicy requires historical RepairBill regression evidence")
    if str(policy.get("lsp_policy") or "") == "when_available" and "lsp" not in tags and not str(exceptions.get("lsp") or "").strip():
        raise ValueError("VerificationPolicy requires LSP evidence or an explicit UNKNOWN reason")
    allowed_obligations = {
        str(item) for item in list(policy.get("allowed_obligations") or []) if str(item)
    }
    unexpected = tags - allowed_obligations if allowed_obligations else set()
    if unexpected:
        raise ValueError(
            "verification submission contains obligations outside this node's scope: "
            + ", ".join(sorted(unexpected))
        )
    findings_by_case = {
        str(item.get("case") or "")
        for item in list(value.get("findings") or [])
        if str(item.get("case") or "")
    }
    failed_without_findings = [
        str(item.get("name") or "")
        for item in list(value.get("recorded_results") or [])
        if str(item.get("status") or "") == "FAIL"
        and str(item.get("name") or "") not in findings_by_case
    ]
    if failed_without_findings:
        raise ValueError(
            "every FAIL case requires at least one structured finding: "
            + ", ".join(sorted(failed_without_findings))
        )


def _preflight_verification_case_execution(
    workspace: Mapping[str, Any],
    *,
    draft_kind: str,
    requested_case_kind: str,
) -> None:
    if draft_kind != "verification" or requested_case_kind not in {
        "contract_adversarial",
        "diff_risk",
    }:
        return
    work_view = bound_reference_payload(workspace, "module_work_view", required=False)
    historical = list(work_view.get("historical_repair_bills") or []) or list(
        work_view.get("historical_repair_bill_refs") or []
    )
    if not historical:
        return
    context, store = _store_context(workspace, draft_kind=draft_kind)
    cases = recorded_cases(store.read(context, seed=_empty_payload()).payload)
    if any(str(item.get("case_kind") or "") == "historical_regression" for item in cases):
        return
    raise ValueError(
        "run the historical RepairBill regression before adversarial or diff-risk cases"
    )


def _validate_case_references(cases: list[Mapping[str, Any]], *, workspace: Mapping[str, Any], standalone: bool) -> None:
    for item in cases:
        if not (list(item.get("requirements") or []) or list(item.get("locations") or []) or list(item.get("invariants") or [])):
            raise ValueError(f"case {item.get('name')!r} requires Requirement text, a source location, or an invariant")
    view_name = "review_request" if standalone else "module_work_view"
    view = bound_reference_payload(workspace, view_name, required=False)
    if view and requirement_refs_from_view(view):
        validate_submission_requirement_refs({"cases": cases}, work_view=view, owner="Review evidence")


def _case_declaration(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": str(item.get("name") or ""),
        "case_kind": str(item.get("case_kind") or ""),
        "command": list(item.get("command") or []),
        "expected_exit_codes": list(item.get("expected_exit_codes") or [0]),
        "requirements": [dict(value) for value in list(item.get("requirements") or [])],
        "locations": [dict(value) for value in list(item.get("locations") or [])],
        "invariants": [str(value) for value in list(item.get("invariants") or [])],
        "description": str(item.get("description") or ""),
    }


def _public_finding(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in dict(item).items()
        if not str(key).startswith("_")
    }


def _internal_context(context: SubmissionDraftContext, workspace: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "draft_key": context.draft_key,
        "invocation_id": context.invocation_id,
        "fencing_token": context.fencing_token,
        "input_fingerprint": context.input_fingerprint,
        "scratch_fingerprint": scratch_fingerprint(workspace),
    }


def _policy_exceptions(cases: list[Mapping[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in cases:
        if str(item.get("status") or "") != "UNKNOWN":
            continue
        for tag in list(item.get("obligation_tags") or []):
            result[str(tag)] = str(item.get("summary") or "UNKNOWN")
    return result


def _default_summary(cases: list[Mapping[str, Any]], findings: list[Mapping[str, Any]]) -> str:
    counts = {status: sum(1 for item in cases if str(item.get("status")) == status) for status in ("PASS", "FAIL", "UNKNOWN")}
    finding_counts: dict[str, int] = {}
    for item in findings:
        kind = str(item.get("defect_kind") or "unclassified")
        finding_counts[kind] = finding_counts.get(kind, 0) + 1
    suffix = ", ".join(f"{kind}={count}" for kind, count in sorted(finding_counts.items()))
    return (
        f"Recorded {len(cases)} cases: {counts['PASS']} PASS, {counts['FAIL']} FAIL, "
        f"{counts['UNKNOWN']} UNKNOWN; {len(findings)} findings"
        + (f" ({suffix})." if suffix else ".")
    )


def _semantic_finding_fingerprint(item: Mapping[str, Any]) -> str:
    semantic = {
        str(key): value
        for key, value in dict(item).items()
        if not str(key).startswith("_")
    }
    return hashlib.sha256(
        json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _unique_mappings(values: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        item = dict(value)
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _draft_kind(workspace: Mapping[str, Any]) -> str:
    role = str(dict(workspace.get("minion_v2") or {}).get("role") or "")
    return "standalone_review" if role == "reviewer" else "verification"


def _store_context(workspace: Mapping[str, Any], *, draft_kind: str) -> tuple[SubmissionDraftContext, SubmissionDraftStore]:
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind=draft_kind)
    return context, SubmissionDraftStore(Path(str(workspace["runtime_root"])))


def _empty_payload() -> dict[str, Any]:
    return {"definitions": {"scratch_files": []}, "evidence": {"cases": {}}, "findings": [], "summary": {}}


def _single_requirement(args: Mapping[str, Any]) -> list[dict[str, str]]:
    section = str(args.get("requirement_section") or "").strip()
    requirement = str(args.get("requirement") or "").strip()
    if bool(section) != bool(requirement):
        raise ValueError("requirement_section and requirement must be provided together")
    return [{"section": section, "requirement": requirement}] if section else []


def _single_location(args: Mapping[str, Any]) -> list[dict[str, str]]:
    path = str(args.get("path") or "").strip()
    if not path:
        return []
    return [{"path": path, **({"symbol": str(args.get("symbol"))} if args.get("symbol") else {}), **({"section": str(args.get("contract_section"))} if args.get("contract_section") else {})}]


def _resolve_finding_target(
    workspace: Mapping[str, Any],
    *,
    defect_kind: str,
    requested: str,
) -> str:
    binding = dict(workspace.get("minion_v2") or {})
    contract = dict(binding.get("verification_tool_contract") or {})
    if not contract:
        return requested
    allowed = [
        str(item)
        for item in list(
            dict(contract.get("allowed_defect_targets") or {}).get(defect_kind) or []
        )
        if str(item).strip()
    ]
    if requested:
        if requested not in allowed:
            rendered = ", ".join(allowed) or "<none>"
            raise ValueError(
                f"target_module {requested!r} is invalid for {defect_kind}; allowed targets: {rendered}"
            )
        return requested
    if len(allowed) == 1:
        return allowed[0]
    if defect_kind in {"module_defect", "dependency_defect"} and len(allowed) > 1:
        raise ValueError(
            f"{defect_kind} requires target_module because several targets are bound: "
            + ", ".join(allowed)
        )
    return ""


def _assert_tool_contract_allows(
    workspace: Mapping[str, Any],
    *,
    name: str,
    args: Mapping[str, Any],
) -> None:
    binding = dict(workspace.get("minion_v2") or {})
    contract = dict(binding.get("verification_tool_contract") or {})
    if not contract:
        return
    allowed = {str(item) for item in list(contract.get("allowed_capabilities") or [])}
    if allowed and name not in allowed:
        raise ValueError(
            f"verification capability {name!r} is outside the bound node contract"
        )
    obligation = ""
    if name in _RUN_TO_KIND_TAG:
        obligation = _RUN_TO_KIND_TAG[name][1]
    elif name == "op_minion_verification_run_lsp_check":
        obligation = "lsp"
    elif name == "op_minion_verification_check_unavailable":
        obligation = str(args.get("obligation") or "").strip()
    allowed_obligations = {
        str(item) for item in list(contract.get("allowed_obligations") or [])
    }
    if obligation and allowed_obligations and obligation not in allowed_obligations:
        raise ValueError(
            f"verification obligation {obligation!r} is outside the bound node contract; "
            "use only the declared module or scenario entrypoints"
        )


def _require_adapter(adapter: Any | None) -> Any:
    if adapter is None:
        raise ValueError("verification run tool requires scoped execution")
    return adapter


def _ok(call: CanonicalToolCall, text: str, structured: Mapping[str, Any]) -> CanonicalToolResult:
    return CanonicalToolResult(name=call.name, ok=True, text=text, llm_text=text, structured=dict(structured), call_id=call.call_id, status=RuntimeStatus.OK)


def _error(call: CanonicalToolCall, exc: Exception) -> CanonicalToolResult:
    text = f"{exc.__class__.__name__}: {exc}"
    return CanonicalToolResult(name=call.name, ok=False, text=text, llm_text=text + " Correct only this local issue and retry.", structured={"error": str(exc), "error_type": exc.__class__.__name__}, call_id=call.call_id, status=RuntimeStatus.INVALID)
