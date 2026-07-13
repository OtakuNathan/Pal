from __future__ import annotations

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
from pal.minion.workspace_tools import _append_unique_artifact, _write_minion_artifact
from pal.shared import RuntimeStatus


REQUIREMENTS_BUILDER_CAPABILITIES = (
    "op_minion_requirements_replace_batch",
    "op_minion_requirements_validate",
    "op_minion_requirements_submit",
)

CONTRACT_SKETCH_BUILDER_CAPABILITIES = (
    "op_minion_contract_read",
    "op_minion_contract_get",
    "op_minion_contract_validate",
    "op_minion_contract_add_gate_checks_batch",
    "op_minion_contract_delete_gate_checks_batch",
    "op_minion_contract_add_constraints_batch",
    "op_minion_contract_delete_constraints_batch",
    "op_minion_contract_add_design_decisions_batch",
    "op_minion_contract_delete_design_decisions_batch",
    "op_minion_contract_add_unit_outlines_batch",
    "op_minion_contract_replace_unit_outlines_batch",
    "op_minion_contract_delete_units_batch",
    "op_minion_contract_add_unit_acceptance_batch",
    "op_minion_contract_add_cross_unit_contracts_batch",
    "op_minion_contract_delete_cross_unit_contracts_batch",
    "op_minion_contract_set_integration",
    "op_minion_contract_submit_sketch",
)

REVISION_CONTRACT_BUILDER_CAPABILITIES = (
    "op_minion_contract_revision_read",
)

ARCHITECTURE_REVIEW_BUILDER_CAPABILITIES = (
    "op_minion_architecture_review_submit",
)

CONTRACT_BUILDER_CAPABILITIES = (
    *REQUIREMENTS_BUILDER_CAPABILITIES,
    *CONTRACT_SKETCH_BUILDER_CAPABILITIES,
    *REVISION_CONTRACT_BUILDER_CAPABILITIES,
    *ARCHITECTURE_REVIEW_BUILDER_CAPABILITIES,
)

ARCHITECT_BUILDER_CAPABILITIES = (
    *CONTRACT_SKETCH_BUILDER_CAPABILITIES,
)


def _object_schema(
    properties: Mapping[str, Any],
    *,
    required: tuple[str, ...] = (),
    additional_properties: bool | Mapping[str, Any] = False,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": dict(properties),
        "additionalProperties": additional_properties,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _batch_schema(name: str, item_schema: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {name: {"type": "array", "items": dict(item_schema)}},
        "required": [name],
        "additionalProperties": False,
    }


_STRING_LIST_SCHEMA = {"type": "array", "items": {"type": "string"}}
_ID_LIST_SCHEMA = _object_schema(
    {"ids": _STRING_LIST_SCHEMA},
    required=("ids",),
)
_DESCRIBED_VALUE_SCHEMA = {
    "anyOf": [
        {"type": "string"},
        _object_schema(
            {
                "description": {"type": "string"},
                "states": _STRING_LIST_SCHEMA,
                "initial_state": {"type": "string"},
                "terminal_states": _STRING_LIST_SCHEMA,
                "transitions": {"type": "array", "items": {"type": "string"}},
            }
        ),
    ]
}
_NAMED_CONTRACT_SCHEMA = _object_schema(
    {
        "name": {"type": "string", "minLength": 1},
        "data_shape": {"type": "string"},
        "direction": {"type": "string", "enum": ["provided", "consumed"]},
        "valid_when": {"type": "string"},
        "lifetime": {"type": "string"},
        "ownership": {"type": "string"},
        "error_behavior": {"type": "string"},
        "compatibility": {"type": "string"},
    },
    required=("name",),
)
_CONTRACT_STATEMENT_SCHEMA = {
    "anyOf": [
        {"type": "string"},
        _object_schema(
            {
                "kind": {"type": "string"},
                "statement": {"type": "string"},
                "condition": {"type": "string"},
                "expected": {"type": "string"},
            },
            required=("kind",),
        ),
    ]
}
_COMPLEXITY_BUDGET_SCHEMA = _object_schema(
    {
        "target_file_count": {"type": "integer", "minimum": 0},
        "estimated_context_tokens": {"type": "integer", "minimum": 0},
        "public_interface_count": {"type": "integer", "minimum": 0},
        "cross_unit_contract_count": {"type": "integer", "minimum": 0},
        "stateful_resource_count": {"type": "integer", "minimum": 0},
        "expected_candidate_cycles": {"type": "integer", "minimum": 0},
        "platform_dependency_level": {"type": "integer", "minimum": 0},
    },
    required=(
        "target_file_count",
        "estimated_context_tokens",
        "public_interface_count",
        "cross_unit_contract_count",
        "stateful_resource_count",
        "expected_candidate_cycles",
        "platform_dependency_level",
    ),
)
_UNIT_SCHEMA = _object_schema(
    {
        "unit_id": {"type": "string", "minLength": 1},
        "unit_behavior_kind": {
            "type": "string",
            "enum": ["stateless", "resource_owner", "service", "workflow", "adapter"],
        },
        "responsibility": {"type": "string", "minLength": 1},
        "owned_area": {
            **_STRING_LIST_SCHEMA,
            "description": "Semantic module responsibility boundaries, not an exhaustive file allowlist. Candidate diffs are recorded and reconciled by manager/integration.",
        },
        "reference_only_paths": _STRING_LIST_SCHEMA,
        "provided_interfaces": {"type": "array", "items": _NAMED_CONTRACT_SCHEMA},
        "consumed_interfaces": {"type": "array", "items": _NAMED_CONTRACT_SCHEMA},
        "ownership": _object_schema({}, additional_properties={"type": "string"}),
        "lifecycle": _DESCRIBED_VALUE_SCHEMA,
        "state_model": _DESCRIBED_VALUE_SCHEMA,
        "invariants": {"type": "array", "items": _CONTRACT_STATEMENT_SCHEMA},
        "error_behavior": {"type": "array", "items": _CONTRACT_STATEMENT_SCHEMA},
        "compatibility": {"type": "array", "items": _CONTRACT_STATEMENT_SCHEMA},
        "dependency_constraints": {"type": "array", "items": _CONTRACT_STATEMENT_SCHEMA},
        "requirement_ids": _STRING_LIST_SCHEMA,
        "verification_obligations": {"type": "array", "items": _CONTRACT_STATEMENT_SCHEMA},
        "complexity_budget": _COMPLEXITY_BUDGET_SCHEMA,
        "split_conditions": _STRING_LIST_SCHEMA,
    },
    required=(
        "unit_id",
        "unit_behavior_kind",
        "responsibility",
        "owned_area",
        "reference_only_paths",
        "provided_interfaces",
        "consumed_interfaces",
        "ownership",
        "lifecycle",
        "state_model",
        "invariants",
        "error_behavior",
        "compatibility",
        "dependency_constraints",
        "requirement_ids",
        "verification_obligations",
        "complexity_budget",
        "split_conditions",
    ),
)
_REQUIREMENT_SCHEMA = _object_schema(
    {
        "requirement_id": {"type": "string"},
        "statement": {"type": "string", "minLength": 1},
        "strength": {"type": "string", "enum": ["hard", "soft"]},
        "source_refs": _STRING_LIST_SCHEMA,
        "acceptance_semantics": {"type": "string"},
        "ambiguities": _STRING_LIST_SCHEMA,
    },
    required=("statement", "strength"),
)
_STABLE_ITEM_BASE = {
    "id": {"type": "string"},
    "requirement_ids": _STRING_LIST_SCHEMA,
}
_GATE_CHECK_SCHEMA = _object_schema(
    {**_STABLE_ITEM_BASE, "check": {"type": "string", "minLength": 1}, "scope": {"type": "string"}},
    required=("check",),
)
_CONSTRAINT_SCHEMA = _object_schema(
    {**_STABLE_ITEM_BASE, "constraint": {"type": "string", "minLength": 1}, "rationale": {"type": "string"}},
    required=("constraint",),
)
_DESIGN_DECISION_SCHEMA = _object_schema(
    {
        **_STABLE_ITEM_BASE,
        "decision": {"type": "string", "minLength": 1},
        "rationale": {"type": "string"},
        "downstream_impact": {"type": "string"},
    },
    required=("decision", "rationale"),
)
_ACCEPTANCE_SCHEMA = _object_schema(
    {
        "id": {"type": "string"},
        "criterion": {"type": "string", "minLength": 1},
        "requirement_ids": _STRING_LIST_SCHEMA,
    },
    required=("criterion",),
)
_CROSS_UNIT_CONTRACT_SCHEMA = _object_schema(
    {
        "id": {"type": "string"},
        "producer": {"type": "string", "minLength": 1},
        "consumer": {"type": "string", "minLength": 1},
        "interface": {"type": "string"},
        "data_shape": {"type": "string"},
        "valid_when": {"type": "string"},
        "ownership_transfer": {"type": "string"},
        "lifecycle_handoff": {"type": "string"},
        "compatibility": {"type": "string"},
        "error_behavior": {"type": "string"},
        "requirement_ids": _STRING_LIST_SCHEMA,
    },
    required=("producer", "consumer"),
)
_TOPOLOGY_SCHEMA = _object_schema(
    {"depends_on": _object_schema({}, additional_properties=_STRING_LIST_SCHEMA)},
    required=("depends_on",),
)
_INTEGRATION_SCHEMA = _object_schema(
    {
        "depends_on": _STRING_LIST_SCHEMA,
        "entrypoint": {"type": "string"},
        "dataflow": _STRING_LIST_SCHEMA,
        "completion_condition": {"type": "string"},
        "failure_behavior": {"type": "string"},
    },
    required=("depends_on",),
)
_ASSUMPTION_SCHEMA = _object_schema(
    {
        "id": {"type": "string"},
        "statement": {"type": "string", "minLength": 1},
        "owner": {"type": "string"},
        "impact": {"type": "string"},
        "verification_plan": {"type": "string"},
    },
    required=("statement",),
)
_RISK_SCHEMA = _object_schema(
    {
        "id": {"type": "string"},
        "risk": {"type": "string", "minLength": 1},
        "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "mitigation": {"type": "string"},
    },
    required=("risk",),
)
_REVISION_TARGET_SCHEMA = _object_schema(
    {
        "section": {
            "type": "string",
            "enum": [
                "requirements",
                "constraint",
                "design_decision",
                "gate_check",
                "unit",
                "cross_unit_contract",
                "topology",
                "integration_contract",
                "assumption_ledger",
                "risk_ledger",
            ],
        },
        "id": {"type": "string", "minLength": 1},
        "fields": _STRING_LIST_SCHEMA,
        "operation": {"type": "string", "enum": ["create", "update", "delete"]},
    },
    required=("section", "id"),
)


CONTRACT_BUILDER_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_minion_requirements_replace_batch": {
        "name": "op_minion_requirements_replace_batch",
        "description": "Replace the requirements draft transactionally. Each requirement has statement, strength=hard|soft, and optional requirement_id/source_refs/acceptance_semantics/ambiguities. Preserve every user-visible value; IDs are assigned when omitted.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "requirements": {"type": "array", "items": _REQUIREMENT_SCHEMA},
                "open_clarifications": {"type": "array", "items": {"type": "string"}},
                "source_coverage": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["requirements"],
            "additionalProperties": False,
        },
    },
    "op_minion_requirements_validate": {"name": "op_minion_requirements_validate", "description": "Validate the bound requirements draft. Takes no arguments.", "parameters_schema": _object_schema({})},
    "op_minion_requirements_submit": {"name": "op_minion_requirements_submit", "description": "Validate and freeze requirements.json. This is the only valid requirements completion path. Takes no arguments.", "parameters_schema": _object_schema({})},
    "op_minion_contract_read": {"name": "op_minion_contract_read", "description": "Read the normalized bound Contract Sketch draft. Takes no arguments.", "parameters_schema": _object_schema({})},
    "op_minion_contract_revision_read": {
        "name": "op_minion_contract_revision_read",
        "description": "Read only the manager-bound semantic revision scope and its current values. Scope targets use section=requirements|constraint|design_decision|gate_check|unit|cross_unit_contract|topology|integration_contract|assumption_ledger|risk_ledger, stable id, optional fields, and operation=create|update|delete. Never use contract_read during a revision.",
        "parameters_schema": _object_schema({}),
    },
    "op_minion_contract_get": {
        "name": "op_minion_contract_get",
        "description": "Read one Unit Contract from the bound draft by unit_id.",
        "parameters_schema": {"type": "object", "properties": {"unit_id": {"type": "string"}}, "required": ["unit_id"], "additionalProperties": False},
    },
    "op_minion_contract_validate": {"name": "op_minion_contract_validate", "description": "Run all mechanical Contract Sketch checks without submitting. Takes no arguments.", "parameters_schema": _object_schema({})},
    "op_minion_contract_add_gate_checks_batch": {"name": "op_minion_contract_add_gate_checks_batch", "description": "Add or replace module-boundary and end-to-end gate checks. Each item requires check; optional id, scope, requirement_ids. Do not add implementation checklists.", "parameters_schema": _batch_schema("gate_checks", _GATE_CHECK_SCHEMA)},
    "op_minion_contract_add_constraints_batch": {"name": "op_minion_contract_add_constraints_batch", "description": "Add or replace global constraints. Each item requires constraint; optional id, rationale, requirement_ids. Reuse stable IDs during revision.", "parameters_schema": _batch_schema("constraints", _CONSTRAINT_SCHEMA)},
    "op_minion_contract_delete_constraints_batch": {"name": "op_minion_contract_delete_constraints_batch", "description": "Delete existing global constraints by stable id. In a scoped revision, ids must be marked with operation=delete.", "parameters_schema": _ID_LIST_SCHEMA},
    "op_minion_contract_add_design_decisions_batch": {"name": "op_minion_contract_add_design_decisions_batch", "description": "Add or replace design decisions. Each item requires decision and rationale; optional id, downstream_impact, requirement_ids.", "parameters_schema": _batch_schema("design_decisions", _DESIGN_DECISION_SCHEMA)},
    "op_minion_contract_delete_design_decisions_batch": {"name": "op_minion_contract_delete_design_decisions_batch", "description": "Delete existing design decisions by stable id. In a scoped revision, ids must be marked with operation=delete.", "parameters_schema": _ID_LIST_SCHEMA},
    "op_minion_contract_add_unit_outlines_batch": {
        "name": "op_minion_contract_add_unit_outlines_batch",
        "description": "Add complete Unit Contracts transactionally. unit_behavior_kind MUST be one of stateless|resource_owner|service|workflow|adapter. Interface direction, when present, is provided|consumed. owned_area states the module's semantic responsibility boundary, not an exhaustive file allowlist; manager worktree isolation and candidate/integration diff audit enforce execution separation. Supply directional interfaces, ownership, lifecycle, state_model, invariants/errors/compatibility/dependency constraints, requirement_ids, verification obligations, all seven complexity_budget integers, and split_conditions. Milestones, evidence, and implementation steps are forbidden.",
        "parameters_schema": _batch_schema("units", _UNIT_SCHEMA),
    },
    "op_minion_contract_replace_unit_outlines_batch": {
        "name": "op_minion_contract_replace_unit_outlines_batch",
        "description": "Replace complete Unit Contracts by existing unit_id. The full Unit schema is required; owned_area remains a semantic module boundary, while manager worktree isolation and candidate/integration diff audit enforce execution separation. unit_behavior_kind is stateless|resource_owner|service|workflow|adapter and interface direction is provided|consumed. Use only for units named by the finding; unknown IDs are rejected.",
        "parameters_schema": _batch_schema("units", _UNIT_SCHEMA),
    },
    "op_minion_contract_delete_units_batch": {"name": "op_minion_contract_delete_units_batch", "description": "Delete existing Unit Contracts by stable unit_id. In a scoped revision, ids must be marked with operation=delete and matching topology changes must also be in scope.", "parameters_schema": {"type": "object", "properties": {"unit_ids": _STRING_LIST_SCHEMA}, "required": ["unit_ids"], "additionalProperties": False}},
    "op_minion_contract_add_unit_acceptance_batch": {
        "name": "op_minion_contract_add_unit_acceptance_batch",
        "description": "Add unit-boundary acceptance obligations. Each criterion requires criterion text and may include id and requirement_ids. Never add implementation milestones or a test matrix.",
        "parameters_schema": {
            "type": "object",
            "properties": {"unit_id": {"type": "string"}, "criteria": {"type": "array", "items": _ACCEPTANCE_SCHEMA}},
            "required": ["unit_id", "criteria"],
            "additionalProperties": False,
        },
    },
    "op_minion_contract_add_cross_unit_contracts_batch": {"name": "op_minion_contract_add_cross_unit_contracts_batch", "description": "Add or replace directional cross-unit contracts. Each item requires producer and consumer; include interface, data_shape, valid_when, ownership_transfer, lifecycle_handoff, compatibility, error_behavior, and requirement_ids where applicable.", "parameters_schema": _batch_schema("cross_unit_contracts", _CROSS_UNIT_CONTRACT_SCHEMA)},
    "op_minion_contract_delete_cross_unit_contracts_batch": {"name": "op_minion_contract_delete_cross_unit_contracts_batch", "description": "Delete existing directional cross-unit contracts by stable id. In a scoped revision, ids must be marked with operation=delete.", "parameters_schema": _ID_LIST_SCHEMA},
    "op_minion_contract_delete_gate_checks_batch": {"name": "op_minion_contract_delete_gate_checks_batch", "description": "Delete existing gate checks by stable id. In a scoped revision, ids must be marked with operation=delete.", "parameters_schema": _ID_LIST_SCHEMA},
    "op_minion_contract_set_integration": {
        "name": "op_minion_contract_set_integration",
        "description": "Set topology, integration, assumptions, and risks. topology.depends_on maps every unit_id to work-start blockers. integration_contract requires depends_on. assumption_ledger contains assumptions; risk_ledger contains risks with severity low|medium|high|critical.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "topology": _TOPOLOGY_SCHEMA,
                "integration_contract": _INTEGRATION_SCHEMA,
                "assumption_ledger": _object_schema({"assumptions": {"type": "array", "items": _ASSUMPTION_SCHEMA}}, required=("assumptions",)),
                "risk_ledger": _object_schema({"risks": {"type": "array", "items": _RISK_SCHEMA}}, required=("risks",)),
            },
            "required": ["topology", "integration_contract", "assumption_ledger", "risk_ledger"],
            "additionalProperties": False,
        },
    },
    "op_minion_contract_submit_sketch": {"name": "op_minion_contract_submit_sketch", "description": "Validate and freeze architecture_bundle.json. This is the only valid planning completion path. Takes no arguments.", "parameters_schema": _object_schema({})},
    "op_minion_architecture_review_submit": {
        "name": "op_minion_architecture_review_submit",
        "description": "Submit verdict PASS or FAIL after tracing immutable requirements to unit and cross-unit contracts. finding_kind is requirements_defect|contract_defect|architecture_defect; severity is error|warning. Every FAIL finding must include revision_targets with semantic section=requirements|constraint|design_decision|gate_check|unit|cross_unit_contract|topology|integration_contract|assumption_ledger|risk_ledger, stable id, optional fields, and operation=create|update|delete. The reviewer cannot modify the contract.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "finding_kind": {
                                "type": "string",
                                "enum": [
                                    "requirements_defect",
                                    "contract_defect",
                                    "architecture_defect",
                                ],
                            },
                            "summary": {"type": "string", "minLength": 1},
                            "refs": {"type": "array", "items": {"type": "string"}},
                            "severity": {"type": "string", "enum": ["error", "warning"]},
                            "revision_targets": {"type": "array", "items": _REVISION_TARGET_SCHEMA},
                        },
                        "required": ["finding_kind", "summary", "revision_targets"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["verdict", "findings"],
            "additionalProperties": False,
        },
    },
}


def is_contract_builder_capability(name: str) -> bool:
    return str(name or "") in CONTRACT_BUILDER_TOOL_SPECS


def contract_builder_tool_result(
    call: CanonicalToolCall,
    workspace: dict[str, Any],
    produced_artifacts: list[dict[str, Any]],
) -> CanonicalToolResult:
    try:
        runtime = ContractBuilderRuntime(workspace, produced_artifacts)
        structured = runtime.execute(call.name, dict(call.args or {}))
        message = str(structured.pop("message", "contract builder updated"))
        return CanonicalToolResult(
            name=call.name,
            ok=True,
            text=message,
            llm_text=message,
            structured=structured,
            call_id=call.call_id,
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
            status=RuntimeStatus.INVALID,
        )


class ContractBuilderRuntime:
    def __init__(self, workspace: dict[str, Any], produced_artifacts: list[dict[str, Any]]) -> None:
        self.workspace = workspace
        self.produced_artifacts = produced_artifacts
        self.stage = str(workspace.get("contract_builder_stage") or "").strip()
        if self.stage not in {"architect", "architect_planning", "requirements", "contract", "architecture_review"}:
            raise ValueError("contract_builder_stage is not bound")
        root_text = str(workspace.get("artifact_stage_dir") or workspace.get("artifact_dir") or "").strip()
        if not root_text:
            raise ValueError("contract builder requires artifact_stage_dir")
        root = Path(root_text)
        self.root = root / ".contract_builder"
        self.path = self.root / f"{self.stage}.json"

    def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.stage == "architect":
            expected = _builder_stage_for_capability(name)
            if expected != "contract":
                raise ValueError(f"capability is not available to architect: {name}")
            self.path = self.root / f"{expected}.json"
        elif self.stage == "architect_planning":
            expected = _builder_stage_for_capability(name)
            if expected != "contract":
                raise ValueError(f"capability is not available to architect: {name}")
            self.path = self.root / f"{expected}.json"
        state = self._load()
        if name == "op_minion_requirements_replace_batch":
            self._require_stage("requirements")
            requirements = []
            for index, raw in enumerate(list(args.get("requirements") or []), start=1):
                item = dict(raw or {})
                item.setdefault("requirement_id", f"R-{index}")
                item.setdefault("strength", "hard")
                item.setdefault("source_refs", [])
                item.setdefault("acceptance_semantics", "")
                item.setdefault("ambiguities", [])
                requirements.append(item)
            candidate = {"requirements": requirements, "open_clarifications": list(args.get("open_clarifications") or []), "source_coverage": list(args.get("source_coverage") or [])}
            validate_requirements_artifact(candidate)
            state["payload"] = candidate
            self._save(state)
            return {"message": "requirements draft replaced", "count": len(requirements)}
        if name in {"op_minion_requirements_validate", "op_minion_requirements_submit"}:
            self._require_stage("requirements")
            payload = validate_requirements_artifact(dict(state.get("payload") or {}))
            return self._validated_or_submitted(name.endswith("submit"), state, payload, "requirements.json")
        if name == "op_minion_contract_read":
            self._require_stage("contract")
            if state.get("revision_scope"):
                raise ValueError("revision drafts expose only op_minion_contract_revision_read")
            return {"message": "contract draft", "draft": deepcopy(state.get("payload") or {})}
        if name == "op_minion_contract_revision_read":
            self._require_stage("contract")
            return self._revision_scope_view(state)
        if name == "op_minion_contract_get":
            self._require_stage("contract")
            unit_id = str(args.get("unit_id") or "")
            unit = next((item for item in list(dict(state.get("payload") or {}).get("units") or []) if str(item.get("unit_id") or "") == unit_id), None)
            if unit is None:
                raise ValueError(f"unknown unit_id: {unit_id}")
            return {"message": f"unit {unit_id}", "unit": deepcopy(unit)}
        if name.startswith("op_minion_contract_"):
            self._require_stage("contract")
            result = self._execute_contract(name, args, state)
            return result
        if name == "op_minion_architecture_review_submit":
            self._require_stage("architecture_review")
            payload = {"verdict": str(args.get("verdict") or ""), "findings": list(args.get("findings") or [])}
            _validate_architecture_review(payload)
            return self._submit(state, payload, "architecture_review.json")
        raise ValueError(f"unsupported contract builder capability: {name}")

    def _execute_contract(self, name: str, args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        payload = deepcopy(dict(state.get("payload") or _empty_contract()))
        mapping = {
            "op_minion_contract_add_gate_checks_batch": ("gate_checks", "gate_checks", "G"),
            "op_minion_contract_add_constraints_batch": ("global_constraints", "constraints", "C"),
            "op_minion_contract_add_design_decisions_batch": ("design_decisions", "design_decisions", "D"),
            "op_minion_contract_add_cross_unit_contracts_batch": ("cross_unit_contracts", "cross_unit_contracts", "X"),
        }
        if name in mapping:
            field, arg_name, prefix = mapping[name]
            payload[field] = _upsert_stable_id_items(
                list(payload.get(field) or []),
                list(args.get(arg_name) or []),
                prefix=prefix,
                field_name=field,
            )
        elif name in {
            "op_minion_contract_delete_constraints_batch",
            "op_minion_contract_delete_design_decisions_batch",
            "op_minion_contract_delete_gate_checks_batch",
            "op_minion_contract_delete_cross_unit_contracts_batch",
        }:
            delete_mapping = {
                "op_minion_contract_delete_constraints_batch": "global_constraints",
                "op_minion_contract_delete_design_decisions_batch": "design_decisions",
                "op_minion_contract_delete_gate_checks_batch": "gate_checks",
                "op_minion_contract_delete_cross_unit_contracts_batch": "cross_unit_contracts",
            }
            field_name = delete_mapping[name]
            ids = {str(item).strip() for item in list(args.get("ids") or []) if str(item).strip()}
            existing = [dict(item or {}) for item in list(payload.get(field_name) or [])]
            known = {str(item.get("id") or "") for item in existing}
            missing = ids - known
            if missing:
                raise ValueError(f"cannot delete unknown {field_name} ids: {', '.join(sorted(missing))}")
            payload[field_name] = [item for item in existing if str(item.get("id") or "") not in ids]
        elif name == "op_minion_contract_add_unit_outlines_batch":
            existing_ids = {str(item.get("unit_id") or "") for item in list(payload.get("units") or [])}
            additions = [dict(item or {}) for item in list(args.get("units") or [])]
            for item in additions:
                unit_id = str(item.get("unit_id") or "").strip()
                if not unit_id or unit_id in existing_ids:
                    raise ValueError(f"invalid or duplicate unit_id: {unit_id or '<empty>'}")
                _reject_implementation_fields(item)
                validate_unit_contract(item, complexity_policy=ComplexityBudgetPolicy())
                existing_ids.add(unit_id)
            payload["units"] = [*list(payload.get("units") or []), *additions]
        elif name == "op_minion_contract_replace_unit_outlines_batch":
            units = [dict(item or {}) for item in list(payload.get("units") or [])]
            positions = {str(item.get("unit_id") or ""): index for index, item in enumerate(units)}
            replacements = [dict(item or {}) for item in list(args.get("units") or [])]
            replaced_ids: set[str] = set()
            for item in replacements:
                unit_id = str(item.get("unit_id") or "").strip()
                if not unit_id or unit_id not in positions:
                    raise ValueError(f"cannot replace unknown unit_id: {unit_id or '<empty>'}")
                if unit_id in replaced_ids:
                    raise ValueError(f"duplicate replacement unit_id: {unit_id}")
                _reject_implementation_fields(item)
                validate_unit_contract(item, complexity_policy=ComplexityBudgetPolicy())
                units[positions[unit_id]] = item
                replaced_ids.add(unit_id)
            payload["units"] = units
        elif name == "op_minion_contract_delete_units_batch":
            ids = {str(item).strip() for item in list(args.get("unit_ids") or []) if str(item).strip()}
            units = [dict(item or {}) for item in list(payload.get("units") or [])]
            known = {str(item.get("unit_id") or "") for item in units}
            missing = ids - known
            if missing:
                raise ValueError(f"cannot delete unknown unit_ids: {', '.join(sorted(missing))}")
            payload["units"] = [item for item in units if str(item.get("unit_id") or "") not in ids]
        elif name == "op_minion_contract_add_unit_acceptance_batch":
            unit_id = str(args.get("unit_id") or "")
            unit = next((item for item in list(payload.get("units") or []) if str(item.get("unit_id") or "") == unit_id), None)
            if unit is None:
                raise ValueError(f"unknown unit_id: {unit_id}")
            criteria = list(unit.get("acceptance_criteria") or [])
            for raw in list(args.get("criteria") or []):
                item = dict(raw or {})
                if not str(item.get("criterion") or "").strip():
                    raise ValueError("unit acceptance criterion text is required")
                item.setdefault("id", f"AC-{len(criteria) + 1}")
                criteria.append(item)
            unit["acceptance_criteria"] = criteria
        elif name == "op_minion_contract_set_integration":
            for field in ("topology", "integration_contract", "assumption_ledger", "risk_ledger"):
                payload[field] = deepcopy(args[field])
        elif name == "op_minion_contract_validate":
            self._validate_contract(payload)
            self._assert_revision_scope(state, payload)
            return {"message": "contract draft is valid", "valid": True}
        elif name == "op_minion_contract_submit_sketch":
            self._validate_contract(payload)
            self._assert_revision_scope(state, payload)
            return self._submit(state, payload, "architecture_bundle.json")
        else:
            raise ValueError(f"unsupported contract mutation: {name}")
        self._assert_revision_scope(state, payload)
        self._save({**state, "payload": payload})
        return {"message": "contract draft updated", "counts": _contract_counts(payload)}

    @staticmethod
    def _revision_scope_view(state: Mapping[str, Any]) -> dict[str, Any]:
        scope = dict(state.get("revision_scope") or {})
        targets = normalize_revision_targets(scope.get("write_targets") or [])
        if not targets:
            raise ValueError("this is not a scoped revision draft")
        payload = dict(state.get("payload") or {})
        return {
            "message": "bound revision scope",
            "scope": {**scope, "write_targets": [item.to_dict() for item in targets]},
            "current_values": [
                {
                    "target": target.to_dict(),
                    "value": _revision_target_value(payload, target.section, target.target_id),
                }
                for target in targets
            ],
        }

    @staticmethod
    def _assert_revision_scope(state: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
        scope = dict(state.get("revision_scope") or {})
        if not scope:
            return
        allowed = normalize_revision_targets(scope.get("write_targets") or [])
        if not allowed:
            raise ValueError("scoped revision has no writable semantic targets")
        base = dict(state.get("base_payload") or {})
        if not base:
            raise ValueError("scoped revision is missing its immutable base payload")
        forbidden = [
            item.to_dict()
            for item in contract_revision_changes(base, payload)
            if not revision_target_allows(item, allowed)
        ]
        if forbidden:
            raise ValueError("revision changed a target outside its bound scope: " + json.dumps(forbidden, sort_keys=True))

    def _load(self) -> dict[str, Any]:
        if self.path.is_file():
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
        return {"schema_version": "1", "stage": self.stage, "lifecycle": "editing", "payload": _empty_contract() if self.stage == "contract" else {}}

    def _save(self, state: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(dict(state), ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def _validated_or_submitted(self, submit: bool, state: dict[str, Any], payload: dict[str, Any], filename: str) -> dict[str, Any]:
        if submit:
            return self._submit(state, payload, filename)
        return {"message": f"{self.stage} draft is valid", "valid": True}

    def _submit(self, state: dict[str, Any], payload: Mapping[str, Any], filename: str) -> dict[str, Any]:
        role = "primary"
        if self.stage == "architect" and filename != "architecture_bundle.json":
            role = "supporting"
        artifact = _write_minion_artifact(
            self.workspace,
            {
                "relative_path": filename,
                "title": f"V2 {self.stage} submission",
                "role": role,
                "mime_type": "application/json",
                "overwrite": True,
                "content": json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            },
        )
        _append_unique_artifact(self.produced_artifacts, artifact)
        self._save({**state, "payload": dict(payload), "lifecycle": "submitted", "submitted_artifact": artifact})
        return {"message": f"{self.stage} submitted", "submitted": True, "artifact": artifact}

    def _require_stage(self, expected: str) -> None:
        if self.stage not in {expected, "architect", "architect_planning"}:
            raise ValueError(f"capability requires {expected} stage, got {self.stage}")

    @staticmethod
    def _validate_contract(payload: Mapping[str, Any]) -> None:
        required = {
            "global_constraints",
            "design_decisions",
            "gate_checks",
            "units",
            "cross_unit_contracts",
            "topology",
            "integration_contract",
            "assumption_ledger",
            "risk_ledger",
        }
        missing = sorted(required - set(payload))
        if missing:
            raise ValueError("contract draft missing fields: " + ", ".join(missing))
        for field_name in ("global_constraints", "design_decisions", "gate_checks", "cross_unit_contracts"):
            _require_unique_stable_ids(list(payload.get(field_name) or []), field_name=field_name)
        units = [validate_unit_contract(dict(item), complexity_policy=ComplexityBudgetPolicy()) for item in list(payload.get("units") or [])]
        if not units:
            raise ValueError("contract draft requires at least one unit")
        unit_ids = {str(item["unit_id"]) for item in units}
        if len(unit_ids) != len(units):
            raise ValueError("contract draft has duplicate unit IDs")
        depends_on = {str(key): [str(item) for item in list(value or [])] for key, value in dict(dict(payload.get("topology") or {}).get("depends_on") or {}).items()}
        if set(depends_on) != unit_ids:
            raise ValueError("topology must contain exactly every unit_id")
        unknown = {item for values in depends_on.values() for item in values if item not in unit_ids}
        if unknown:
            raise ValueError("topology references unknown units: " + ", ".join(sorted(unknown)))
        _assert_acyclic(depends_on)
        _reject_implementation_fields(payload)


def _builder_stage_for_capability(name: str) -> str:
    if name in REQUIREMENTS_BUILDER_CAPABILITIES:
        return "requirements"
    if name in CONTRACT_SKETCH_BUILDER_CAPABILITIES:
        return "contract"
    if name in REVISION_CONTRACT_BUILDER_CAPABILITIES:
        return "contract"
    return ""


def _upsert_stable_id_items(
    existing: list[Any],
    incoming: list[Any],
    *,
    prefix: str,
    field_name: str,
) -> list[dict[str, Any]]:
    result = [dict(item or {}) for item in existing]
    _require_unique_stable_ids(result, field_name=field_name)
    positions = {str(item["id"]): index for index, item in enumerate(result)}
    incoming_ids: set[str] = set()
    next_index = len(result) + 1
    for raw in incoming:
        item = dict(raw or {})
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            while f"{prefix}-{next_index}" in positions or f"{prefix}-{next_index}" in incoming_ids:
                next_index += 1
            item_id = f"{prefix}-{next_index}"
            item["id"] = item_id
            next_index += 1
        if item_id in incoming_ids:
            raise ValueError(f"duplicate {field_name} id in batch: {item_id}")
        if item_id in positions:
            result[positions[item_id]] = item
        else:
            positions[item_id] = len(result)
            result.append(item)
        incoming_ids.add(item_id)
    return result


def _require_unique_stable_ids(items: list[Any], *, field_name: str) -> None:
    seen: set[str] = set()
    for raw in items:
        item_id = str(dict(raw or {}).get("id") or "").strip()
        if not item_id:
            raise ValueError(f"{field_name} item requires a stable id")
        if item_id in seen:
            raise ValueError(f"duplicate {field_name} id: {item_id}")
        seen.add(item_id)


def _revision_target_value(payload: Mapping[str, Any], section: str, target_id: str) -> Any:
    collection_sections = {
        "constraint": ("global_constraints", "id"),
        "design_decision": ("design_decisions", "id"),
        "gate_check": ("gate_checks", "id"),
        "unit": ("units", "unit_id"),
        "cross_unit_contract": ("cross_unit_contracts", "id"),
    }
    if section in collection_sections:
        field_name, id_field = collection_sections[section]
        return next(
            (
                deepcopy(dict(item or {}))
                for item in list(payload.get(field_name) or [])
                if str(dict(item or {}).get(id_field) or "") == target_id
            ),
            None,
        )
    if section == "topology":
        values = dict(dict(payload.get("topology") or {}).get("depends_on") or {})
        return {"unit_id": target_id, "depends_on": deepcopy(values.get(target_id))}
    if section == "integration_contract" and target_id == "integration":
        return deepcopy(dict(payload.get("integration_contract") or {}))
    if section == "assumption_ledger" and target_id == "assumptions":
        return deepcopy(dict(payload.get("assumption_ledger") or {}))
    if section == "risk_ledger" and target_id == "risks":
        return deepcopy(dict(payload.get("risk_ledger") or {}))
    return None


def _empty_contract() -> dict[str, Any]:
    return {
        "global_constraints": [],
        "design_decisions": [],
        "gate_checks": [],
        "units": [],
        "cross_unit_contracts": [],
        "topology": {},
        "integration_contract": {},
        "assumption_ledger": {"assumptions": []},
        "risk_ledger": {"risks": []},
    }


def seed_contract_builder_draft(
    workspace: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    revision_scope: Mapping[str, Any] | None = None,
) -> None:
    runtime = ContractBuilderRuntime(dict(workspace), [])
    runtime._require_stage("contract")
    candidate = deepcopy(dict(payload))
    for field_name in ("global_constraints", "design_decisions", "gate_checks", "cross_unit_contracts"):
        candidate[field_name] = _collapse_seed_items_by_id(
            list(candidate.get(field_name) or []),
            field_name=field_name,
        )
    runtime._validate_contract(candidate)
    state: dict[str, Any] = {
        "schema_version": "1",
        "stage": "contract",
        "lifecycle": "editing",
        "payload": candidate,
    }
    if revision_scope is not None:
        scope = dict(revision_scope)
        normalized = normalize_revision_targets(scope.get("write_targets") or [])
        if not normalized:
            raise ValueError("revision scope requires at least one write target")
        state["revision_scope"] = {**scope, "write_targets": [item.to_dict() for item in normalized]}
        state["base_payload"] = deepcopy(candidate)
    runtime._save(state)


def _contract_counts(payload: Mapping[str, Any]) -> dict[str, int]:
    return {field: len(list(payload.get(field) or [])) for field in ("global_constraints", "design_decisions", "gate_checks", "units", "cross_unit_contracts")}


def _collapse_seed_items_by_id(items: list[Any], *, field_name: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for raw in items:
        item = dict(raw or {})
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            raise ValueError(f"{field_name} item requires a stable id")
        if item_id in positions:
            result[positions[item_id]] = item
        else:
            positions[item_id] = len(result)
            result.append(item)
    return result


def _validate_architecture_review(payload: Mapping[str, Any]) -> None:
    verdict = str(payload.get("verdict") or "")
    findings = list(payload.get("findings") or [])
    if verdict not in {"PASS", "FAIL"}:
        raise ValueError("review verdict must be PASS or FAIL")
    if verdict == "PASS" and findings:
        raise ValueError("PASS architecture review cannot contain findings")
    if verdict == "FAIL" and not findings:
        raise ValueError("FAIL architecture review requires typed findings")
    allowed_kinds = {"requirements_defect", "contract_defect", "architecture_defect"}
    allowed_severities = {"error", "warning"}
    for index, raw in enumerate(findings):
        finding = dict(raw or {})
        kind = str(finding.get("finding_kind") or "")
        summary = str(finding.get("summary") or "").strip()
        severity = str(finding.get("severity") or "error")
        if kind not in allowed_kinds:
            raise ValueError(f"finding {index} has invalid finding_kind: {kind or '<empty>'}")
        if not summary:
            raise ValueError(f"finding {index} requires a non-empty summary")
        if severity not in allowed_severities:
            raise ValueError(f"finding {index} has invalid severity: {severity}")
        try:
            targets = normalize_revision_targets(finding.get("revision_targets") or [])
        except ValueError as exc:
            raise ValueError(f"finding {index} has invalid revision_targets: {exc}") from exc
        if not targets:
            raise ValueError(f"finding {index} requires at least one revision target")


def _reject_implementation_fields(value: Any) -> None:
    forbidden = {"milestone", "milestones", "implementation_steps", "implementation_checklist", "test_matrix", "function_steps"}
    if isinstance(value, Mapping):
        found = forbidden & set(value)
        if found:
            raise ValueError("Contract Sketch contains implementation-level fields: " + ", ".join(sorted(found)))
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
