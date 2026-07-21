from __future__ import annotations

from pal.execution.generated_tool_models import (
    MinionV2ContractBuilderOpMinionArchitectureReviewSubmitInput,
    MinionV2ContractBuilderOpMinionContractAddAssumptionInput,
    MinionV2ContractBuilderOpMinionContractAddConstraintInput,
    MinionV2ContractBuilderOpMinionContractAddCrossUnitContractInput,
    MinionV2ContractBuilderOpMinionContractAddDesignDecisionInput,
    MinionV2ContractBuilderOpMinionContractAddGateCheckInput,
    MinionV2ContractBuilderOpMinionContractAddRiskInput,
    MinionV2ContractBuilderOpMinionContractSetIntegrationInput,
    MinionV2ContractBuilderOpMinionContractSubmitInput,
    MinionV2ContractBuilderOpMinionContractUnitAddInterfaceInput,
    MinionV2ContractBuilderOpMinionContractUnitAddRuleInput,
    MinionV2ContractBuilderOpMinionContractUnitRemoveInput,
    MinionV2ContractBuilderOpMinionContractUnitSetLifecycleInput,
    MinionV2ContractBuilderOpMinionContractUnitSetOwnershipInput,
    MinionV2ContractBuilderOpMinionContractUnitSetStateInput,
    MinionV2ContractBuilderOpMinionContractUnitUpsertInput,
)

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.v2.architecture import (
    contract_revision_changes,
    normalize_revision_targets,
    revision_target_allows,
)
from pal.minion.v2.review_findings import (
    ADD_FINDING_CAPABILITY,
    empty_review_draft,
    structured_findings,
)
from pal.minion.v2.submission_drafts import (
    SubmissionDraftContext,
    SubmissionDraftStore,
    assert_authoring_schema_budget,
)
from pal.minion.workspace_tools import _append_unique_artifact, _write_minion_artifact
from pal.shared import RuntimeStatus


CONTRACT_SKETCH_BUILDER_CAPABILITIES = (
    "op_minion_contract_unit_upsert",
    "op_minion_contract_unit_add_interface",
    "op_minion_contract_unit_set_ownership",
    "op_minion_contract_unit_set_lifecycle",
    "op_minion_contract_unit_set_state",
    "op_minion_contract_unit_add_rule",
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
    ADD_FINDING_CAPABILITY,
    "op_minion_architecture_review_submit",
)
CONTRACT_BUILDER_CAPABILITIES = (
    *CONTRACT_SKETCH_BUILDER_CAPABILITIES,
    *ARCHITECTURE_REVIEW_BUILDER_CAPABILITIES,
)
ARCHITECT_BUILDER_CAPABILITIES = (
    *CONTRACT_SKETCH_BUILDER_CAPABILITIES,
    "op_minion_architecture_ask_user",
)


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
_NAME = _schema({"name": {"type": "string", "minLength": 1}}, required=("name",))
_CONSTRAINT = _schema(
    {
        "name": {"type": "string", "minLength": 1},
        "constraint": {"type": "string", "minLength": 1},
        "rationale": {"type": "string"},
    },
    required=("name", "constraint"),
)
_DECISION = _schema(
    {
        "name": {"type": "string", "minLength": 1},
        "decision": {"type": "string", "minLength": 1},
        "rationale": {"type": "string", "minLength": 1},
        "downstream_impact": {"type": "string"},
    },
    required=("name", "decision", "rationale"),
)
_GATE = _schema(
    {
        "name": {"type": "string", "minLength": 1},
        "check": {"type": "string", "minLength": 1},
        "scope": {"type": "string"},
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
CONTRACT_BUILDER_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_minion_contract_unit_upsert": {"name": "op_contract_unit_upsert", "description": "Create or update one semantic unit shell and its construction dependencies. behavior_kind is stateless, resource_owner, service, workflow, or adapter. owned_area names this unit's boundary; reference_only_paths are readable truth sources. Do not include implementation steps.", "InputModel": MinionV2ContractBuilderOpMinionContractUnitUpsertInput},
    "op_minion_contract_unit_add_interface": {"name": "op_contract_unit_add_interface", "description": "Add one provided or consumed interface contract to one unit. direction is provided or consumed; data_shape, valid_when, lifetime, ownership, error_behavior, and compatibility describe the boundary.", "InputModel": MinionV2ContractBuilderOpMinionContractUnitAddInterfaceInput},
    "op_minion_contract_unit_set_ownership": {"name": "op_contract_unit_set_ownership", "description": "Set one unit's ownership rule as a semantic statement.", "InputModel": MinionV2ContractBuilderOpMinionContractUnitSetOwnershipInput},
    "op_minion_contract_unit_set_lifecycle": {"name": "op_contract_unit_set_lifecycle", "description": "Set one unit's lifecycle model. Stateless units may state process/import lifetime.", "InputModel": MinionV2ContractBuilderOpMinionContractUnitSetLifecycleInput},
    "op_minion_contract_unit_set_state": {"name": "op_contract_unit_set_state", "description": "Set one unit's state model. Stateless units must use description=stateless.", "InputModel": MinionV2ContractBuilderOpMinionContractUnitSetStateInput},
    "op_minion_contract_unit_add_rule": {"name": "op_contract_unit_add_rule", "description": "Add one rule. kind is invariant, error_behavior, compatibility, dependency_constraint, verification_obligation, or split_condition; statement is required and condition/expected are optional.", "InputModel": MinionV2ContractBuilderOpMinionContractUnitAddRuleInput},
    "op_minion_contract_unit_remove": {"name": "op_contract_unit_remove", "description": "Remove one semantic unit during a scoped revision.", "InputModel": MinionV2ContractBuilderOpMinionContractUnitRemoveInput},
    "op_minion_contract_add_constraint": {"name": "op_contract_add_constraint", "description": "Add or replace one named global constraint.", "InputModel": MinionV2ContractBuilderOpMinionContractAddConstraintInput},
    "op_minion_contract_add_design_decision": {"name": "op_contract_add_design_decision", "description": "Add or replace one named architecture decision.", "InputModel": MinionV2ContractBuilderOpMinionContractAddDesignDecisionInput},
    "op_minion_contract_add_gate_check": {"name": "op_contract_add_gate_check", "description": "Add or replace one module-boundary or end-to-end gate, not an implementation checklist.", "InputModel": MinionV2ContractBuilderOpMinionContractAddGateCheckInput},
    "op_minion_contract_add_cross_unit_contract": {"name": "op_contract_add_cross_unit_contract", "description": "Add or replace one directional cross-unit data/lifecycle contract.", "InputModel": MinionV2ContractBuilderOpMinionContractAddCrossUnitContractInput},
    "op_minion_contract_set_integration": {"name": "op_contract_set_integration", "description": "Set the real end-to-end delivery entrypoint, dataflow, completion, and failure behavior.", "InputModel": MinionV2ContractBuilderOpMinionContractSetIntegrationInput},
    "op_minion_contract_add_assumption": {"name": "op_contract_add_assumption", "description": "Add or replace one named assumption with owner, impact, and verification plan.", "InputModel": MinionV2ContractBuilderOpMinionContractAddAssumptionInput},
    "op_minion_contract_add_risk": {"name": "op_contract_add_risk", "description": "Add or replace one named risk and mitigation. severity is low, medium, high, or critical.", "InputModel": MinionV2ContractBuilderOpMinionContractAddRiskInput},
    "op_minion_contract_submit": {"name": "op_contract_submit", "description": "Submit the current Architecture Contract Draft. Takes no arguments; Manager checks only names, topology, and structural safety.", "InputModel": MinionV2ContractBuilderOpMinionContractSubmitInput},
    "op_minion_architecture_review_submit": {"name": "op_architecture_review_submit", "description": "Submit the review. Takes no arguments; verdict is PASS with no findings and FAIL otherwise.", "InputModel": MinionV2ContractBuilderOpMinionArchitectureReviewSubmitInput},
}

for _tool_name, _tool_spec in CONTRACT_BUILDER_TOOL_SPECS.items():
    assert_authoring_schema_budget(
        _tool_spec["InputModel"].model_json_schema(mode="validation", union_format="primitive_type_array"),
        owner=_tool_name,
    )


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
        if name == "op_minion_contract_submit":
            output, version = _compile_contract(call, workspace)
            return _publish(call, workspace, produced_artifacts, output, version=version, draft_kind="contract", filename="architecture_bundle.json")
        if name == "op_minion_architecture_review_submit":
            output, version = _compile_review(call, workspace)
            return _publish(call, workspace, produced_artifacts, output, version=version, draft_kind="architecture_review", filename="architecture_review.json")
        if stage in {"architect", "architect_planning", "contract"}:
            return _mutate_contract(call, workspace)
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
            **dict(args),
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


def _compile_review(call: CanonicalToolCall, workspace: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    _require_no_args(call)
    context, store = _store(workspace, "architecture_review")
    snapshot = store.read(context, seed=_review_seed())
    findings = structured_findings(snapshot.payload)
    output = {"schema_version": "2", "verdict": "FAIL" if findings else "PASS", "findings": findings}
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
    context, store = _store(workspace, draft_kind)
    if store.uses_role_gateway:
        store.mark_submitted(
            context,
            expected_version=version,
            submission_payload=dict(output),
        )
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
    if not store.uses_role_gateway:
        store.mark_submitted(
            context,
            expected_version=version,
            submission_payload=dict(output),
        )
    return _ok(call, f"{draft_kind} submitted. Stop now.", {"submitted": True})


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
    units = [dict(item) for item in list(payload.get("units") or []) if isinstance(item, Mapping)]
    if not units:
        raise ValueError("Architecture Contract requires at least one unit")
    unit_names = [str(item.get("unit_id") or "").strip() for item in units]
    if any(not name for name in unit_names):
        raise ValueError("Architecture Contract unit names must not be empty")
    names = set(unit_names)
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
    integration_unknown = set(str(item) for item in list(integration.get("depends_on") or [])) - names
    if integration_unknown:
        raise ValueError("integration contract references unknown units: " + ", ".join(sorted(integration_unknown)))
    _reject_implementation_fields(payload)


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
        "verification_obligations": [],
        "split_conditions": [],
    }


def _contract_seed(workspace: Mapping[str, Any]) -> dict[str, Any]:
    base = workspace.get("contract_revision_base_payload")
    contract = deepcopy(dict(base)) if isinstance(base, Mapping) else _empty_contract()
    return {"definitions": {"contract": contract}, "evidence": {}, "findings": [], "summary": {}}


def _review_seed() -> dict[str, Any]:
    return empty_review_draft()


def _stage(workspace: Mapping[str, Any]) -> str:
    value = str(workspace.get("contract_builder_stage") or "").strip()
    if value not in {"architect", "architect_planning", "contract", "architecture_review"}:
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
    del prefix
    return semantic_name


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
    structured_findings({"findings": findings})


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
