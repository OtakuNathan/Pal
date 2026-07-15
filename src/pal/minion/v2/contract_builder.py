from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.v2.architecture import (
    ComplexityBudgetPolicy,
    contract_revision_changes,
    normalize_revision_targets,
    revision_target_allows,
    validate_requirements_artifact,
    validate_unit_contract,
)
from pal.minion.v2.submission_drafts import (
    SubmissionDraftContext,
    SubmissionDraftStore,
    assert_authoring_schema_budget,
)
from pal.minion.workspace_tools import _append_unique_artifact, _write_minion_artifact
from pal.shared import RuntimeStatus


REQUIREMENTS_BUILDER_CAPABILITIES = (
    "op_minion_requirement_upsert",
    "op_minion_requirement_remove",
    "op_minion_requirement_add_clarification",
    "op_minion_requirement_add_source_coverage",
    "op_minion_requirements_submit",
)

CONTRACT_SKETCH_BUILDER_CAPABILITIES = (
    "op_minion_contract_unit_upsert",
    "op_minion_contract_unit_add_interface",
    "op_minion_contract_unit_set_ownership",
    "op_minion_contract_unit_set_lifecycle",
    "op_minion_contract_unit_set_state",
    "op_minion_contract_unit_add_rule",
    "op_minion_contract_unit_set_complexity",
    "op_minion_contract_unit_cover_requirement",
    "op_minion_contract_unit_remove",
    "op_minion_contract_add_constraint",
    "op_minion_contract_add_design_decision",
    "op_minion_contract_add_gate_check",
    "op_minion_contract_add_cross_unit_contract",
    "op_minion_contract_set_integration",
    "op_minion_contract_add_assumption",
    "op_minion_contract_add_risk",
    "op_minion_contract_submit",
)

REVISION_CONTRACT_BUILDER_CAPABILITIES: tuple[str, ...] = ()
ARCHITECTURE_REVIEW_BUILDER_CAPABILITIES = (
    "op_minion_contract_review_finding",
    "op_minion_architecture_review_submit",
)
CONTRACT_BUILDER_CAPABILITIES = (
    *REQUIREMENTS_BUILDER_CAPABILITIES,
    *CONTRACT_SKETCH_BUILDER_CAPABILITIES,
    *ARCHITECTURE_REVIEW_BUILDER_CAPABILITIES,
)
ARCHITECT_BUILDER_CAPABILITIES = CONTRACT_SKETCH_BUILDER_CAPABILITIES


def _schema(properties: Mapping[str, Any], *, required: tuple[str, ...] = ()) -> dict[str, Any]:
    value: dict[str, Any] = {
        "type": "object",
        "properties": dict(properties),
        "additionalProperties": False,
    }
    if required:
        value["required"] = list(required)
    return value


_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}
_NO_ARGS = _schema({})
_REQUIREMENT_UPSERT = _schema(
    {
        "section": {"type": "string", "minLength": 1},
        "statement": {"type": "string", "minLength": 1},
        "strength": {"type": "string", "enum": ["hard", "soft"]},
        "source_refs": _STRING_ARRAY,
        "acceptance_semantics": {"type": "string"},
        "ambiguities": _STRING_ARRAY,
    },
    required=("section", "statement", "strength"),
)
_REQUIREMENT_REF = _schema(
    {
        "section": {"type": "string", "minLength": 1},
        "requirement": {"type": "string", "minLength": 1},
    },
    required=("section", "requirement"),
)
_TEXT = _schema({"text": {"type": "string", "minLength": 1}}, required=("text",))
_UNIT_UPSERT = _schema(
    {
        "name": {"type": "string", "minLength": 1},
        "behavior_kind": {"type": "string", "enum": ["stateless", "resource_owner", "service", "workflow", "adapter"]},
        "responsibility": {"type": "string", "minLength": 1},
        "owned_area": _STRING_ARRAY,
        "reference_only_paths": _STRING_ARRAY,
        "depends_on": _STRING_ARRAY,
    },
    required=("name", "behavior_kind", "responsibility", "owned_area", "depends_on"),
)
_INTERFACE = _schema(
    {
        "unit": {"type": "string", "minLength": 1},
        "direction": {"type": "string", "enum": ["provided", "consumed"]},
        "name": {"type": "string", "minLength": 1},
        "data_shape": {"type": "string"},
        "valid_when": {"type": "string"},
        "lifetime": {"type": "string"},
        "ownership": {"type": "string"},
        "error_behavior": {"type": "string"},
        "compatibility": {"type": "string"},
    },
    required=("unit", "direction", "name"),
)
_UNIT_STATEMENT = _schema(
    {
        "unit": {"type": "string", "minLength": 1},
        "statement": {"type": "string", "minLength": 1},
    },
    required=("unit", "statement"),
)
_LIFECYCLE = _schema(
    {
        "unit": {"type": "string", "minLength": 1},
        "description": {"type": "string", "minLength": 1},
        "states": _STRING_ARRAY,
        "initial_state": {"type": "string"},
        "terminal_states": _STRING_ARRAY,
        "transitions": _STRING_ARRAY,
    },
    required=("unit", "description"),
)
_RULE = _schema(
    {
        "unit": {"type": "string", "minLength": 1},
        "kind": {"type": "string", "enum": ["invariant", "error_behavior", "compatibility", "dependency_constraint", "verification_obligation", "split_condition"]},
        "statement": {"type": "string", "minLength": 1},
        "condition": {"type": "string"},
        "expected": {"type": "string"},
    },
    required=("unit", "kind", "statement"),
)
_COMPLEXITY = _schema(
    {
        "unit": {"type": "string", "minLength": 1},
        "target_file_count": {"type": "integer", "minimum": 0},
        "estimated_context_tokens": {"type": "integer", "minimum": 0},
        "public_interface_count": {"type": "integer", "minimum": 0},
        "cross_unit_contract_count": {"type": "integer", "minimum": 0},
        "stateful_resource_count": {"type": "integer", "minimum": 0},
        "expected_candidate_cycles": {"type": "integer", "minimum": 0},
        "platform_dependency_level": {"type": "integer", "minimum": 0},
    },
    required=(
        "unit",
        "target_file_count",
        "estimated_context_tokens",
        "public_interface_count",
        "cross_unit_contract_count",
        "stateful_resource_count",
        "expected_candidate_cycles",
        "platform_dependency_level",
    ),
)
_UNIT_REQUIREMENT = _schema(
    {
        "unit": {"type": "string", "minLength": 1},
        "section": {"type": "string", "minLength": 1},
        "requirement": {"type": "string", "minLength": 1},
    },
    required=("unit", "section", "requirement"),
)
_NAME = _schema({"name": {"type": "string", "minLength": 1}}, required=("name",))
_CONSTRAINT = _schema(
    {
        "name": {"type": "string", "minLength": 1},
        "constraint": {"type": "string", "minLength": 1},
        "rationale": {"type": "string"},
        "requirement_section": {"type": "string"},
        "requirement": {"type": "string"},
    },
    required=("name", "constraint"),
)
_DECISION = _schema(
    {
        "name": {"type": "string", "minLength": 1},
        "decision": {"type": "string", "minLength": 1},
        "rationale": {"type": "string", "minLength": 1},
        "downstream_impact": {"type": "string"},
        "requirement_section": {"type": "string"},
        "requirement": {"type": "string"},
    },
    required=("name", "decision", "rationale"),
)
_GATE = _schema(
    {
        "name": {"type": "string", "minLength": 1},
        "check": {"type": "string", "minLength": 1},
        "scope": {"type": "string"},
        "requirement_section": {"type": "string"},
        "requirement": {"type": "string"},
    },
    required=("name", "check"),
)
_CROSS = _schema(
    {
        "producer": {"type": "string", "minLength": 1},
        "consumer": {"type": "string", "minLength": 1},
        "interface": {"type": "string", "minLength": 1},
        "data_shape": {"type": "string"},
        "valid_when": {"type": "string"},
        "ownership_transfer": {"type": "string"},
        "lifecycle_handoff": {"type": "string"},
        "compatibility": {"type": "string"},
        "error_behavior": {"type": "string"},
        "requirement_section": {"type": "string"},
        "requirement": {"type": "string"},
    },
    required=("producer", "consumer", "interface"),
)
_INTEGRATION = _schema(
    {
        "depends_on": _STRING_ARRAY,
        "entrypoint": {"type": "string", "minLength": 1},
        "dataflow": _STRING_ARRAY,
        "completion_condition": {"type": "string", "minLength": 1},
        "failure_behavior": {"type": "string", "minLength": 1},
    },
    required=("depends_on", "entrypoint", "completion_condition", "failure_behavior"),
)
_ASSUMPTION = _schema(
    {
        "name": {"type": "string", "minLength": 1},
        "statement": {"type": "string", "minLength": 1},
        "owner": {"type": "string"},
        "impact": {"type": "string"},
        "verification_plan": {"type": "string"},
    },
    required=("name", "statement"),
)
_RISK = _schema(
    {
        "name": {"type": "string", "minLength": 1},
        "risk": {"type": "string", "minLength": 1},
        "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "mitigation": {"type": "string"},
    },
    required=("name", "risk", "severity"),
)
_REVIEW_FINDING = _schema(
    {
        "finding_kind": {"type": "string", "enum": ["requirements_defect", "contract_defect", "architecture_defect"]},
        "summary": {"type": "string", "minLength": 1},
        "severity": {"type": "string", "enum": ["error", "warning"]},
        "target_section": {
            "type": "string",
            "enum": ["requirements", "constraint", "design_decision", "gate_check", "unit", "cross_unit_contract", "topology", "integration_contract", "assumption_ledger", "risk_ledger"],
        },
        "target_name": {"type": "string", "minLength": 1},
        "target_requirement_section": {"type": "string"},
        "fields": _STRING_ARRAY,
        "operation": {"type": "string", "enum": ["create", "update", "delete"]},
        "refs": _STRING_ARRAY,
    },
    required=("finding_kind", "summary", "severity", "target_section", "target_name", "operation"),
)


CONTRACT_BUILDER_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_minion_requirement_upsert": {"name": "op_requirement_upsert", "description": "Create or replace one exact natural-language Requirement. Identity is section plus statement. strength is hard or soft; source_refs, acceptance_semantics, and ambiguities are optional.", "parameters_schema": _REQUIREMENT_UPSERT},
    "op_minion_requirement_remove": {"name": "op_requirement_remove", "description": "Remove one Requirement by exact section and statement.", "parameters_schema": _REQUIREMENT_REF},
    "op_minion_requirement_add_clarification": {"name": "op_requirement_add_clarification", "description": "Add one unresolved clarification without rewriting the Requirement.", "parameters_schema": _TEXT},
    "op_minion_requirement_add_source_coverage": {"name": "op_requirement_add_source_coverage", "description": "Record one covered user/source input in natural language.", "parameters_schema": _TEXT},
    "op_minion_requirements_submit": {"name": "op_requirements_submit", "description": "Submit the current Requirements Draft. Takes no arguments.", "parameters_schema": _NO_ARGS},
    "op_minion_contract_unit_upsert": {"name": "op_contract_unit_upsert", "description": "Create or update one semantic unit shell and its construction dependencies. behavior_kind is stateless, resource_owner, service, workflow, or adapter. owned_area names this unit's boundary; reference_only_paths are readable truth sources. Do not include implementation steps.", "parameters_schema": _UNIT_UPSERT},
    "op_minion_contract_unit_add_interface": {"name": "op_contract_unit_add_interface", "description": "Add one provided or consumed interface contract to one unit. direction is provided or consumed; data_shape, valid_when, lifetime, ownership, error_behavior, and compatibility describe the boundary.", "parameters_schema": _INTERFACE},
    "op_minion_contract_unit_set_ownership": {"name": "op_contract_unit_set_ownership", "description": "Set one unit's ownership rule as a semantic statement.", "parameters_schema": _UNIT_STATEMENT},
    "op_minion_contract_unit_set_lifecycle": {"name": "op_contract_unit_set_lifecycle", "description": "Set one unit's lifecycle model. Stateless units may state process/import lifetime.", "parameters_schema": _LIFECYCLE},
    "op_minion_contract_unit_set_state": {"name": "op_contract_unit_set_state", "description": "Set one unit's state model. Stateless units must use description=stateless.", "parameters_schema": _LIFECYCLE},
    "op_minion_contract_unit_add_rule": {"name": "op_contract_unit_add_rule", "description": "Add one rule. kind is invariant, error_behavior, compatibility, dependency_constraint, verification_obligation, or split_condition; statement is required and condition/expected are optional.", "parameters_schema": _RULE},
    "op_minion_contract_unit_set_complexity": {"name": "op_contract_unit_set_complexity", "description": "Set the checkable one-Candidate complexity budget for one unit.", "parameters_schema": _COMPLEXITY},
    "op_minion_contract_unit_cover_requirement": {"name": "op_contract_unit_cover_requirement", "description": "Bind one exact Requirement to one unit. Manager resolves the hidden Requirement identity.", "parameters_schema": _UNIT_REQUIREMENT},
    "op_minion_contract_unit_remove": {"name": "op_contract_unit_remove", "description": "Remove one semantic unit during a scoped revision.", "parameters_schema": _NAME},
    "op_minion_contract_add_constraint": {"name": "op_contract_add_constraint", "description": "Add or replace one named global constraint.", "parameters_schema": _CONSTRAINT},
    "op_minion_contract_add_design_decision": {"name": "op_contract_add_design_decision", "description": "Add or replace one named architecture decision.", "parameters_schema": _DECISION},
    "op_minion_contract_add_gate_check": {"name": "op_contract_add_gate_check", "description": "Add or replace one module-boundary or end-to-end gate, not an implementation checklist.", "parameters_schema": _GATE},
    "op_minion_contract_add_cross_unit_contract": {"name": "op_contract_add_cross_unit_contract", "description": "Add or replace one directional cross-unit data/lifecycle contract.", "parameters_schema": _CROSS},
    "op_minion_contract_set_integration": {"name": "op_contract_set_integration", "description": "Set the real end-to-end delivery entrypoint, dataflow, completion, and failure behavior.", "parameters_schema": _INTEGRATION},
    "op_minion_contract_add_assumption": {"name": "op_contract_add_assumption", "description": "Add or replace one named assumption with owner, impact, and verification plan.", "parameters_schema": _ASSUMPTION},
    "op_minion_contract_add_risk": {"name": "op_contract_add_risk", "description": "Add or replace one named risk and mitigation. severity is low, medium, high, or critical.", "parameters_schema": _RISK},
    "op_minion_contract_submit": {"name": "op_contract_submit", "description": "Submit the current Architecture Contract Draft. Takes no arguments; Manager compiles hidden IDs and topology.", "parameters_schema": _NO_ARGS},
    "op_minion_contract_review_finding": {"name": "op_architecture_review_finding", "description": "Record one requirements_defect, contract_defect, or architecture_defect. severity is error or warning. target_section is requirements, constraint, design_decision, gate_check, unit, cross_unit_contract, topology, integration_contract, assumption_ledger, or risk_ledger. target_name is semantic text/name; target_requirement_section disambiguates duplicate Requirement text; operation is create, update, or delete. Manager resolves hidden revision identity.", "parameters_schema": _REVIEW_FINDING},
    "op_minion_architecture_review_submit": {"name": "op_architecture_review_submit", "description": "Submit the review. Takes no arguments; verdict is PASS with no findings and FAIL otherwise.", "parameters_schema": _NO_ARGS},
}

for _tool_name, _tool_spec in CONTRACT_BUILDER_TOOL_SPECS.items():
    assert_authoring_schema_budget(_tool_spec["parameters_schema"], owner=_tool_name)


def is_contract_builder_capability(name: str) -> bool:
    return str(name or "") in CONTRACT_BUILDER_TOOL_SPECS


def contract_builder_tool_result(
    call: CanonicalToolCall,
    workspace: dict[str, Any],
    produced_artifacts: list[dict[str, Any]],
) -> CanonicalToolResult:
    try:
        stage = _stage(workspace)
        name = str(call.name or "")
        if name == "op_minion_requirements_submit":
            output, version = _compile_requirements(call, workspace)
            return _publish(call, workspace, produced_artifacts, output, version=version, draft_kind="requirements", filename="requirements.json")
        if name == "op_minion_contract_submit":
            output, version = _compile_contract(call, workspace)
            return _publish(call, workspace, produced_artifacts, output, version=version, draft_kind="contract", filename="architecture_bundle.json")
        if name == "op_minion_architecture_review_submit":
            output, version = _compile_review(call, workspace)
            return _publish(call, workspace, produced_artifacts, output, version=version, draft_kind="architecture_review", filename="architecture_review.json")
        if stage == "requirements":
            return _mutate_requirements(call, workspace)
        if stage in {"architect", "architect_planning", "contract"}:
            return _mutate_contract(call, workspace)
        if stage == "architecture_review":
            return _mutate_review(call, workspace)
        raise ValueError(f"capability {name} is unavailable in stage {stage}")
    except Exception as exc:
        text = f"{exc.__class__.__name__}: {exc}"
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=text,
            llm_text=text + " Correct only this local semantic field and retry in the same invocation.",
            structured={"error": str(exc), "error_type": exc.__class__.__name__},
            call_id=call.call_id,
            status=RuntimeStatus.INVALID,
        )


def _mutate_requirements(call: CanonicalToolCall, workspace: Mapping[str, Any]) -> CanonicalToolResult:
    name = str(call.name or "")
    if name not in REQUIREMENTS_BUILDER_CAPABILITIES[:-1]:
        raise ValueError(f"unknown Requirements authoring capability: {name}")
    args = dict(call.args or {})
    context, store = _store(workspace, "requirements")

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        definitions = dict(payload.get("definitions") or {})
        requirements = [dict(item) for item in list(definitions.get("requirements") or [])]
        if name == "op_minion_requirement_upsert":
            incoming = {
                "section": str(args.get("section") or "").strip(),
                "statement": str(args.get("statement") or "").strip(),
                "strength": str(args.get("strength") or "hard"),
                "source_refs": _strings(args.get("source_refs") or [], "source_refs"),
                "acceptance_semantics": str(args.get("acceptance_semantics") or ""),
                "ambiguities": _strings(args.get("ambiguities") or [], "ambiguities"),
            }
            key = _requirement_key(incoming["section"], incoming["statement"])
            requirements = [item for item in requirements if _requirement_key(item.get("section"), item.get("statement")) != key]
            requirements.append(incoming)
        elif name == "op_minion_requirement_remove":
            key = _requirement_key(args.get("section"), args.get("requirement"))
            before = len(requirements)
            requirements = [item for item in requirements if _requirement_key(item.get("section"), item.get("statement")) != key]
            if len(requirements) == before:
                raise ValueError("unknown exact Requirement")
        else:
            field = "open_clarifications" if name == "op_minion_requirement_add_clarification" else "source_coverage"
            values = list(definitions.get(field) or [])
            text = str(args.get("text") or "").strip()
            if text and text not in values:
                values.append(text)
            definitions[field] = values
        definitions["requirements"] = requirements
        payload["definitions"] = definitions
        return payload, {"updated": True, "requirement_count": len(requirements)}

    result = store.mutate(context, operation_key=_op_key(call), request=args, reducer=reducer, seed=_requirements_seed())
    return _ok(call, "Requirements Draft updated", result)


def _compile_requirements(call: CanonicalToolCall, workspace: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    _require_no_args(call)
    context, store = _store(workspace, "requirements")
    snapshot = store.read(context, seed=_requirements_seed())
    definitions = dict(snapshot.payload.get("definitions") or {})
    output = validate_requirements_artifact(
        {
            "requirements": list(definitions.get("requirements") or []),
            "open_clarifications": list(definitions.get("open_clarifications") or []),
            "source_coverage": list(definitions.get("source_coverage") or []),
        }
    )
    return output, snapshot.version


def _mutate_contract(call: CanonicalToolCall, workspace: Mapping[str, Any]) -> CanonicalToolResult:
    name = str(call.name or "")
    if name not in CONTRACT_SKETCH_BUILDER_CAPABILITIES[:-1]:
        raise ValueError(f"unknown Contract authoring capability: {name}")
    args = dict(call.args or {})
    context, store = _store(workspace, "contract")

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        definitions = dict(payload.get("definitions") or {})
        contract = deepcopy(dict(definitions.get("contract") or _empty_contract()))
        _apply_contract_operation(contract, name, args, workspace=workspace)
        _assert_revision_scope(workspace, contract)
        definitions["contract"] = contract
        payload["definitions"] = definitions
        return payload, {"updated": True, "counts": _contract_counts(contract)}

    result = store.mutate(context, operation_key=_op_key(call), request=args, reducer=reducer, seed=_contract_seed(workspace))
    return _ok(call, "Architecture Contract Draft updated", result)


def _apply_contract_operation(contract: dict[str, Any], name: str, args: Mapping[str, Any], *, workspace: Mapping[str, Any]) -> None:
    units = [dict(item) for item in list(contract.get("units") or [])]
    unit_by_name = {str(item.get("unit_id") or ""): item for item in units}
    if name == "op_minion_contract_unit_upsert":
        unit_name = _semantic_name(args.get("name"))
        unit = dict(unit_by_name.get(unit_name) or _empty_unit(unit_name))
        unit.update(
            {
                "unit_id": unit_name,
                "unit_behavior_kind": str(args.get("behavior_kind") or ""),
                "responsibility": str(args.get("responsibility") or "").strip(),
                "owned_area": _strings(args.get("owned_area") or [], "owned_area"),
                "reference_only_paths": _strings(args.get("reference_only_paths") or [], "reference_only_paths"),
            }
        )
        _replace_named(units, unit, id_field="unit_id")
        topology = dict(dict(contract.get("topology") or {}).get("depends_on") or {})
        topology[unit_name] = _strings(args.get("depends_on") or [], "depends_on")
        contract["topology"] = {"depends_on": topology}
    elif name == "op_minion_contract_unit_remove":
        unit_name = _semantic_name(args.get("name"))
        if unit_name not in unit_by_name:
            raise ValueError(f"unknown unit: {unit_name}")
        units = [item for item in units if str(item.get("unit_id")) != unit_name]
        topology = dict(dict(contract.get("topology") or {}).get("depends_on") or {})
        topology.pop(unit_name, None)
        contract["topology"] = {"depends_on": topology}
    elif name.startswith("op_minion_contract_unit_"):
        unit_name = _semantic_name(args.get("unit"))
        unit = unit_by_name.get(unit_name)
        if unit is None:
            raise ValueError(f"unknown unit: {unit_name}")
        if name == "op_minion_contract_unit_add_interface":
            field = "provided_interfaces" if str(args.get("direction")) == "provided" else "consumed_interfaces"
            interface = {key: value for key, value in args.items() if key not in {"unit", "direction"} and value not in (None, "")}
            interface["direction"] = str(args.get("direction"))
            _append_unique(unit, field, interface, semantic_key="name")
        elif name == "op_minion_contract_unit_set_ownership":
            unit["ownership"] = {"rule": str(args.get("statement") or "").strip()}
        elif name in {"op_minion_contract_unit_set_lifecycle", "op_minion_contract_unit_set_state"}:
            field = "lifecycle" if name.endswith("lifecycle") else "state_model"
            unit[field] = {
                "description": str(args.get("description") or "").strip(),
                "states": _strings(args.get("states") or [], "states"),
                "initial_state": str(args.get("initial_state") or ""),
                "terminal_states": _strings(args.get("terminal_states") or [], "terminal_states"),
                "transitions": _strings(args.get("transitions") or [], "transitions"),
            }
            if field == "state_model" and str(args.get("description") or "").strip().lower() in {"stateless", "n/a", "none"}:
                unit[field] = "stateless"
        elif name == "op_minion_contract_unit_add_rule":
            kind = str(args.get("kind") or "")
            field = {
                "invariant": "invariants",
                "error_behavior": "error_behavior",
                "compatibility": "compatibility",
                "dependency_constraint": "dependency_constraints",
                "verification_obligation": "verification_obligations",
                "split_condition": "split_conditions",
            }[kind]
            if field == "split_conditions":
                values = list(unit.get(field) or [])
                statement = str(args.get("statement") or "").strip()
                if statement not in values:
                    values.append(statement)
                unit[field] = values
            else:
                _append_unique(
                    unit,
                    field,
                    {
                        "kind": kind,
                        "statement": str(args.get("statement") or "").strip(),
                        "condition": str(args.get("condition") or ""),
                        "expected": str(args.get("expected") or ""),
                    },
                    semantic_key="statement",
                )
        elif name == "op_minion_contract_unit_set_complexity":
            unit["complexity_budget"] = {key: int(value) for key, value in args.items() if key != "unit"}
        elif name == "op_minion_contract_unit_cover_requirement":
            requirement_id = _resolve_requirement_id(workspace, args.get("section"), args.get("requirement"))
            values = list(unit.get("requirement_ids") or [])
            if requirement_id not in values:
                values.append(requirement_id)
            unit["requirement_ids"] = values
        _replace_named(units, unit, id_field="unit_id")
    elif name in {"op_minion_contract_add_constraint", "op_minion_contract_add_design_decision", "op_minion_contract_add_gate_check"}:
        field, prefix, text_fields = {
            "op_minion_contract_add_constraint": ("global_constraints", "C", ("constraint", "rationale")),
            "op_minion_contract_add_design_decision": ("design_decisions", "D", ("decision", "rationale", "downstream_impact")),
            "op_minion_contract_add_gate_check": ("gate_checks", "G", ("check", "scope")),
        }[name]
        semantic_name = _semantic_name(args.get("name"))
        item = {
            "id": _manager_id(prefix, semantic_name),
            "semantic_name": semantic_name,
            **{key: str(args.get(key) or "") for key in text_fields},
            "requirement_ids": _optional_requirement_ids(workspace, args),
        }
        _replace_named(contract[field], item, id_field="id")
    elif name == "op_minion_contract_add_cross_unit_contract":
        producer = _semantic_name(args.get("producer"))
        consumer = _semantic_name(args.get("consumer"))
        interface = str(args.get("interface") or "").strip()
        semantic_name = f"{producer}_to_{consumer}_{_slug(interface)}"
        item = {
            "id": _manager_id("X", semantic_name),
            "semantic_name": semantic_name,
            **{key: value for key, value in args.items() if key not in {"requirement_section", "requirement"}},
            "requirement_ids": _optional_requirement_ids(workspace, args),
        }
        _replace_named(contract["cross_unit_contracts"], item, id_field="id")
    elif name == "op_minion_contract_set_integration":
        contract["integration_contract"] = {
            "depends_on": _strings(args.get("depends_on") or [], "depends_on"),
            "entrypoint": str(args.get("entrypoint") or ""),
            "dataflow": _strings(args.get("dataflow") or [], "dataflow"),
            "completion_condition": str(args.get("completion_condition") or ""),
            "failure_behavior": str(args.get("failure_behavior") or ""),
        }
    elif name == "op_minion_contract_add_assumption":
        semantic_name = _semantic_name(args.get("name"))
        item = {"id": _manager_id("A", semantic_name), "semantic_name": semantic_name, **{key: value for key, value in args.items() if key != "name"}}
        _replace_named(contract["assumption_ledger"]["assumptions"], item, id_field="id")
    elif name == "op_minion_contract_add_risk":
        semantic_name = _semantic_name(args.get("name"))
        item = {"id": _manager_id("RISK", semantic_name), "semantic_name": semantic_name, **{key: value for key, value in args.items() if key != "name"}}
        _replace_named(contract["risk_ledger"]["risks"], item, id_field="id")
    else:
        raise ValueError(f"unsupported Contract operation: {name}")
    contract["units"] = units


def _compile_contract(call: CanonicalToolCall, workspace: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    _require_no_args(call)
    context, store = _store(workspace, "contract")
    snapshot = store.read(context, seed=_contract_seed(workspace))
    contract = deepcopy(dict(dict(snapshot.payload.get("definitions") or {}).get("contract") or {}))
    _validate_contract(contract)
    _assert_revision_scope(workspace, contract)
    base = workspace.get("contract_revision_base_payload")
    if isinstance(base, Mapping) and contract == dict(base):
        raise ValueError("Architecture Contract revision makes no semantic change")
    return contract, snapshot.version


def _mutate_review(call: CanonicalToolCall, workspace: Mapping[str, Any]) -> CanonicalToolResult:
    if str(call.name or "") != "op_minion_contract_review_finding":
        raise ValueError(f"unknown architecture review capability: {call.name}")
    args = dict(call.args or {})
    target = _resolve_revision_target(workspace, args)
    finding = {
        "finding_kind": str(args.get("finding_kind") or ""),
        "summary": str(args.get("summary") or "").strip(),
        "severity": str(args.get("severity") or "error"),
        "refs": _strings(args.get("refs") or [], "refs"),
        "revision_targets": [target],
    }
    context, store = _store(workspace, "architecture_review")

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        findings = [dict(item) for item in list(payload.get("findings") or [])]
        findings.append(finding)
        payload["findings"] = findings
        return payload, {"recorded": True, "finding_count": len(findings)}

    result = store.mutate(context, operation_key=_op_key(call), request=args, reducer=reducer, seed=_review_seed())
    return _ok(call, "architecture finding recorded", result)


def _compile_review(call: CanonicalToolCall, workspace: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    _require_no_args(call)
    context, store = _store(workspace, "architecture_review")
    snapshot = store.read(context, seed=_review_seed())
    findings = [dict(item) for item in list(snapshot.payload.get("findings") or [])]
    output = {"verdict": "FAIL" if findings else "PASS", "findings": findings}
    _validate_architecture_review(output)
    return output, snapshot.version


def _publish(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
    produced_artifacts: list[dict[str, Any]],
    output: Mapping[str, Any],
    *,
    version: int,
    draft_kind: str,
    filename: str,
) -> CanonicalToolResult:
    artifact = _write_minion_artifact(
        dict(workspace),
        {
            "relative_path": filename,
            "title": f"V2 {draft_kind} submission",
            "role": "primary",
            "mime_type": "application/json",
            "overwrite": True,
            "content": json.dumps(dict(output), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        },
    )
    _append_unique_artifact(produced_artifacts, artifact)
    context, store = _store(workspace, draft_kind)
    store.mark_submitted(context, expected_version=version)
    return _ok(call, f"{draft_kind} submitted. Stop now.", {"submitted": True, "artifact": artifact})


def seed_contract_builder_draft(
    workspace: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    revision_scope: Mapping[str, Any] | None = None,
) -> None:
    if not isinstance(workspace, dict):
        raise TypeError("contract revision workspace must be mutable")
    candidate = deepcopy(dict(payload))
    _validate_contract(candidate)
    workspace["contract_revision_base_payload"] = candidate
    if revision_scope is not None:
        normalized = normalize_revision_targets(dict(revision_scope).get("write_targets") or [])
        if not normalized:
            raise ValueError("revision scope requires at least one write target")
        workspace["contract_revision_scope"] = {
            **dict(revision_scope),
            "write_targets": [item.to_dict() for item in normalized],
        }


def _validate_contract(payload: Mapping[str, Any]) -> None:
    units = [validate_unit_contract(dict(item), complexity_policy=ComplexityBudgetPolicy()) for item in list(payload.get("units") or [])]
    if not units:
        raise ValueError("Architecture Contract requires at least one unit")
    names = {str(item["unit_id"]) for item in units}
    if len(names) != len(units):
        raise ValueError("Architecture Contract has duplicate unit names")
    depends_on = {str(key): [str(item) for item in list(value or [])] for key, value in dict(dict(payload.get("topology") or {}).get("depends_on") or {}).items()}
    if set(depends_on) != names:
        raise ValueError("topology must contain exactly every semantic unit name")
    unknown = {item for values in depends_on.values() for item in values if item not in names}
    if unknown:
        raise ValueError("topology references unknown units: " + ", ".join(sorted(unknown)))
    _assert_acyclic(depends_on)
    for raw in list(payload.get("cross_unit_contracts") or []):
        contract = dict(raw or {})
        producer = str(contract.get("producer") or "")
        consumer = str(contract.get("consumer") or "")
        if producer not in names or consumer not in names:
            raise ValueError(
                "cross-unit contract references unknown unit: "
                + " -> ".join(item or "<empty>" for item in (producer, consumer))
            )
        if producer == consumer:
            raise ValueError(f"cross-unit contract cannot connect {producer} to itself")
    integration = dict(payload.get("integration_contract") or {})
    if not str(integration.get("entrypoint") or "").strip() or not str(integration.get("completion_condition") or "").strip():
        raise ValueError("integration contract requires a real entrypoint and completion condition")
    integration_unknown = set(str(item) for item in list(integration.get("depends_on") or [])) - names
    if integration_unknown:
        raise ValueError("integration contract references unknown units: " + ", ".join(sorted(integration_unknown)))
    if not str(integration.get("failure_behavior") or "").strip():
        raise ValueError("integration contract requires deterministic failure behavior")
    _reject_implementation_fields(payload)


def _resolve_revision_target(workspace: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
    contract = dict(workspace.get("contract_review_base_payload") or {})
    section = str(args.get("target_section") or "")
    target_name = str(args.get("target_name") or "").strip()
    operation = str(args.get("operation") or "update")
    fields = _strings(args.get("fields") or [], "fields")
    if section == "requirements":
        requirement_section = str(args.get("target_requirement_section") or "").strip()
        target_id = _resolve_requirement_id(
            workspace,
            requirement_section,
            target_name,
            allow_any_section=not requirement_section,
        )
    elif section in {"unit", "topology"}:
        known_units = {str(item.get("unit_id")) for item in list(contract.get("units") or [])}
        if operation != "create" and target_name not in known_units:
            raise ValueError(f"unknown semantic unit target: {target_name}")
        if operation == "create" and target_name in known_units:
            raise ValueError(f"semantic unit target already exists: {target_name}")
        target_id = target_name
    elif section in {"integration_contract", "assumption_ledger", "risk_ledger"}:
        target_id = {"integration_contract": "integration", "assumption_ledger": "assumptions", "risk_ledger": "risks"}[section]
    else:
        field, prefix = {
            "constraint": ("global_constraints", "C"),
            "design_decision": ("design_decisions", "D"),
            "gate_check": ("gate_checks", "G"),
            "cross_unit_contract": ("cross_unit_contracts", "X"),
        }[section]
        matches = [dict(item) for item in list(contract.get(field) or []) if str(dict(item).get("semantic_name") or "") == target_name]
        if operation == "create" and not matches:
            target_id = _manager_id(prefix, target_name)
        elif len(matches) != 1:
            raise ValueError(f"semantic review target is unknown or ambiguous: {section} {target_name!r}")
        else:
            if operation == "create":
                raise ValueError(f"semantic review target already exists: {section} {target_name!r}")
            target_id = str(matches[0]["id"])
    target = {"section": section, "id": target_id, "fields": fields, "operation": operation}
    normalize_revision_targets([target])
    return target


def _resolve_requirement_id(
    workspace: Mapping[str, Any],
    section: Any,
    requirement: Any,
    *,
    allow_any_section: bool = False,
) -> str:
    payload = _bound_requirements(workspace)
    normalized_section = " ".join(str(section or "").split()).casefold()
    normalized_requirement = " ".join(str(requirement or "").split()).casefold()
    matches = [
        dict(item)
        for item in list(payload.get("requirements") or [])
        if " ".join(str(dict(item).get("statement") or "").split()).casefold() == normalized_requirement
        and (allow_any_section or " ".join(str(dict(item).get("section") or "").split()).casefold() == normalized_section)
    ]
    if len(matches) != 1:
        raise ValueError("Requirement reference is unknown or ambiguous; use exact section and text")
    return str(matches[0]["requirement_id"])


def _bound_requirements(workspace: Mapping[str, Any]) -> dict[str, Any]:
    hidden = workspace.get("contract_review_requirements_payload")
    if isinstance(hidden, Mapping):
        return validate_requirements_artifact(dict(hidden))
    for item in list(workspace.get("reference_paths") or []):
        reference = dict(item or {})
        if str(reference.get("name") or "") == "requirements":
            path = Path(str(reference.get("path") or ""))
            if path.is_file():
                return validate_requirements_artifact(json.loads(path.read_text(encoding="utf-8")))
    raise ValueError("bound RequirementsArtifact is unavailable")


def _optional_requirement_ids(workspace: Mapping[str, Any], args: Mapping[str, Any]) -> list[str]:
    section = str(args.get("requirement_section") or "").strip()
    requirement = str(args.get("requirement") or "").strip()
    if bool(section) != bool(requirement):
        raise ValueError("requirement_section and requirement must be provided together")
    return [_resolve_requirement_id(workspace, section, requirement)] if section else []


def _assert_revision_scope(workspace: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    base = workspace.get("contract_revision_base_payload")
    scope = dict(workspace.get("contract_revision_scope") or {})
    if not isinstance(base, Mapping) or not scope:
        return
    allowed = normalize_revision_targets(scope.get("write_targets") or [])
    forbidden = [
        item.to_dict()
        for item in contract_revision_changes(dict(base), payload)
        if not revision_target_allows(item, allowed)
    ]
    if forbidden:
        raise ValueError("revision changed a semantic target outside its bound scope: " + json.dumps(forbidden, sort_keys=True))


def _empty_contract() -> dict[str, Any]:
    return {
        "global_constraints": [],
        "design_decisions": [],
        "gate_checks": [],
        "units": [],
        "cross_unit_contracts": [],
        "topology": {"depends_on": {}},
        "integration_contract": {},
        "assumption_ledger": {"assumptions": []},
        "risk_ledger": {"risks": []},
    }


def _empty_unit(name: str) -> dict[str, Any]:
    return {
        "unit_id": name,
        "unit_behavior_kind": "stateless",
        "responsibility": "",
        "owned_area": [],
        "reference_only_paths": [],
        "provided_interfaces": [],
        "consumed_interfaces": [],
        "ownership": {},
        "lifecycle": {},
        "state_model": {},
        "invariants": [],
        "error_behavior": [],
        "compatibility": [],
        "dependency_constraints": [],
        "requirement_ids": [],
        "verification_obligations": [],
        "complexity_budget": {},
        "split_conditions": [],
    }


def _requirements_seed() -> dict[str, Any]:
    return {"definitions": {"requirements": [], "open_clarifications": [], "source_coverage": []}, "evidence": {}, "findings": [], "summary": {}}


def _contract_seed(workspace: Mapping[str, Any]) -> dict[str, Any]:
    base = workspace.get("contract_revision_base_payload")
    contract = deepcopy(dict(base)) if isinstance(base, Mapping) else _empty_contract()
    return {"definitions": {"contract": contract}, "evidence": {}, "findings": [], "summary": {}}


def _review_seed() -> dict[str, Any]:
    return {"definitions": {}, "evidence": {}, "findings": [], "summary": {}}


def _stage(workspace: Mapping[str, Any]) -> str:
    value = str(workspace.get("contract_builder_stage") or "").strip()
    if value not in {"architect", "architect_planning", "requirements", "contract", "architecture_review"}:
        raise ValueError("contract_builder_stage is not bound")
    return value


def _store(workspace: Mapping[str, Any], draft_kind: str) -> tuple[SubmissionDraftContext, SubmissionDraftStore]:
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind=draft_kind)
    return context, SubmissionDraftStore(Path(str(workspace["runtime_root"])))


def _replace_named(items: list[dict[str, Any]], incoming: Mapping[str, Any], *, id_field: str) -> None:
    identity = str(incoming.get(id_field) or "")
    for index, item in enumerate(items):
        if str(item.get(id_field) or "") == identity:
            items[index] = dict(incoming)
            return
    items.append(dict(incoming))


def _append_unique(owner: dict[str, Any], field: str, incoming: Mapping[str, Any], *, semantic_key: str) -> None:
    values = [dict(item) for item in list(owner.get(field) or [])]
    identity = str(incoming.get(semantic_key) or "")
    values = [item for item in values if str(item.get(semantic_key) or "") != identity]
    values.append(dict(incoming))
    owner[field] = values


def _manager_id(prefix: str, semantic_name: str) -> str:
    digest = hashlib.sha256(f"{prefix}\0{semantic_name}".encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _semantic_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in name):
        raise ValueError("semantic name must use lowercase snake_case")
    return name


def _slug(value: str) -> str:
    result = "_".join("".join(character.lower() if character.isalnum() else " " for character in value).split())
    return result or "contract"


def _strings(value: Any, owner: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{owner} must be a string array")
    return [str(item) for item in value]


def _requirement_key(section: Any, statement: Any) -> tuple[str, str]:
    return " ".join(str(section or "").split()).casefold(), " ".join(str(statement or "").split()).casefold()


def _op_key(call: CanonicalToolCall) -> str:
    return str(call.call_id or "").strip() or f"{call.name}:{hashlib.sha256(json.dumps(dict(call.args or {}), sort_keys=True).encode()).hexdigest()}"


def _require_no_args(call: CanonicalToolCall) -> None:
    if dict(call.args or {}):
        raise ValueError(f"{call.name} takes no arguments")


def _contract_counts(payload: Mapping[str, Any]) -> dict[str, int]:
    return {field: len(list(payload.get(field) or [])) for field in ("global_constraints", "design_decisions", "gate_checks", "units", "cross_unit_contracts")}


def _validate_architecture_review(payload: Mapping[str, Any]) -> None:
    verdict = str(payload.get("verdict") or "")
    findings = list(payload.get("findings") or [])
    if verdict == "PASS" and findings:
        raise ValueError("PASS architecture review cannot contain findings")
    if verdict == "FAIL" and not findings:
        raise ValueError("FAIL architecture review requires findings")
    if verdict not in {"PASS", "FAIL"}:
        raise ValueError("architecture review verdict is invalid")
    for finding in findings:
        normalize_revision_targets(dict(finding).get("revision_targets") or [])


def _reject_implementation_fields(value: Any) -> None:
    forbidden = {"milestone", "milestones", "implementation_steps", "implementation_checklist", "test_matrix", "function_steps"}
    if isinstance(value, Mapping):
        found = forbidden & set(value)
        if found:
            raise ValueError("Architecture Contract contains implementation-level fields: " + ", ".join(sorted(found)))
        for item in value.values():
            _reject_implementation_fields(item)
    elif isinstance(value, list):
        for item in value:
            _reject_implementation_fields(item)


def _assert_acyclic(depends_on: Mapping[str, list[str]]) -> None:
    pending = {key: set(values) for key, values in depends_on.items()}
    while pending:
        ready = {key for key, values in pending.items() if not values}
        if not ready:
            raise ValueError("topology contains a dependency cycle: " + ", ".join(sorted(pending)))
        pending = {key: values - ready for key, values in pending.items() if key not in ready}


def _ok(call: CanonicalToolCall, text: str, structured: Mapping[str, Any]) -> CanonicalToolResult:
    return CanonicalToolResult(name=call.name, ok=True, text=text, llm_text=text, structured=dict(structured), call_id=call.call_id, status=RuntimeStatus.OK)
