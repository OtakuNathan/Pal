from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.v2.submission_preflight import (
    bound_reference_payload,
    validate_submission_requirement_refs,
)
from pal.minion.v2.submission_drafts import (
    SubmissionDraftContext,
    SubmissionDraftStore,
    assert_authoring_schema_budget,
)
from pal.minion.v2.skeleton import (
    MODULE_KINDS,
    MODULE_NAME_PATTERN,
    SemanticReferenceError,
    architecture_revision_changed_paths_since,
    semantic_requirements,
    validate_architecture_revision_scope,
    validate_architecture_changed_paths,
    validate_architecture_submission,
)
from pal.minion.workspace_tools import _append_unique_artifact, _write_minion_artifact
from pal.shared import RuntimeStatus


ARCHITECTURE_SKELETON_CAPABILITIES = (
    "op_minion_architecture_module_upsert",
    "op_minion_architecture_module_consume_contract",
    "op_minion_architecture_module_cover_requirement",
    "op_minion_architecture_module_add_reference",
    "op_minion_architecture_module_remove",
    "op_minion_architecture_verification_upsert",
    "op_minion_architecture_verification_consume_contract",
    "op_minion_architecture_verification_cover_requirement",
    "op_minion_architecture_verification_add_entrypoint",
    "op_minion_architecture_verification_set_environment",
    "op_minion_architecture_verification_remove",
    "op_minion_architecture_submit",
)
SKELETON_REVIEW_CAPABILITIES = (
    "op_minion_architecture_review_requirement_audit",
    "op_minion_architecture_review_module_audit",
    "op_minion_architecture_review_verification_audit",
    "op_minion_architecture_review_finding",
    "op_minion_skeleton_review_submit",
)
SKELETON_BUILDER_CAPABILITIES = (*ARCHITECTURE_SKELETON_CAPABILITIES, *SKELETON_REVIEW_CAPABILITIES)

_MODULE_UPSERT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "module_kind": {"type": "string", "enum": ["implementation", "contract_only"]},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "contract_paths": {"type": "array", "items": {"type": "string"}},
        "implementation_files": {"type": "array", "items": {"type": "string"}},
        "implementation_directories": {"type": "array", "items": {"type": "string"}},
        "test_files": {"type": "array", "items": {"type": "string"}},
        "test_directories": {"type": "array", "items": {"type": "string"}},
        "reference_only": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "module_kind", "depends_on", "contract_paths"],
    "additionalProperties": False,
}
_CONSUME_SCHEMA = {
    "type": "object",
    "properties": {
        "consumer": {"type": "string", "minLength": 1},
        "provider": {"type": "string", "minLength": 1},
        "path": {"type": "string", "minLength": 1},
        "symbol": {"type": "string"},
    },
    "required": ["consumer", "provider", "path"],
    "additionalProperties": False,
}
_COVER_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "section": {"type": "string", "minLength": 1},
        "requirement": {"type": "string", "minLength": 1},
    },
    "required": ["name", "section", "requirement"],
    "additionalProperties": False,
}
_REFERENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "module": {"type": "string", "minLength": 1},
        "kind": {
            "type": "string",
            "enum": ["workspace_file", "workspace_symbol", "reference_file", "reference_symbol", "documentation", "research_conclusion"],
        },
        "path": {"type": "string"},
        "symbol": {"type": "string"},
        "reference_name": {"type": "string"},
        "source": {"type": "string"},
        "section": {"type": "string"},
        "conclusion": {"type": "string"},
    },
    "required": ["module", "kind"],
    "additionalProperties": False,
}
_NAME_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string", "minLength": 1}},
    "required": ["name"],
    "additionalProperties": False,
}
_VERIFICATION_UPSERT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "kind": {"type": "string", "enum": ["consumer_probe", "end_to_end", "dogfood", "platform"]},
        "depends_on": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "kind", "depends_on"],
    "additionalProperties": False,
}
_ENTRYPOINT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "kind": {"type": "string", "enum": ["source_symbol", "build_target", "product_entrypoint", "platform_probe"]},
        "path": {"type": "string"},
        "symbol": {"type": "string"},
        "target": {"type": "string"},
    },
    "required": ["name", "kind"],
    "additionalProperties": False,
}
_ENVIRONMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "platform": {"type": "string"},
        "toolchain": {"type": "string"},
        "feature_flags": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": ["name"],
    "additionalProperties": False,
}
_REVIEW_FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "finding_kind": {"type": "string", "enum": ["requirements_defect", "contract_defect", "architecture_defect"]},
        "summary": {"type": "string", "minLength": 1},
        "severity": {"type": "string", "enum": ["error", "warning"]},
        "affected_modules": {"type": "array", "items": {"type": "string"}},
        "requirement_section": {"type": "string"},
        "requirement": {"type": "string"},
        "path": {"type": "string"},
        "symbol": {"type": "string"},
        "contract_section": {"type": "string"},
    },
    "required": ["finding_kind", "summary", "severity", "affected_modules"],
    "additionalProperties": False,
}
_REVIEW_REQUIREMENT_AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "section": {"type": "string", "minLength": 1},
        "requirement": {"type": "string", "minLength": 1},
        "assessment": {"type": "string", "enum": ["supported", "defect"]},
        "modules": {"type": "array", "items": {"type": "string"}},
        "delivery_paths": {"type": "array", "items": {"type": "string"}},
        "verification_nodes": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string", "minLength": 1},
    },
    "required": [
        "section",
        "requirement",
        "assessment",
        "modules",
        "delivery_paths",
        "verification_nodes",
        "rationale",
    ],
    "additionalProperties": False,
}
_REVIEW_MODULE_AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "classification": {"type": "string", "enum": ["sound", "defect"]},
        "dependency_topology": {"type": "string", "enum": ["sound", "defect"]},
        "contract_flow": {"type": "string", "enum": ["complete", "defect"]},
        "ownership_lifecycle_state": {"type": "string", "enum": ["sound", "defect"]},
        "scope": {"type": "string", "enum": ["sufficient", "defect"]},
        "rationale": {"type": "string", "minLength": 1},
    },
    "required": [
        "name",
        "classification",
        "dependency_topology",
        "contract_flow",
        "ownership_lifecycle_state",
        "scope",
        "rationale",
    ],
    "additionalProperties": False,
}
_REVIEW_VERIFICATION_AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "candidate_combination": {"type": "string", "enum": ["sound", "defect"]},
        "contract_consumption": {"type": "string", "enum": ["sound", "defect"]},
        "entrypoint_environment": {"type": "string", "enum": ["sound", "defect"]},
        "requirement_proof": {"type": "string", "enum": ["sound", "defect"]},
        "rationale": {"type": "string", "minLength": 1},
    },
    "required": [
        "name",
        "candidate_combination",
        "contract_consumption",
        "entrypoint_environment",
        "requirement_proof",
        "rationale",
    ],
    "additionalProperties": False,
}
_NO_ARGS_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}

SKELETON_BUILDER_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_minion_architecture_module_upsert": {
        "name": "op_architecture_module_upsert",
        "description": "Create or replace one semantic module shell. contract_paths freeze public shape; implementation/test scopes remain Coder-writable. module_kind is implementation or contract_only.",
        "parameters_schema": _MODULE_UPSERT_SCHEMA,
    },
    "op_minion_architecture_module_consume_contract": {
        "name": "op_architecture_module_consume_contract",
        "description": "Add one directional contract consumption edge from consumer to provider using a real contract path and optional symbol.",
        "parameters_schema": _CONSUME_SCHEMA,
    },
    "op_minion_architecture_module_cover_requirement": {
        "name": "op_architecture_module_cover_requirement",
        "description": "Bind one exact natural-language Requirement to one module.",
        "parameters_schema": _COVER_SCHEMA,
    },
    "op_minion_architecture_module_add_reference": {
        "name": "op_architecture_module_add_reference",
        "description": "Add or replace one readable source reference on a module. A reference with the same path/symbol/section or source/section replaces the old locator, so a correction never requires rebuilding the module. kind is workspace_file, workspace_symbol, reference_file, reference_symbol, documentation, or research_conclusion.",
        "parameters_schema": _REFERENCE_SCHEMA,
    },
    "op_minion_architecture_module_remove": {
        "name": "op_architecture_module_remove",
        "description": "Remove one semantic module during a scoped revision. Dependencies must be corrected before submit.",
        "parameters_schema": _NAME_SCHEMA,
    },
    "op_minion_architecture_verification_upsert": {
        "name": "op_architecture_verification_upsert",
        "description": "Create or replace one real verification scenario. kind is consumer_probe, end_to_end, dogfood, or platform; depends_on names the exact Candidate combination.",
        "parameters_schema": _VERIFICATION_UPSERT_SCHEMA,
    },
    "op_minion_architecture_verification_consume_contract": {
        "name": "op_architecture_verification_consume_contract",
        "description": "Add one real contract consumed by a verification scenario.",
        "parameters_schema": _CONSUME_SCHEMA,
    },
    "op_minion_architecture_verification_cover_requirement": {
        "name": "op_architecture_verification_cover_requirement",
        "description": "Bind one exact Requirement to the real verification scenario that proves it.",
        "parameters_schema": _COVER_SCHEMA,
    },
    "op_minion_architecture_verification_add_entrypoint": {
        "name": "op_architecture_verification_add_entrypoint",
        "description": "Add one real source_symbol, build_target, product_entrypoint, or platform_probe entrypoint to a verification scenario.",
        "parameters_schema": _ENTRYPOINT_SCHEMA,
    },
    "op_minion_architecture_verification_set_environment": {
        "name": "op_architecture_verification_set_environment",
        "description": "Set the concrete platform, toolchain, feature flags, and notes for one verification scenario.",
        "parameters_schema": _ENVIRONMENT_SCHEMA,
    },
    "op_minion_architecture_verification_remove": {
        "name": "op_architecture_verification_remove",
        "description": "Remove one verification scenario during a scoped revision.",
        "parameters_schema": _NAME_SCHEMA,
    },
    "op_minion_architecture_submit": {
        "name": "op_architecture_submit",
        "description": "Preflight and submit the current code skeleton plus semantic Construction/Verification topology. Takes no arguments; correct only reported local defects and retry.",
        "parameters_schema": _NO_ARGS_SCHEMA,
    },
    "op_minion_architecture_review_finding": {
        "name": "op_architecture_review_finding",
        "description": "Record one requirements_defect, contract_defect, or architecture_defect using module names, exact Requirement text, and source location. severity is error or warning.",
        "parameters_schema": _REVIEW_FINDING_SCHEMA,
    },
    "op_minion_architecture_review_requirement_audit": {
        "name": "op_architecture_review_requirement_audit",
        "description": "Audit one exact hard Requirement. assessment is supported or defect. Record whether the claimed modules, writable delivery paths, and Verification Nodes can actually deliver and prove it.",
        "parameters_schema": _REVIEW_REQUIREMENT_AUDIT_SCHEMA,
    },
    "op_minion_architecture_review_module_audit": {
        "name": "op_architecture_review_module_audit",
        "description": "Audit one exact module. classification, dependency_topology, and ownership_lifecycle_state are sound or defect; contract_flow is complete or defect; scope is sufficient or defect.",
        "parameters_schema": _REVIEW_MODULE_AUDIT_SCHEMA,
    },
    "op_minion_architecture_review_verification_audit": {
        "name": "op_architecture_review_verification_audit",
        "description": "Audit one exact Verification Node's Candidate combination, contract consumption, entrypoint/environment, and Requirement proof. Each assessment is sound or defect.",
        "parameters_schema": _REVIEW_VERIFICATION_AUDIT_SCHEMA,
    },
    "op_minion_skeleton_review_submit": {
        "name": "op_architecture_review_submit",
        "description": "Submit only after recording one audit for every hard Requirement, module, and Verification Node. Takes no arguments; Manager validates audit completeness and infers the verdict.",
        "parameters_schema": _NO_ARGS_SCHEMA,
    },
}

for _tool_name, _tool_spec in SKELETON_BUILDER_TOOL_SPECS.items():
    assert_authoring_schema_budget(_tool_spec["parameters_schema"], owner=_tool_name)


def compile_architecture_review_invocation_tool_contract(
    *,
    requirements: Mapping[str, Any],
    architecture: Mapping[str, Any],
) -> dict[str, Any]:
    hard_requirements = [
        {"section": item.section, "requirement": item.requirement}
        for item in semantic_requirements(requirements)
        if item.strength == "hard"
    ]
    modules = dict(architecture.get("modules") or {})
    verification_nodes = dict(architecture.get("verification_nodes") or {})
    requirement_claims = []
    for requirement in hard_requirements:
        key = (requirement["section"], requirement["requirement"])
        requirement_claims.append(
            {
                **requirement,
                "modules": sorted(
                    name
                    for name, raw in modules.items()
                    if key in _requirement_keys(dict(raw).get("covers") or [])
                ),
                "verification_nodes": sorted(
                    name
                    for name, raw in verification_nodes.items()
                    if key in _requirement_keys(dict(raw).get("covers") or [])
                ),
            }
        )
    module_names = sorted(str(name) for name in modules)
    verification_names = sorted(str(name) for name in verification_nodes)
    overrides = {
        "op_minion_architecture_review_requirement_audit": (
            "Record one required hard-Requirement audit. section and requirement must exactly match the bound catalog. "
            "assessment=supported is your semantic judgment, not a Manager-derived result: name only Architect-claimed modules and "
            "Verification Nodes, cite exact declared contract/writable delivery paths, and explain why those concrete boundaries can "
            "deliver and prove the full Requirement. Use assessment=defect when any obligation is omitted, narrowed, unowned, or "
            "unverifiable, then record a typed finding. Bound claims: "
            + json.dumps(requirement_claims, ensure_ascii=False, sort_keys=True)
        ),
        "op_minion_architecture_review_module_audit": (
            "Record one required module audit. Read its code contract and structured architecture entry before judging every field; "
            "contract_flow=complete means all real provided/consumed cross-module data and callback flows are explicitly represented, "
            "not merely implied by prose or depends_on. Any defect assessment requires a typed finding. Exact module names: "
            + json.dumps(module_names, ensure_ascii=False)
        ),
        "op_minion_architecture_review_verification_audit": (
            "Record one required Verification Node audit. A covers entry is only an Architect claim. Judge the exact Candidate "
            "combination, consumed contracts, real entrypoint/environment, and whether this scenario can prove every claimed Requirement. "
            "Any defect assessment requires a typed finding. Exact Verification Node names: "
            + json.dumps(verification_names, ensure_ascii=False)
        ),
        "op_minion_skeleton_review_submit": (
            "Submit only after recording exactly one current audit for each bound hard Requirement, module, and Verification Node. "
            f"Required counts: hard Requirements={len(hard_requirements)}, modules={len(module_names)}, "
            f"Verification Nodes={len(verification_names)}. Missing audits are rejected before worker exit."
        ),
    }
    return {
        "contract_version": "1",
        "hard_requirements": hard_requirements,
        "module_names": module_names,
        "verification_node_names": verification_names,
        "description_overrides": overrides,
    }


def is_skeleton_builder_capability(name: str) -> bool:
    return str(name or "") in SKELETON_BUILDER_TOOL_SPECS


def skeleton_builder_tool_result(
    call: CanonicalToolCall,
    workspace: dict[str, Any],
    produced_artifacts: list[dict[str, Any]],
) -> CanonicalToolResult:
    try:
        name = str(call.name or "")
        if name == "op_minion_architecture_submit":
            output, version = _compile_architecture_submission(call, workspace)
            filename, title, draft_kind = "architecture_submission.json", "V2 architecture skeleton submission", "architecture"
        elif name == "op_minion_skeleton_review_submit":
            output, version = _compile_architecture_review(call, workspace)
            filename, title, draft_kind = "architecture_review.json", "V2 architecture skeleton review", "architecture_review"
        else:
            return _mutate_architecture_draft(call, workspace)
        artifact = _write_minion_artifact(
            workspace,
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
        context = SubmissionDraftContext.from_workspace(workspace, draft_kind=draft_kind)
        SubmissionDraftStore(Path(str(workspace["runtime_root"]))).mark_submitted(
            context,
            expected_version=version,
            submission_payload=output,
        )
        return CanonicalToolResult(
            name=name,
            ok=True,
            text=f"{title} submitted",
            llm_text=f"{title} submitted",
            structured={"submitted": True, "artifact": artifact},
            call_id=call.call_id,
            status=RuntimeStatus.OK,
        )
    except Exception as exc:
        message = f"{exc.__class__.__name__}: {exc}"
        structured = (
            exc.to_dict()
            if isinstance(exc, SemanticReferenceError)
            else {"error": str(exc), "error_type": exc.__class__.__name__}
        )
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=message,
            llm_text=message + " Correct only this local semantic field and retry in the same invocation.",
            structured=structured,
            call_id=call.call_id,
            status=RuntimeStatus.INVALID,
        )


def _mutate_architecture_draft(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
) -> CanonicalToolResult:
    name = str(call.name or "")
    args = dict(call.args or {})
    if name == "op_minion_architecture_review_requirement_audit":
        return _record_architecture_review_requirement_audit(call, workspace)
    if name == "op_minion_architecture_review_module_audit":
        return _record_architecture_review_module_audit(call, workspace)
    if name == "op_minion_architecture_review_verification_audit":
        return _record_architecture_review_verification_audit(call, workspace)
    if name == "op_minion_architecture_review_finding":
        return _record_architecture_review_finding(call, workspace)
    if name not in set(ARCHITECTURE_SKELETON_CAPABILITIES) - {"op_minion_architecture_submit"}:
        raise ValueError(f"unknown architecture authoring capability: {name}")
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind="architecture")
    store = SubmissionDraftStore(Path(str(workspace["runtime_root"])))

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        definitions = dict(payload.get("definitions") or {})
        submission = dict(definitions.get("submission") or {"modules": {}, "verification_nodes": {}})
        modules = dict(submission.get("modules") or {})
        verification_nodes = dict(submission.get("verification_nodes") or {})
        if name == "op_minion_architecture_module_upsert":
            module_name = _semantic_name(args, "name")
            current = dict(modules.get(module_name) or {})
            modules[module_name] = {
                "module_kind": str(args.get("module_kind") or ""),
                "depends_on": _string_array(args.get("depends_on") or [], owner="depends_on"),
                "consumes": list(current.get("consumes") or []),
                "paths": {
                    "contract_paths": _string_array(args.get("contract_paths") or [], owner="contract_paths"),
                    "implementation_scopes": _path_scopes(args, "implementation"),
                    "test_scopes": _path_scopes(args, "test"),
                    "reference_only": _string_array(args.get("reference_only") or [], owner="reference_only"),
                },
                "covers": list(current.get("covers") or []),
                "evidence": list(current.get("evidence") or []),
            }
        elif name == "op_minion_architecture_module_consume_contract":
            consumer = _semantic_name(args, "consumer")
            module = _required_named_item(modules, consumer, owner="module")
            _append_unique_semantic(
                module,
                "consumes",
                {
                    "module": _semantic_name(args, "provider"),
                    "path": str(args.get("path") or "").strip(),
                    **({"symbol": str(args.get("symbol"))} if str(args.get("symbol") or "").strip() else {}),
                },
            )
            modules[consumer] = module
        elif name == "op_minion_architecture_module_cover_requirement":
            module_name = _semantic_name(args, "name")
            module = _required_named_item(modules, module_name, owner="module")
            _append_unique_semantic(module, "covers", _requirement_ref(args))
            modules[module_name] = module
        elif name == "op_minion_architecture_module_add_reference":
            module_name = _semantic_name(args, "module")
            module = _required_named_item(modules, module_name, owner="module")
            evidence = {key: value for key, value in args.items() if key != "module" and value not in (None, "")}
            _upsert_semantic_reference(module, evidence)
            modules[module_name] = module
        elif name == "op_minion_architecture_module_remove":
            module_name = _semantic_name(args, "name")
            if module_name not in modules:
                raise ValueError(f"unknown module: {module_name}")
            del modules[module_name]
        elif name == "op_minion_architecture_verification_upsert":
            node_name = _semantic_name(args, "name")
            current = dict(verification_nodes.get(node_name) or {})
            verification_nodes[node_name] = {
                "kind": str(args.get("kind") or ""),
                "depends_on": _string_array(args.get("depends_on") or [], owner="depends_on"),
                "consumes": list(current.get("consumes") or []),
                "covers": list(current.get("covers") or []),
                "entrypoints": list(current.get("entrypoints") or []),
                "environment": dict(current.get("environment") or {}),
            }
        elif name == "op_minion_architecture_verification_consume_contract":
            node_name = _semantic_name(args, "consumer")
            node = _required_named_item(verification_nodes, node_name, owner="Verification Node")
            _append_unique_semantic(
                node,
                "consumes",
                {
                    "module": _semantic_name(args, "provider"),
                    "path": str(args.get("path") or "").strip(),
                    **({"symbol": str(args.get("symbol"))} if str(args.get("symbol") or "").strip() else {}),
                },
            )
            verification_nodes[node_name] = node
        elif name == "op_minion_architecture_verification_cover_requirement":
            node_name = _semantic_name(args, "name")
            node = _required_named_item(verification_nodes, node_name, owner="Verification Node")
            _append_unique_semantic(node, "covers", _requirement_ref(args))
            verification_nodes[node_name] = node
        elif name == "op_minion_architecture_verification_add_entrypoint":
            node_name = _semantic_name(args, "name")
            node = _required_named_item(verification_nodes, node_name, owner="Verification Node")
            entrypoint = {key: value for key, value in args.items() if key != "name" and value not in (None, "")}
            _append_unique_semantic(node, "entrypoints", entrypoint)
            verification_nodes[node_name] = node
        elif name == "op_minion_architecture_verification_set_environment":
            node_name = _semantic_name(args, "name")
            node = _required_named_item(verification_nodes, node_name, owner="Verification Node")
            node["environment"] = {
                key: value
                for key, value in args.items()
                if key != "name" and value not in (None, "", [])
            }
            verification_nodes[node_name] = node
        elif name == "op_minion_architecture_verification_remove":
            node_name = _semantic_name(args, "name")
            if node_name not in verification_nodes:
                raise ValueError(f"unknown Verification Node: {node_name}")
            del verification_nodes[node_name]
        submission["modules"] = modules
        submission["verification_nodes"] = verification_nodes
        definitions["submission"] = submission
        payload["definitions"] = definitions
        return payload, {
            "updated": True,
            "module_count": len(modules),
            "verification_node_count": len(verification_nodes),
        }

    result = store.mutate(
        context,
        operation_key=str(call.call_id or f"{name}:{json.dumps(args, sort_keys=True)}"),
        request=args,
        reducer=reducer,
        seed=_architecture_seed(workspace),
    )
    return CanonicalToolResult(
        name=call.name,
        ok=True,
        text="architecture Draft updated",
        llm_text="Architecture Draft updated. Continue with the next semantic unit; submit only after the skeleton and topology are complete.",
        structured=dict(result),
        call_id=call.call_id,
        status=RuntimeStatus.OK,
    )


def _compile_architecture_submission(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
) -> tuple[dict[str, Any], int]:
    if dict(call.args or {}):
        raise ValueError("architecture_submit takes no arguments")
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind="architecture")
    snapshot = SubmissionDraftStore(Path(str(workspace["runtime_root"]))).read(
        context,
        seed=_architecture_seed(workspace),
    )
    output = dict(dict(snapshot.payload.get("definitions") or {}).get("submission") or {})
    _validate_submission_shape(output)
    _preflight_submission(output, workspace)
    if workspace.get("architecture_revision_base_submission"):
        base = dict(workspace.get("architecture_revision_base_submission") or {})
        changed_paths = _architecture_revision_changed_paths(workspace)
        if output == base and not changed_paths:
            raise ValueError("architecture revision makes no source or semantic change")
        _validate_revision_scope(output, workspace, changed_paths=changed_paths)
    return output, snapshot.version


def _record_architecture_review_requirement_audit(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
) -> CanonicalToolResult:
    args = dict(call.args or {})
    audit = {
        "section": str(args.get("section") or "").strip(),
        "requirement": str(args.get("requirement") or "").strip(),
        "assessment": str(args.get("assessment") or "").strip(),
        "modules": _unique_strings(args.get("modules") or [], owner="modules"),
        "delivery_paths": _unique_strings(args.get("delivery_paths") or [], owner="delivery_paths"),
        "verification_nodes": _unique_strings(
            args.get("verification_nodes") or [], owner="verification_nodes"
        ),
        "rationale": str(args.get("rationale") or "").strip(),
    }
    _validate_requirement_audit(audit, workspace)
    key = _requirement_audit_key(audit["section"], audit["requirement"])
    return _record_review_audit(
        call,
        workspace,
        category="requirement_audits",
        key=key,
        audit=audit,
        label="Requirement audit",
    )


def _record_architecture_review_module_audit(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
) -> CanonicalToolResult:
    args = dict(call.args or {})
    audit = {
        "name": str(args.get("name") or "").strip(),
        "classification": str(args.get("classification") or "").strip(),
        "dependency_topology": str(args.get("dependency_topology") or "").strip(),
        "contract_flow": str(args.get("contract_flow") or "").strip(),
        "ownership_lifecycle_state": str(args.get("ownership_lifecycle_state") or "").strip(),
        "scope": str(args.get("scope") or "").strip(),
        "rationale": str(args.get("rationale") or "").strip(),
    }
    _validate_module_audit(audit, workspace)
    return _record_review_audit(
        call,
        workspace,
        category="module_audits",
        key=audit["name"],
        audit=audit,
        label="Module audit",
    )


def _record_architecture_review_verification_audit(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
) -> CanonicalToolResult:
    args = dict(call.args or {})
    audit = {
        "name": str(args.get("name") or "").strip(),
        "candidate_combination": str(args.get("candidate_combination") or "").strip(),
        "contract_consumption": str(args.get("contract_consumption") or "").strip(),
        "entrypoint_environment": str(args.get("entrypoint_environment") or "").strip(),
        "requirement_proof": str(args.get("requirement_proof") or "").strip(),
        "rationale": str(args.get("rationale") or "").strip(),
    }
    _validate_verification_audit(audit, workspace)
    return _record_review_audit(
        call,
        workspace,
        category="verification_audits",
        key=audit["name"],
        audit=audit,
        label="Verification audit",
    )


def _record_review_audit(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
    *,
    category: str,
    key: str,
    audit: Mapping[str, Any],
    label: str,
) -> CanonicalToolResult:
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind="architecture_review")
    store = SubmissionDraftStore(Path(str(workspace["runtime_root"])))

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        evidence = dict(payload.get("evidence") or {})
        audits = dict(evidence.get(category) or {})
        audits[key] = dict(audit)
        evidence[category] = audits
        payload["evidence"] = evidence
        return payload, {"recorded": True, "audit_count": len(audits)}

    result = store.mutate(
        context,
        operation_key=str(call.call_id or f"{category}:{key}"),
        request=dict(call.args or {}),
        reducer=reducer,
        seed=_architecture_review_seed(),
    )
    return CanonicalToolResult(
        name=call.name,
        ok=True,
        text=f"{label} recorded",
        llm_text=f"{label} recorded. Continue until every bound item has one current audit.",
        structured=dict(result),
        call_id=call.call_id,
        status=RuntimeStatus.OK,
    )


def _record_architecture_review_finding(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
) -> CanonicalToolResult:
    args = dict(call.args or {})
    finding = {
        "finding_kind": str(args.get("finding_kind") or ""),
        "summary": str(args.get("summary") or "").strip(),
        "severity": str(args.get("severity") or ""),
        "affected_modules": _string_array(args.get("affected_modules") or [], owner="affected_modules"),
        "requirements": (
            [_requirement_ref({"section": args.get("requirement_section"), "requirement": args.get("requirement")})]
            if str(args.get("requirement_section") or "").strip() or str(args.get("requirement") or "").strip()
            else []
        ),
        "locations": (
            [
                {
                    "path": str(args.get("path") or "").strip(),
                    **({"symbol": str(args.get("symbol"))} if str(args.get("symbol") or "").strip() else {}),
                    **({"section": str(args.get("contract_section"))} if str(args.get("contract_section") or "").strip() else {}),
                }
            ]
            if str(args.get("path") or "").strip()
            else []
        ),
    }
    if not finding["summary"]:
        raise ValueError("architecture review finding requires summary")
    if not finding["requirements"] and not finding["locations"]:
        raise ValueError("architecture review finding requires exact Requirement text or a source location")
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind="architecture_review")
    store = SubmissionDraftStore(Path(str(workspace["runtime_root"])))

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        findings = [dict(item) for item in list(payload.get("findings") or [])]
        findings.append(finding)
        payload["findings"] = findings
        return payload, {"recorded": True, "finding_count": len(findings)}

    result = store.mutate(
        context,
        operation_key=str(call.call_id or f"review-finding:{finding['summary']}"),
        request=args,
        reducer=reducer,
        seed=_architecture_review_seed(),
    )
    return CanonicalToolResult(
        name=call.name,
        ok=True,
        text="architecture review finding recorded",
        llm_text="Finding recorded. Continue breadth-first review, then call architecture_review_submit with no arguments.",
        structured=dict(result),
        call_id=call.call_id,
        status=RuntimeStatus.OK,
    )


def _compile_architecture_review(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
) -> tuple[dict[str, Any], int]:
    if dict(call.args or {}):
        raise ValueError("architecture_review_submit takes no arguments")
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind="architecture_review")
    snapshot = SubmissionDraftStore(Path(str(workspace["runtime_root"]))).read(
        context,
        seed=_architecture_review_seed(),
    )
    findings = [dict(item) for item in list(snapshot.payload.get("findings") or [])]
    audit = _compiled_review_audit(snapshot.payload)
    output = {
        "verdict": "FAIL" if findings else "PASS",
        "findings": findings,
        "audit": audit,
    }
    _validate_review_shape(output)
    _preflight_review_submission(output, workspace)
    return output, snapshot.version


def _architecture_review_seed() -> dict[str, Any]:
    return {
        "definitions": {},
        "evidence": {
            "requirement_audits": {},
            "module_audits": {},
            "verification_audits": {},
        },
        "findings": [],
        "summary": {},
    }


def _architecture_seed(workspace: Mapping[str, Any]) -> dict[str, Any]:
    base = workspace.get("architecture_revision_base_submission")
    submission = json.loads(json.dumps(dict(base))) if isinstance(base, Mapping) else {"modules": {}, "verification_nodes": {}}
    return {"definitions": {"submission": submission}, "evidence": {}, "findings": [], "summary": {}}


def _path_scopes(args: Mapping[str, Any], prefix: str) -> list[dict[str, str]]:
    result = [
        {"kind": "file", "path": value}
        for value in _string_array(args.get(f"{prefix}_files") or [], owner=f"{prefix}_files")
    ]
    result.extend(
        {"kind": "directory", "path": value}
        for value in _string_array(args.get(f"{prefix}_directories") or [], owner=f"{prefix}_directories")
    )
    return result


def _semantic_name(args: Mapping[str, Any], field: str) -> str:
    value = str(args.get(field) or "").strip()
    if MODULE_NAME_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be a stable snake_case semantic name")
    return value


def _string_array(value: Any, *, owner: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{owner} must be a string array")
    return [str(item) for item in value]


def _unique_strings(value: Any, *, owner: str) -> list[str]:
    values = [item.strip() for item in _string_array(value, owner=owner)]
    if any(not item for item in values):
        raise ValueError(f"{owner} must not contain empty values")
    return list(dict.fromkeys(values))


def _requirement_keys(value: Any) -> set[tuple[str, str]]:
    return {
        (
            str(dict(item or {}).get("section") or "").strip(),
            str(dict(item or {}).get("requirement") or "").strip(),
        )
        for item in list(value or [])
        if isinstance(item, Mapping)
    }


def _requirement_audit_key(section: str, requirement: str) -> str:
    return json.dumps([str(section), str(requirement)], ensure_ascii=False, separators=(",", ":"))


def _review_bound_inputs(workspace: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        bound_reference_payload(workspace, "requirements"),
        bound_reference_payload(workspace, "architecture_index"),
    )


def _validate_requirement_audit(audit: Mapping[str, Any], workspace: Mapping[str, Any]) -> None:
    requirements, architecture = _review_bound_inputs(workspace)
    key = (str(audit.get("section") or ""), str(audit.get("requirement") or ""))
    hard = {
        (item.section, item.requirement)
        for item in semantic_requirements(requirements)
        if item.strength == "hard"
    }
    if key not in hard:
        allowed = "; ".join(f"{section}: {requirement}" for section, requirement in sorted(hard))
        raise ValueError(
            f"Requirement audit must use exact bound hard Requirement text: {key[0]}: {key[1]}. "
            f"Allowed: {allowed or '<none>'}"
        )
    assessment = str(audit.get("assessment") or "")
    if assessment not in {"supported", "defect"}:
        raise ValueError("Requirement audit assessment must be supported or defect")
    if not str(audit.get("rationale") or "").strip():
        raise ValueError("Requirement audit requires a semantic rationale")
    modules = dict(architecture.get("modules") or {})
    verification_nodes = dict(architecture.get("verification_nodes") or {})
    selected_modules = set(str(item) for item in list(audit.get("modules") or []))
    selected_nodes = set(str(item) for item in list(audit.get("verification_nodes") or []))
    unknown_modules = sorted(selected_modules - set(modules))
    unknown_nodes = sorted(selected_nodes - set(verification_nodes))
    if unknown_modules:
        raise ValueError("Requirement audit references unknown modules: " + ", ".join(unknown_modules))
    if unknown_nodes:
        raise ValueError(
            "Requirement audit references unknown Verification Nodes: " + ", ".join(unknown_nodes)
        )
    allowed_paths = {
        path
        for name in selected_modules
        for path in _module_review_paths(dict(modules[name] or {}))
    }
    selected_paths = set(str(item) for item in list(audit.get("delivery_paths") or []))
    unknown_paths = sorted(selected_paths - allowed_paths)
    if unknown_paths:
        raise ValueError(
            "Requirement audit delivery_paths must belong to the selected modules: "
            + ", ".join(unknown_paths)
        )
    if assessment == "supported":
        claimed_modules = {
            str(name)
            for name, raw in modules.items()
            if key in _requirement_keys(dict(raw or {}).get("covers") or [])
        }
        claimed_nodes = {
            str(name)
            for name, raw in verification_nodes.items()
            if key in _requirement_keys(dict(raw or {}).get("covers") or [])
        }
        if claimed_modules and not selected_modules:
            raise ValueError("Supported Requirement audit must name at least one claiming delivery module")
        if selected_modules - claimed_modules:
            raise ValueError(
                "Supported Requirement audit selected modules that do not claim this Requirement: "
                + ", ".join(sorted(selected_modules - claimed_modules))
            )
        if not selected_nodes:
            raise ValueError("Supported Requirement audit must name at least one proving Verification Node")
        if selected_nodes - claimed_nodes:
            raise ValueError(
                "Supported Requirement audit selected Verification Nodes that do not claim this Requirement: "
                + ", ".join(sorted(selected_nodes - claimed_nodes))
            )
        if selected_modules and not selected_paths:
            raise ValueError("Supported Requirement audit must cite a declared delivery path")


def _module_review_paths(module: Mapping[str, Any]) -> set[str]:
    paths = dict(module.get("paths") or {})
    return {
        str(item)
        for item in list(paths.get("contract_paths") or [])
        if str(item).strip()
    } | {
        str(dict(item or {}).get("path") or "")
        for field in ("implementation_scopes", "test_scopes")
        for item in list(paths.get(field) or [])
        if isinstance(item, Mapping) and str(dict(item).get("path") or "").strip()
    }


def _validate_module_audit(audit: Mapping[str, Any], workspace: Mapping[str, Any]) -> None:
    _, architecture = _review_bound_inputs(workspace)
    modules = dict(architecture.get("modules") or {})
    name = str(audit.get("name") or "")
    if name not in modules:
        raise ValueError(
            f"Module audit references unknown module: {name}. Allowed: {', '.join(sorted(modules))}"
        )
    allowed = {
        "classification": {"sound", "defect"},
        "dependency_topology": {"sound", "defect"},
        "contract_flow": {"complete", "defect"},
        "ownership_lifecycle_state": {"sound", "defect"},
        "scope": {"sufficient", "defect"},
    }
    for field, values in allowed.items():
        if str(audit.get(field) or "") not in values:
            raise ValueError(f"Module audit {field} must be one of: {', '.join(sorted(values))}")
    if not str(audit.get("rationale") or "").strip():
        raise ValueError("Module audit requires a semantic rationale")


def _validate_verification_audit(audit: Mapping[str, Any], workspace: Mapping[str, Any]) -> None:
    _, architecture = _review_bound_inputs(workspace)
    nodes = dict(architecture.get("verification_nodes") or {})
    name = str(audit.get("name") or "")
    if name not in nodes:
        raise ValueError(
            f"Verification audit references unknown node: {name}. Allowed: {', '.join(sorted(nodes))}"
        )
    for field in (
        "candidate_combination",
        "contract_consumption",
        "entrypoint_environment",
        "requirement_proof",
    ):
        if str(audit.get(field) or "") not in {"sound", "defect"}:
            raise ValueError(f"Verification audit {field} must be sound or defect")
    if not str(audit.get("rationale") or "").strip():
        raise ValueError("Verification audit requires a semantic rationale")


def _compiled_review_audit(payload: Mapping[str, Any]) -> dict[str, Any]:
    evidence = dict(payload.get("evidence") or {})
    return {
        "requirements": sorted(
            (dict(item) for item in dict(evidence.get("requirement_audits") or {}).values()),
            key=lambda item: (str(item.get("section") or ""), str(item.get("requirement") or "")),
        ),
        "modules": sorted(
            (dict(item) for item in dict(evidence.get("module_audits") or {}).values()),
            key=lambda item: str(item.get("name") or ""),
        ),
        "verification_nodes": sorted(
            (dict(item) for item in dict(evidence.get("verification_audits") or {}).values()),
            key=lambda item: str(item.get("name") or ""),
        ),
    }


def _required_named_item(values: Mapping[str, Any], name: str, *, owner: str) -> dict[str, Any]:
    if name not in values:
        raise ValueError(f"unknown {owner}: {name}")
    return dict(values[name] or {})


def _append_unique_semantic(owner: dict[str, Any], field: str, value: Mapping[str, Any]) -> None:
    values = [dict(item) for item in list(owner.get(field) or [])]
    encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True)
    if all(json.dumps(item, ensure_ascii=False, sort_keys=True) != encoded for item in values):
        values.append(dict(value))
    owner[field] = values


def _upsert_semantic_reference(owner: dict[str, Any], value: Mapping[str, Any]) -> None:
    values = [dict(item) for item in list(owner.get("evidence") or [])]
    locator = _semantic_reference_locator(value)
    if locator is not None:
        for index, current in enumerate(values):
            if _semantic_reference_locator(current) == locator:
                values[index] = dict(value)
                owner["evidence"] = values
                return
    encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True)
    if all(json.dumps(item, ensure_ascii=False, sort_keys=True) != encoded for item in values):
        values.append(dict(value))
    owner["evidence"] = values


def _semantic_reference_locator(value: Mapping[str, Any]) -> tuple[str, ...] | None:
    path = str(value.get("path") or "").strip()
    if path:
        return (
            "path",
            path,
            str(value.get("symbol") or "").strip(),
            str(value.get("section") or "").strip(),
        )
    source = str(value.get("source") or "").strip()
    if source:
        return (
            "source",
            source,
            str(value.get("section") or "").strip(),
        )
    return None


def _requirement_ref(args: Mapping[str, Any]) -> dict[str, str]:
    section = str(args.get("section") or "").strip()
    requirement = str(args.get("requirement") or "").strip()
    if not section or not requirement:
        raise ValueError("Requirement reference requires section and exact requirement text")
    return {"section": section, "requirement": requirement}


def _validate_submission_shape(payload: Mapping[str, Any]) -> None:
    modules = payload.get("modules")
    if not isinstance(modules, Mapping) or not modules:
        raise ValueError("modules must be a non-empty map")
    names = {str(name) for name in modules}
    for name, raw_module in modules.items():
        if MODULE_NAME_PATTERN.fullmatch(str(name)) is None:
            raise ValueError(f"invalid semantic module name: {name}")
        module = dict(raw_module or {})
        missing = {"module_kind", "depends_on", "consumes", "paths", "covers", "evidence"} - set(module)
        if missing:
            raise ValueError(f"module {name} is missing: {', '.join(sorted(missing))}")
        unknown = set(str(item) for item in list(module.get("depends_on") or [])) - names
        if unknown:
            raise ValueError(f"module {name} references unknown dependencies: {', '.join(sorted(unknown))}")
        paths = dict(module.get("paths") or {})
        if not list(paths.get("contract_paths") or []):
            raise ValueError(f"module {name} requires paths.contract_paths")
        module_kind = str(module.get("module_kind") or "")
        if module_kind not in MODULE_KINDS:
            raise ValueError(f"module {name} has invalid module_kind: {module_kind or '<empty>'}")
        writable = [
            *list(paths.get("implementation_scopes") or []),
            *list(paths.get("test_scopes") or []),
        ]
        if module_kind == "implementation":
            for field in ("implementation_scopes", "test_scopes"):
                if not list(paths.get(field) or []):
                    raise ValueError(f"implementation module {name} requires paths.{field}")
        elif writable:
            raise ValueError(
                f"contract_only module {name} cannot declare implementation_scopes or test_scopes"
            )
    _assert_acyclic({str(name): [str(item) for item in list(dict(module).get("depends_on") or [])] for name, module in modules.items()})
    verification_nodes = payload.get("verification_nodes")
    if not isinstance(verification_nodes, Mapping) or not verification_nodes:
        raise ValueError("verification_nodes must be a non-empty map")


def _validate_revision_scope(
    merged: Mapping[str, Any],
    workspace: Mapping[str, Any],
    *,
    changed_paths: Sequence[str] | None = None,
) -> None:
    base = dict(workspace.get("architecture_revision_base_submission") or {})
    scope = dict(workspace.get("architecture_revision_scope") or {})
    if not base:
        raise ValueError("architecture revision has no bound base submission")
    if not scope:
        return
    if changed_paths is None:
        changed_paths = _architecture_revision_changed_paths(workspace)
    validate_architecture_revision_scope(
        base_submission=base,
        revised_submission=merged,
        changed_paths=changed_paths,
        scope=scope,
    )


def _architecture_revision_changed_paths(
    workspace: Mapping[str, Any],
) -> tuple[str, ...]:
    repo_path = Path(str(workspace.get("repo_path") or "")).expanduser()
    base_sha = str(workspace.get("architecture_revision_base_sha") or "").strip()
    if not base_sha or not repo_path.is_dir():
        return ()
    baseline_states = workspace.get("architecture_revision_base_path_states")
    if isinstance(baseline_states, Mapping):
        return architecture_revision_changed_paths_since(
            repo_path,
            base_sha,
            {str(path): str(value) for path, value in baseline_states.items()},
        )
    return _revision_changed_paths(repo_path, base_sha)


def _revision_changed_paths(repo_path: Path, base_sha: str) -> tuple[str, ...]:
    changed = subprocess.run(
        ["git", "-C", str(repo_path), "diff", "--name-only", "--no-renames", base_sha, "--"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if changed.returncode != 0:
        raise ValueError(changed.stderr.strip() or "cannot inspect architecture revision diff")
    untracked = subprocess.run(
        ["git", "-C", str(repo_path), "ls-files", "--others", "--exclude-standard"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if untracked.returncode != 0:
        raise ValueError(untracked.stderr.strip() or "cannot inspect architecture revision additions")
    return tuple(
        sorted(
            {
                *(line.strip() for line in changed.stdout.splitlines() if line.strip()),
                *(line.strip() for line in untracked.stdout.splitlines() if line.strip()),
            }
        )
    )


def _preflight_submission(payload: Mapping[str, Any], workspace: Mapping[str, Any]) -> None:
    references = {
        str(item.get("name") or ""): Path(str(item.get("path") or ""))
        for item in list(workspace.get("reference_paths") or [])
        if str(item.get("name") or "")
        and str(item.get("path") or "")
        and not bool(item.get("bound_input"))
    }
    requirements = bound_reference_payload(workspace, "requirements")
    evidence = bound_reference_payload(workspace, "evidence_catalog", required=False) or None
    repo_path = Path(str(workspace.get("repo_path") or "")).expanduser()
    if not repo_path.is_dir():
        raise ValueError("architecture worktree is unavailable for architecture preflight")
    normalized = validate_architecture_submission(
        payload,
        requirements_payload=requirements,
        workspace_root=repo_path,
        reference_roots=references,
        evidence_catalog=evidence,
    )
    base_sha = str(workspace.get("architecture_base_sha") or "").strip()
    if base_sha:
        head = _git_output(repo_path, "rev-parse", "HEAD")
        if head != base_sha:
            raise ValueError(
                "Architect changed Git HEAD; commits, merges, rebases, checkouts, and resets are manager-owned operations"
            )
        validate_architecture_changed_paths(
            normalized,
            _revision_changed_paths(repo_path, base_sha),
        )


def _git_output(repo_path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(completed.stderr.strip() or f"cannot run git {' '.join(args)}")
    return completed.stdout.strip()


def _validate_review_shape(payload: Mapping[str, Any]) -> None:
    verdict = str(payload.get("verdict") or "")
    findings = list(payload.get("findings") or [])
    if verdict not in {"PASS", "FAIL"}:
        raise ValueError("verdict must be PASS or FAIL")
    if verdict == "PASS" and findings:
        raise ValueError("PASS cannot contain findings")
    if verdict == "FAIL" and not findings:
        raise ValueError("FAIL requires findings")
    for index, raw in enumerate(findings):
        finding = dict(raw or {})
        if not str(finding.get("summary") or "").strip():
            raise ValueError(f"finding {index} requires summary")
        if str(finding.get("finding_kind") or "") not in {
            "requirements_defect",
            "contract_defect",
            "architecture_defect",
        }:
            raise ValueError(f"finding {index} has invalid finding_kind")


def _preflight_review_submission(
    payload: Mapping[str, Any],
    workspace: Mapping[str, Any],
) -> None:
    requirements = bound_reference_payload(workspace, "requirements", required=False)
    if requirements:
        validate_submission_requirement_refs(
            payload,
            work_view={"requirements": requirements},
            owner="Architecture Reviewer submission",
        )
    architecture = bound_reference_payload(workspace, "architecture_index", required=False)
    if not architecture:
        return
    known_modules = set(str(name) for name in dict(architecture.get("modules") or {}))
    for index, raw in enumerate(list(payload.get("findings") or [])):
        finding = dict(raw or {})
        unknown = sorted(
            str(name)
            for name in list(finding.get("affected_modules") or [])
            if str(name) not in known_modules
        )
        if unknown:
            raise ValueError(
                f"architecture review finding {index} references unknown modules: {', '.join(unknown)}. "
                f"Allowed exact module names: {', '.join(sorted(known_modules))}"
            )
    audit = payload.get("audit")
    if not isinstance(audit, Mapping):
        raise ValueError("architecture review requires the bound Requirement, module, and Verification audits")
    requirement_audits = [dict(item or {}) for item in list(audit.get("requirements") or [])]
    module_audits = [dict(item or {}) for item in list(audit.get("modules") or [])]
    verification_audits = [
        dict(item or {}) for item in list(audit.get("verification_nodes") or [])
    ]
    for item in requirement_audits:
        _validate_requirement_audit(item, workspace)
    for item in module_audits:
        _validate_module_audit(item, workspace)
    for item in verification_audits:
        _validate_verification_audit(item, workspace)
    expected_requirements = {
        (item.section, item.requirement)
        for item in semantic_requirements(requirements)
        if item.strength == "hard"
    }
    actual_requirements = {
        (str(item.get("section") or ""), str(item.get("requirement") or ""))
        for item in requirement_audits
    }
    _assert_exact_review_audit_coverage(
        owner="hard Requirements",
        expected={f"{section}: {requirement}" for section, requirement in expected_requirements},
        actual={f"{section}: {requirement}" for section, requirement in actual_requirements},
        item_count=len(requirement_audits),
    )
    _assert_exact_review_audit_coverage(
        owner="modules",
        expected=known_modules,
        actual={str(item.get("name") or "") for item in module_audits},
        item_count=len(module_audits),
    )
    known_verification = set(str(name) for name in dict(architecture.get("verification_nodes") or {}))
    _assert_exact_review_audit_coverage(
        owner="Verification Nodes",
        expected=known_verification,
        actual={str(item.get("name") or "") for item in verification_audits},
        item_count=len(verification_audits),
    )
    has_defect = _review_audit_has_defect(
        requirement_audits,
        module_audits,
        verification_audits,
    )
    findings = list(payload.get("findings") or [])
    if has_defect and not findings:
        raise ValueError("architecture review audits record defects but no typed finding was recorded")
    if findings and not has_defect:
        raise ValueError("architecture review findings require at least one corresponding defect assessment")


def _assert_exact_review_audit_coverage(
    *,
    owner: str,
    expected: set[str],
    actual: set[str],
    item_count: int,
) -> None:
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing:
        raise ValueError(f"architecture review is missing {owner} audits: " + "; ".join(missing))
    if unexpected:
        raise ValueError(f"architecture review has unknown {owner} audits: " + "; ".join(unexpected))
    if item_count != len(actual):
        raise ValueError(f"architecture review must record exactly one current audit per {owner} item")


def _review_audit_has_defect(
    requirement_audits: list[Mapping[str, Any]],
    module_audits: list[Mapping[str, Any]],
    verification_audits: list[Mapping[str, Any]],
) -> bool:
    return any(str(item.get("assessment") or "") == "defect" for item in requirement_audits) or any(
        "defect"
        in {
            str(item.get("classification") or ""),
            str(item.get("dependency_topology") or ""),
            str(item.get("contract_flow") or ""),
            str(item.get("ownership_lifecycle_state") or ""),
            str(item.get("scope") or ""),
        }
        for item in module_audits
    ) or any(
        "defect"
        in {
            str(item.get("candidate_combination") or ""),
            str(item.get("contract_consumption") or ""),
            str(item.get("entrypoint_environment") or ""),
            str(item.get("requirement_proof") or ""),
        }
        for item in verification_audits
    )


def _assert_acyclic(depends_on: Mapping[str, list[str]]) -> None:
    pending = {name: set(values) for name, values in depends_on.items()}
    while pending:
        ready = {name for name, values in pending.items() if not values}
        if not ready:
            raise ValueError("module dependency graph contains a cycle: " + ", ".join(sorted(pending)))
        pending = {name: values - ready for name, values in pending.items() if name not in ready}
