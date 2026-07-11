from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.v2.architecture import (
    EVIDENCE_SOURCE_KINDS,
    ComplexityBudgetPolicy,
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

EVIDENCE_BUILDER_CAPABILITIES = (
    "op_minion_evidence_add_batch",
    "op_minion_evidence_replace_batch",
    "op_minion_evidence_validate",
    "op_minion_evidence_submit",
)

CONTRACT_SKETCH_BUILDER_CAPABILITIES = (
    "op_minion_contract_read",
    "op_minion_contract_get",
    "op_minion_contract_validate",
    "op_minion_contract_add_gate_checks_batch",
    "op_minion_contract_add_constraints_batch",
    "op_minion_contract_add_design_decisions_batch",
    "op_minion_contract_add_unit_outlines_batch",
    "op_minion_contract_replace_unit_outlines_batch",
    "op_minion_contract_add_unit_acceptance_batch",
    "op_minion_contract_add_cross_unit_contracts_batch",
    "op_minion_contract_set_integration",
    "op_minion_contract_submit_sketch",
)

ARCHITECTURE_REVIEW_BUILDER_CAPABILITIES = (
    "op_minion_architecture_review_submit",
)

CONTRACT_BUILDER_CAPABILITIES = (
    *REQUIREMENTS_BUILDER_CAPABILITIES,
    *EVIDENCE_BUILDER_CAPABILITIES,
    *CONTRACT_SKETCH_BUILDER_CAPABILITIES,
    *ARCHITECTURE_REVIEW_BUILDER_CAPABILITIES,
)


def _batch_schema(name: str, item_schema: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {name: {"type": "array", "items": dict(item_schema or {"type": "object"})}},
        "required": [name],
        "additionalProperties": False,
    }


CONTRACT_BUILDER_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_minion_requirements_replace_batch": {
        "name": "op_minion_requirements_replace_batch",
        "description": "Replace the requirements draft transactionally. Preserve scope, non-goals, explicit API/file/numeric obligations, and every enumerated case as atomic requirements; never compress away a user-visible value. Stable requirement IDs are assigned when omitted.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "requirements": {"type": "array", "items": {"type": "object"}},
                "open_clarifications": {"type": "array"},
                "source_coverage": {"type": "array"},
            },
            "required": ["requirements"],
            "additionalProperties": False,
        },
    },
    "op_minion_requirements_validate": {"name": "op_minion_requirements_validate", "description": "Validate the bound requirements draft.", "parameters_schema": {"type": "object", "additionalProperties": False}},
    "op_minion_requirements_submit": {"name": "op_minion_requirements_submit", "description": "Validate and freeze requirements.json. This is the only valid requirements completion path.", "parameters_schema": {"type": "object", "additionalProperties": False}},
    "op_minion_evidence_replace_batch": {
        "name": "op_minion_evidence_replace_batch",
        "description": "Replace the Evidence Catalog transactionally. Every local item needs a line range or content SHA.",
        "parameters_schema": _batch_schema("evidence"),
    },
    "op_minion_evidence_add_batch": {
        "name": "op_minion_evidence_add_batch",
        "description": "Append one inspected source cluster to the Evidence Catalog transactionally. Persist evidence as research proceeds instead of retaining the complete catalog in context. Evidence IDs must remain unique; every local item needs a line range or content SHA.",
        "parameters_schema": _batch_schema("evidence"),
    },
    "op_minion_evidence_validate": {"name": "op_minion_evidence_validate", "description": "Validate the bound evidence draft.", "parameters_schema": {"type": "object", "additionalProperties": False}},
    "op_minion_evidence_submit": {"name": "op_minion_evidence_submit", "description": "Validate and freeze evidence_catalog.json. This is the only valid research completion path.", "parameters_schema": {"type": "object", "additionalProperties": False}},
    "op_minion_contract_read": {"name": "op_minion_contract_read", "description": "Read the normalized bound Contract Sketch draft.", "parameters_schema": {"type": "object", "additionalProperties": False}},
    "op_minion_contract_get": {
        "name": "op_minion_contract_get",
        "description": "Read one Unit Contract from the bound draft by unit_id.",
        "parameters_schema": {"type": "object", "properties": {"unit_id": {"type": "string"}}, "required": ["unit_id"], "additionalProperties": False},
    },
    "op_minion_contract_validate": {"name": "op_minion_contract_validate", "description": "Run all mechanical Contract Sketch checks without submitting.", "parameters_schema": {"type": "object", "additionalProperties": False}},
    "op_minion_contract_add_gate_checks_batch": {"name": "op_minion_contract_add_gate_checks_batch", "description": "Add source-derived architecture and end-to-end gate checks in one transaction. Include cross-unit dataflow, lifecycle, ownership, invalid-state/error, and final delivery handoffs; do not add implementation checklists.", "parameters_schema": _batch_schema("gate_checks")},
    "op_minion_contract_add_constraints_batch": {"name": "op_minion_contract_add_constraints_batch", "description": "Add global constraints in one transaction.", "parameters_schema": _batch_schema("constraints")},
    "op_minion_contract_add_design_decisions_batch": {"name": "op_minion_contract_add_design_decisions_batch", "description": "Add design decisions with rationale and downstream impact in one transaction.", "parameters_schema": _batch_schema("design_decisions")},
    "op_minion_contract_add_unit_outlines_batch": {
        "name": "op_minion_contract_add_unit_outlines_batch",
        "description": "Add complete Unit Contracts transactionally: boundaries, directional provided/consumed interfaces, ownership, lifecycle/state, invariants, requirement/evidence refs, complexity, and work-start blockers. owned_area must contain canonical target-owned paths/logical areas; declared read-only truth sources belong in reference_only_paths. Shared concrete files need one owner. Milestones and implementation steps are forbidden.",
        "parameters_schema": _batch_schema("units"),
    },
    "op_minion_contract_replace_unit_outlines_batch": {
        "name": "op_minion_contract_replace_unit_outlines_batch",
        "description": "Replace complete existing Unit Contracts by unit_id in a preseeded revision draft. Use only for units named by the bound finding; every unit must remain complete and valid. Unknown unit IDs are rejected so unrelated topology cannot be invented through this repair tool.",
        "parameters_schema": _batch_schema("units"),
    },
    "op_minion_contract_add_unit_acceptance_batch": {
        "name": "op_minion_contract_add_unit_acceptance_batch",
        "description": "Add unit-boundary acceptance obligations, never implementation milestones or a test matrix.",
        "parameters_schema": {
            "type": "object",
            "properties": {"unit_id": {"type": "string"}, "criteria": {"type": "array", "items": {"type": "object"}}},
            "required": ["unit_id", "criteria"],
            "additionalProperties": False,
        },
    },
    "op_minion_contract_add_cross_unit_contracts_batch": {"name": "op_minion_contract_add_cross_unit_contracts_batch", "description": "Add producer/consumer cross-unit contracts transactionally. State direction, data shape, validity timing, ownership transfer/borrowing, lifecycle handoff, compatibility, and invalid-state/error behavior so neither side must inspect sibling internals.", "parameters_schema": _batch_schema("cross_unit_contracts")},
    "op_minion_contract_set_integration": {
        "name": "op_minion_contract_set_integration",
        "description": "Set topology, integration contract, assumptions, and risks in one transaction.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "topology": {"type": "object"},
                "integration_contract": {"type": "object"},
                "assumption_ledger": {"type": "object"},
                "risk_ledger": {"type": "object"},
            },
            "required": ["topology", "integration_contract", "assumption_ledger", "risk_ledger"],
            "additionalProperties": False,
        },
    },
    "op_minion_contract_submit_sketch": {"name": "op_minion_contract_submit_sketch", "description": "Validate and freeze architecture_bundle.json. This is the only valid planning completion path.", "parameters_schema": {"type": "object", "additionalProperties": False}},
    "op_minion_architecture_review_submit": {
        "name": "op_minion_architecture_review_submit",
        "description": "Submit the typed architecture verdict after claim-driven tracing from requirements/evidence to unit and cross-unit contracts. The reviewer cannot modify the contract or replace a targeted trace with a competing design.",
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
                                    "evidence_gap",
                                    "contract_defect",
                                    "architecture_defect",
                                ],
                            },
                            "summary": {"type": "string", "minLength": 1},
                            "refs": {"type": "array", "items": {"type": "string"}},
                            "severity": {"type": "string", "enum": ["error", "warning"]},
                        },
                        "required": ["finding_kind", "summary"],
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
        if self.stage not in {"requirements", "evidence", "contract", "architecture_review"}:
            raise ValueError("contract_builder_stage is not bound")
        root_text = str(workspace.get("artifact_stage_dir") or workspace.get("artifact_dir") or "").strip()
        if not root_text:
            raise ValueError("contract builder requires artifact_stage_dir")
        root = Path(root_text)
        self.path = root / ".contract_builder" / f"{self.stage}.json"

    def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
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
        if name == "op_minion_evidence_replace_batch":
            self._require_stage("evidence")
            evidence = []
            for index, raw in enumerate(list(args.get("evidence") or []), start=1):
                item = dict(raw or {})
                item.setdefault("evidence_id", f"E-{index}")
                evidence.append(item)
            state["payload"] = {"evidence": evidence}
            self._validate_evidence(state["payload"])
            self._save(state)
            return {"message": "evidence draft replaced", "count": len(evidence)}
        if name == "op_minion_evidence_add_batch":
            self._require_stage("evidence")
            evidence = list(dict(state.get("payload") or {}).get("evidence") or [])
            existing_ids = {str(item.get("evidence_id") or "") for item in evidence}
            additions = []
            for raw in list(args.get("evidence") or []):
                item = dict(raw or {})
                evidence_id = str(item.get("evidence_id") or "").strip()
                if not evidence_id:
                    evidence_id = f"E-{len(evidence) + len(additions) + 1}"
                    item["evidence_id"] = evidence_id
                if evidence_id in existing_ids:
                    raise ValueError(f"duplicate evidence_id: {evidence_id}")
                existing_ids.add(evidence_id)
                additions.append(item)
            candidate = {"evidence": [*evidence, *additions]}
            self._validate_evidence(candidate)
            state["payload"] = candidate
            self._save(state)
            return {
                "message": "evidence batch appended",
                "added_count": len(additions),
                "total_count": len(candidate["evidence"]),
            }
        if name in {"op_minion_evidence_validate", "op_minion_evidence_submit"}:
            self._require_stage("evidence")
            payload = dict(state.get("payload") or {})
            self._validate_evidence(payload, require_complete=True)
            return self._validated_or_submitted(name.endswith("submit"), state, payload, "evidence_catalog.json")
        if name == "op_minion_contract_read":
            self._require_stage("contract")
            return {"message": "contract draft", "draft": deepcopy(state.get("payload") or {})}
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
            existing = list(payload.get(field) or [])
            for raw in list(args.get(arg_name) or []):
                item = dict(raw or {})
                item.setdefault("id", f"{prefix}-{len(existing) + 1}")
                existing.append(item)
            payload[field] = existing
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
            return {"message": "contract draft is valid", "valid": True}
        elif name == "op_minion_contract_submit_sketch":
            self._validate_contract(payload)
            return self._submit(state, payload, "architecture_bundle.json")
        else:
            raise ValueError(f"unsupported contract mutation: {name}")
        self._save({**state, "payload": payload})
        return {"message": "contract draft updated", "counts": _contract_counts(payload)}

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
        artifact = _write_minion_artifact(
            self.workspace,
            {
                "relative_path": filename,
                "title": f"V2 {self.stage} submission",
                "role": "primary",
                "mime_type": "application/json",
                "overwrite": True,
                "content": json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            },
        )
        _append_unique_artifact(self.produced_artifacts, artifact)
        self._save({**state, "payload": dict(payload), "lifecycle": "submitted", "submitted_artifact": artifact})
        return {"message": f"{self.stage} submitted", "submitted": True, "artifact": artifact}

    def _require_stage(self, expected: str) -> None:
        if self.stage != expected:
            raise ValueError(f"capability requires {expected} stage, got {self.stage}")

    def _validate_evidence(self, payload: Mapping[str, Any], *, require_complete: bool = False) -> None:
        seen: set[str] = set()
        supported_requirement_ids: set[str] = set()
        bound_requirement_ids = self._bound_requirement_ids()
        for raw in list(payload.get("evidence") or []):
            item = dict(raw or {})
            evidence_id = str(item.get("evidence_id") or "").strip()
            if not evidence_id or evidence_id in seen:
                raise ValueError(f"invalid or duplicate evidence_id: {evidence_id or '<empty>'}")
            if not str(item.get("location") or "").strip() or not str(item.get("summary") or "").strip():
                raise ValueError(f"evidence {evidence_id} requires location and summary")
            source_kind = str(item.get("source_kind") or "local")
            if source_kind not in EVIDENCE_SOURCE_KINDS:
                raise ValueError(f"invalid evidence source_kind: {source_kind}")
            if source_kind == "local" and not item.get("content_sha256") and not (item.get("line_start") and item.get("line_end")):
                raise ValueError(f"local evidence {evidence_id} requires line range or content_sha256")
            requirement_ids = {
                str(value).strip()
                for value in list(item.get("supports_requirement_ids") or [])
                if str(value).strip()
            }
            if not requirement_ids:
                raise ValueError(f"evidence {evidence_id} must support at least one requirement")
            unknown_ids = requirement_ids - bound_requirement_ids if bound_requirement_ids else set()
            if unknown_ids:
                raise ValueError(
                    f"evidence {evidence_id} references unknown requirements: " + ", ".join(sorted(unknown_ids))
                )
            supported_requirement_ids.update(requirement_ids)
            seen.add(evidence_id)
        if require_complete and bound_requirement_ids:
            missing_ids = bound_requirement_ids - supported_requirement_ids
            if missing_ids:
                raise ValueError("requirements lack supporting evidence: " + ", ".join(sorted(missing_ids)))

    def _bound_requirement_ids(self) -> set[str]:
        for raw in list(self.workspace.get("reference_paths") or []):
            reference = dict(raw or {})
            if str(reference.get("name") or "") != "requirements":
                continue
            path = Path(str(reference.get("path") or ""))
            if not path.is_file():
                return set()
            payload = json.loads(path.read_text(encoding="utf-8"))
            return {
                str(item.get("requirement_id") or "").strip()
                for item in list(dict(payload or {}).get("requirements") or [])
                if str(item.get("requirement_id") or "").strip()
            }
        return set()

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


def seed_contract_builder_draft(workspace: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    runtime = ContractBuilderRuntime(dict(workspace), [])
    runtime._require_stage("contract")
    candidate = deepcopy(dict(payload))
    runtime._validate_contract(candidate)
    runtime._save(
        {
            "schema_version": "1",
            "stage": "contract",
            "lifecycle": "editing",
            "payload": candidate,
        }
    )


def _contract_counts(payload: Mapping[str, Any]) -> dict[str, int]:
    return {field: len(list(payload.get(field) or [])) for field in ("global_constraints", "design_decisions", "gate_checks", "units", "cross_unit_contracts")}


def _validate_architecture_review(payload: Mapping[str, Any]) -> None:
    verdict = str(payload.get("verdict") or "")
    findings = list(payload.get("findings") or [])
    if verdict not in {"PASS", "FAIL"}:
        raise ValueError("review verdict must be PASS or FAIL")
    if verdict == "PASS" and findings:
        raise ValueError("PASS architecture review cannot contain findings")
    if verdict == "FAIL" and not findings:
        raise ValueError("FAIL architecture review requires typed findings")
    allowed_kinds = {"requirements_defect", "evidence_gap", "contract_defect", "architecture_defect"}
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
