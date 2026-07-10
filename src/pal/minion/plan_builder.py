from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.plan_store import coerce_plan_ref, plan_revision_from_payload, resolve_plan_ref_path
from pal.minion.work_order import (
    PlanArtifact,
    _normalized_owned_area_key,
    dispatchable_plan_validation,
    new_work_id,
    normalize_plan_write_areas,
    plan_write_area_covers,
    planner_requirements,
    validate_dispatchable_plan_artifact,
    validate_final_plan_artifact,
    work_start_topology_validation,
)
from pal.minion.workspace_tools import _append_unique_artifact, _write_minion_artifact
from pal.shared import RuntimeStatus


PLAN_BUILDER_READ_CAPABILITIES: tuple[str, ...] = (
    "op_minion_plan_read",
    "op_minion_plan_find",
    "op_minion_plan_get",
    "op_minion_plan_validate",
)

PLAN_BUILDER_INITIAL_CAPABILITIES: tuple[str, ...] = (
    "op_minion_plan_begin",
    "op_minion_plan_add_constraints_batch",
    "op_minion_plan_add_constraint",
    "op_minion_plan_add_design_decision",
    "op_minion_plan_add_module_outline",
    "op_minion_plan_add_module_outlines_batch",
    "op_minion_plan_begin_module",
    "op_minion_plan_add_module_interface",
    "op_minion_plan_add_milestone_outline",
    "op_minion_plan_begin_milestone",
    "op_minion_plan_add_acceptance_criteria_batch",
    "op_minion_plan_add_acceptance_criterion",
    "op_minion_plan_end_milestone",
    "op_minion_plan_end_module",
    "op_minion_plan_update_plan",
    "op_minion_plan_validate_and_submit_for_review",
)


def _plan_write_area_schema(field_name: str) -> dict[str, Any]:
    if field_name == "owned_area":
        description = (
            "Canonical module write boundaries only: repo-relative file paths, directories ending in '/', globs, or "
            "explicit logical areas such as domain:<id> or component:<id>. Do not append explanations to a path; "
            "put those in ownership or scope_guard."
        )
    else:
        description = (
            "Canonical repo-relative paths expected to change in this milestone. Every value must be covered by the "
            "module owned_area. Do not append symbols, actions, or parenthetical explanations; put those in task or scope_guard."
        )
    return {
        "type": "array",
        "description": description,
        "items": {"type": "string", "description": description},
    }


PLAN_BUILDER_SKETCH_CAPABILITIES: tuple[str, ...] = (
    "op_minion_plan_read",
    "op_minion_plan_get",
    "op_minion_plan_validate",
    "op_minion_plan_checkout",
    "op_minion_plan_add_gate_check",
    "op_minion_plan_update_gate_check",
    "op_minion_plan_delete_gate_check",
    "op_minion_plan_add_constraints_batch",
    "op_minion_plan_add_constraint",
    "op_minion_plan_update_constraint",
    "op_minion_plan_delete_constraint",
    "op_minion_plan_add_design_decision",
    "op_minion_plan_update_design_decision",
    "op_minion_plan_delete_design_decision",
    "op_minion_plan_add_sketch_module_outline",
    "op_minion_plan_add_sketch_module_outlines_batch",
    "op_minion_plan_add_module_interface",
    "op_minion_plan_add_module_acceptance_criteria_batch",
    "op_minion_plan_add_module_acceptance_criterion",
    "op_minion_plan_update_module",
    "op_minion_plan_delete_module",
    "op_minion_plan_merge_sketch_modules",
    "op_minion_plan_submit_sketch",
)


PLAN_BUILDER_MODULE_DETAIL_CAPABILITIES: tuple[str, ...] = (
    *PLAN_BUILDER_READ_CAPABILITIES,
    "op_minion_plan_add_module_interface",
    "op_minion_plan_add_milestone_outline",
    "op_minion_plan_add_acceptance_criteria_batch",
    "op_minion_plan_add_acceptance_criterion",
    "op_minion_plan_update_milestone",
    "op_minion_plan_delete_milestone",
    "op_minion_plan_update_acceptance_criterion",
    "op_minion_plan_delete_acceptance_criterion",
    "op_minion_plan_replace_milestone_acceptance_criteria",
    "op_minion_plan_validate_and_submit_for_review",
)


PLAN_BUILDER_WRITE_CAPABILITIES: tuple[str, ...] = (
    "op_minion_plan_begin",
    "op_minion_plan_checkout",
    "op_minion_plan_add_gate_check",
    "op_minion_plan_update_gate_check",
    "op_minion_plan_delete_gate_check",
    "op_minion_plan_add_constraints_batch",
    "op_minion_plan_add_constraint",
    "op_minion_plan_update_constraint",
    "op_minion_plan_delete_constraint",
    "op_minion_plan_add_design_decision",
    "op_minion_plan_update_design_decision",
    "op_minion_plan_delete_design_decision",
    "op_minion_plan_add_module_outline",
    "op_minion_plan_add_module_outlines_batch",
    "op_minion_plan_begin_module",
    "op_minion_plan_add_module_interface",
    "op_minion_plan_add_module_acceptance_criteria_batch",
    "op_minion_plan_add_module_acceptance_criterion",
    "op_minion_plan_add_milestone_outline",
    "op_minion_plan_begin_milestone",
    "op_minion_plan_add_acceptance_criteria_batch",
    "op_minion_plan_add_acceptance_criterion",
    "op_minion_plan_end_milestone",
    "op_minion_plan_end_module",
    "op_minion_plan_update_plan",
    "op_minion_plan_update_module",
    "op_minion_plan_delete_module",
    "op_minion_plan_merge_modules",
    "op_minion_plan_update_milestone",
    "op_minion_plan_delete_milestone",
    "op_minion_plan_move_milestone",
    "op_minion_plan_update_acceptance_criterion",
    "op_minion_plan_delete_acceptance_criterion",
    "op_minion_plan_replace_milestone_acceptance_criteria",
    "op_minion_plan_apply_revision_item",
    "op_minion_plan_validate_and_submit_for_review",
    "op_minion_plan_submit_for_review",
    "op_minion_plan_finalize",
)


PLAN_BUILDER_REVISION_CAPABILITIES: tuple[str, ...] = (
    *PLAN_BUILDER_READ_CAPABILITIES,
    *(
        capability
        for capability in PLAN_BUILDER_WRITE_CAPABILITIES
        if capability not in {"op_minion_plan_begin", "op_minion_plan_finalize"}
    ),
)


PLAN_BUILDER_CAPABILITIES: tuple[str, ...] = (*PLAN_BUILDER_READ_CAPABILITIES, *PLAN_BUILDER_WRITE_CAPABILITIES)


PlanBuilderAliasMapper = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class PlanBuilderAliasSpec:
    name: str
    core_tool: str
    description: str
    parameters_schema: dict[str, Any]
    map_args: PlanBuilderAliasMapper
    domain: str = ""

    def tool_spec(self) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "description": self.description,
            "parameters_schema": dict(self.parameters_schema),
        }
        if self.domain:
            payload["domain"] = self.domain
        payload["alias_for"] = self.core_tool
        return payload


PLAN_BUILDER_ALIASES: dict[str, PlanBuilderAliasSpec] = {}


def register_plan_builder_alias(spec: PlanBuilderAliasSpec) -> PlanBuilderAliasSpec:
    name = str(spec.name or "").strip()
    core_tool = str(spec.core_tool or "").strip()
    if not name.startswith("op_"):
        raise ValueError("plan builder alias name must be a capability name starting with op_")
    if name in PLAN_BUILDER_TOOL_SPECS:
        raise ValueError(f"plan builder alias conflicts with core tool: {name}")
    if core_tool not in PLAN_BUILDER_TOOL_SPECS:
        raise ValueError(f"plan builder alias core_tool is unknown: {core_tool}")
    if name == core_tool:
        raise ValueError("plan builder alias cannot target itself")
    PLAN_BUILDER_ALIASES[name] = spec
    return spec


def unregister_plan_builder_alias(name: str) -> None:
    PLAN_BUILDER_ALIASES.pop(str(name or "").strip(), None)


def plan_builder_alias(
    *,
    name: str,
    core_tool: str,
    description: str,
    parameters_schema: dict[str, Any],
    domain: str = "",
) -> Callable[[PlanBuilderAliasMapper], PlanBuilderAliasMapper]:
    def decorator(func: PlanBuilderAliasMapper) -> PlanBuilderAliasMapper:
        register_plan_builder_alias(
            PlanBuilderAliasSpec(
                name=name,
                core_tool=core_tool,
                description=description,
                parameters_schema=dict(parameters_schema),
                map_args=func,
                domain=domain,
            )
        )
        return func

    return decorator


def plan_builder_tool_specs() -> dict[str, dict[str, Any]]:
    specs = {
        **PLAN_BUILDER_TOOL_SPECS,
        **{name: spec.tool_spec() for name, spec in PLAN_BUILDER_ALIASES.items()},
    }
    for name, raw_spec in list(specs.items()):
        spec = dict(raw_spec)
        call_alias = name.removeprefix("op_minion_")
        spec["aliases"] = list(
            dict.fromkeys(
                [
                    *[str(item).strip() for item in list(spec.get("aliases") or []) if str(item).strip()],
                    call_alias,
                ]
            )
        )
        specs[name] = spec
    return specs


def plan_builder_capabilities() -> tuple[str, ...]:
    return (*PLAN_BUILDER_CAPABILITIES, *tuple(PLAN_BUILDER_ALIASES))


def is_plan_builder_capability(name: str) -> bool:
    normalized = str(name or "").strip()
    return normalized in PLAN_BUILDER_TOOL_SPECS or normalized in PLAN_BUILDER_ALIASES

_SUPPORTED_MECHANICAL_CHECK_TYPES: frozenset[str] = frozenset(
    {
        "module_count",
        "module_kind_count",
        "implementation_module_count",
        "milestone_count",
        "module_milestone_count",
        "implementation_milestone_count",
        "acceptance_count",
    }
)

_MECHANICAL_CHECK_TYPE_ALIASES: dict[str, str] = {
    "plan_module_count": "module_count",
    "total_module_count": "module_count",
}

_LOOSE_NONCOUNT_MECHANICAL_TYPES: frozenset[str] = frozenset(
    {
        "equals",
        "equality",
        "value_equals",
        "string_equals",
    }
)

_MECHANICAL_COUNT_TARGETS: dict[str, dict[str, Any]] = {
    "module": {"type": "module_count"},
    "modules": {"type": "module_count"},
    "prelude_module": {"type": "module_kind_count", "module_kind": "prelude"},
    "prelude_modules": {"type": "module_kind_count", "module_kind": "prelude"},
    "implementation_module": {"type": "implementation_module_count"},
    "implementation_modules": {"type": "implementation_module_count"},
    "join_module": {"type": "module_kind_count", "module_kind": "join"},
    "join_modules": {"type": "module_kind_count", "module_kind": "join"},
    "final_verification_join_module": {"type": "module_kind_count", "module_kind": "join"},
    "final_verification_join_modules": {"type": "module_kind_count", "module_kind": "join"},
    "milestone": {"type": "milestone_count"},
    "milestones": {"type": "milestone_count"},
    "implementation_milestone": {"type": "implementation_milestone_count"},
    "implementation_milestones": {"type": "implementation_milestone_count"},
    "acceptance_criterion": {"type": "acceptance_count"},
    "acceptance_criteria": {"type": "acceptance_count"},
}


PLAN_BUILDER_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_minion_plan_read": {
        "name": "op_minion_plan_read",
        "description": "Read a plan builder draft or submitted draft snapshot. Use this before reviewing or revising a plan.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "plan_handle": {"type": "string"},
                "plan_ref": {"type": "object", "description": "Draft/plan ref from the review target."},
                "detail": {"type": "string", "enum": ["summary", "full"], "default": "summary"},
            },
            "additionalProperties": False,
        },
    },
    "op_minion_plan_find": {
        "name": "op_minion_plan_find",
        "description": "Find plan nodes by type and text, returning mutation-safe handles and parent paths.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "plan_handle": {"type": "string"},
                "plan_ref": {"type": "object"},
                "type": {
                    "type": "string",
                    "enum": ["plan", "gate_check", "constraint", "decision", "module", "interface", "milestone", "acceptance_criterion", "any"],
                    "default": "any",
                },
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "additionalProperties": False,
        },
    },
    "op_minion_plan_get": {
        "name": "op_minion_plan_get",
        "description": "Read one plan node by handle before updating or citing it in a review finding.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "handle": {"type": "string"},
                "plan_handle": {"type": "string", "description": "Optional disambiguation when reading a plan node."},
                "plan_ref": {"type": "object", "description": "Optional submitted draft/plan ref for readonly review."},
            },
            "required": ["handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_validate": {
        "name": "op_minion_plan_validate",
        "description": "Validate the current plan draft without publishing or closing it. Use this after local edits and before submitting for review.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "plan_handle": {"type": "string"},
                "plan_ref": {"type": "object"},
            },
            "additionalProperties": False,
        },
    },
    "op_minion_plan_validate_and_submit_for_review": {
        "name": "op_minion_plan_validate_and_submit_for_review",
        "description": (
            "Validate the current draft and freeze the reviewed draft snapshot for the plan_acceptance gate in one call. "
            "Use this as the normal final planner action after all required stage nodes are closed. For a checked-out "
            "revision, pass only plan_handle; use plan_update_plan for intentional top-level edits."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "plan_handle": {"type": "string"},
                "summary": {"type": "string"},
                "system_test_plan": {"type": "array", "items": {"type": "object"}},
                "risks": {"type": "array", "items": {"type": "object"}},
                "assumptions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["plan_handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_begin": {
        "name": "op_minion_plan_begin",
        "description": (
            "Start a structured planner draft. Use this before defining modules. Pal compiles the final draft into "
            "the normal plan.json; do not hand-write plan JSON."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "Planning goal or SPEC summary."},
                "plan_id": {"type": "string", "description": "Optional stable plan id. Pal can generate one."},
                "summary": {"type": "string", "description": "Optional initial plan summary."},
                "languages": {"type": "array", "items": {"type": "string"}, "description": "Canonical implementation language ids."},
                "source_refs": {"type": "array", "items": {"type": "string"}},
                "workflow_next": {
                    "type": "object",
                    "description": (
                        "Optional manager routing hint compiled into metadata.workflow_next. Use profile/next_profile to "
                        "declare the next workflow profile for this artifact, such as software_engineering.coder or none."
                    ),
                },
            },
        },
    },
    "op_minion_plan_checkout": {
        "name": "op_minion_plan_checkout",
        "description": (
            "Create an editable revision draft from a reviewed plan_ref/draft_ref. Use this for plan review repairs; "
            "do not start a new plan when a source_plan_ref is available. In revision work orders, Pal may already bind "
            "source_plan_ref in the workspace, so calling this with an empty argument object is valid."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "source_plan_ref": {"type": "object"},
                "review_gate_ref": {"type": "object"},
                "plan_handle": {"type": "string", "description": "Optional explicit draft handle."},
            },
            "additionalProperties": False,
        },
    },
    "op_minion_plan_add_gate_check": {
        "name": "op_minion_plan_add_gate_check",
        "description": (
            "Add one task-level gate checklist item before planning modules. Use this to turn user/work-order "
            "requirements into an indexed checklist target. Pal validates mechanical checks and plan coverage "
            "against these indexes; do not invent explicit check ids. If the workspace already provides source "
            "gate_contract checks, treat those checks as immutable and add only missing planner-local refinements."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "plan_handle": {"type": "string"},
                "claim": {"type": "string", "description": "Requirement/check text derived from the source task."},
                "priority": {"type": "string", "enum": ["hard", "preference", "advisory", "out_of_scope"], "default": "hard"},
                "kind": {"type": "string", "enum": ["semantic", "mechanical", "hybrid"], "default": "semantic"},
                "source_ref": {"type": "string"},
                "rationale": {"type": "string"},
                "mechanical_check": {
                    "type": "object",
                    "description": (
                        "Optional finite predicate Pal can evaluate, such as "
                        "{type: module_count, module_kind: module, op: eq, value: 1}."
                    ),
                },
            },
            "required": ["plan_handle", "claim"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_update_gate_check": {
        "name": "op_minion_plan_update_gate_check",
        "description": (
            "Update one planner-local task-level gate checklist item by zero-based check_index or gate:N check_ref. "
            "Source gate_contract checks provided by the workspace are immutable; link plan nodes to them or add a "
            "new planner-local check instead."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "plan_handle": {"type": "string"},
                "check_index": {"type": "integer"},
                "check_ref": {"type": "string"},
                "claim": {"type": "string"},
                "priority": {"type": "string", "enum": ["hard", "preference", "advisory", "out_of_scope"]},
                "kind": {"type": "string", "enum": ["semantic", "mechanical", "hybrid"]},
                "source_ref": {"type": "string"},
                "rationale": {"type": "string"},
                "mechanical_check": {"type": "object"},
            },
            "required": ["plan_handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_delete_gate_check": {
        "name": "op_minion_plan_delete_gate_check",
        "description": (
            "Soft-delete one task-level gate checklist item by zero-based check_index or gate:N check_ref. "
            "Indexes remain stable for reviewer/planner references. Source gate_contract checks provided by the "
            "workspace are immutable and cannot be deleted."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "plan_handle": {"type": "string"},
                "check_index": {"type": "integer"},
                "check_ref": {"type": "string"},
            },
            "required": ["plan_handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_add_constraints_batch": {
        "name": "op_minion_plan_add_constraints_batch",
        "description": (
            "Add several spec-derived constraints in one call. Pal still assigns stable handles and validates "
            "strength/source linkage; use this instead of many one-at-a-time constraint calls during initial planning. "
            "Only include gate_check_refs when the active draft has real gate_contract refs; otherwise omit them."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "plan_handle": {"type": "string"},
                "constraints": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "statement": {"type": "string"},
                            "kind": {"type": "string"},
                            "strength": {
                                "type": "string",
                                "enum": ["hard_contract", "chosen_contract", "preference", "out_of_scope"],
                                "default": "hard_contract",
                            },
                            "source_ref": {"type": "string"},
                            "rationale": {"type": "string"},
                            "global_only": {"type": "boolean"},
                            "gate_check_refs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Existing gate_contract refs this constraint covers. Omit unless real refs such as gate:0 are present in the active draft.",
                            },
                        },
                        "required": ["statement"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["plan_handle", "constraints"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_add_constraint": {
        "name": "op_minion_plan_add_constraint",
        "description": (
            "Record a spec-derived constraint before planning modules. Mark hard/chosen contracts explicitly so "
            "downstream work and acceptance criteria can trace to them."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "plan_handle": {"type": "string"},
                "statement": {"type": "string"},
                "kind": {"type": "string", "description": "contract, input, output, failure, compatibility, performance, etc."},
                "strength": {
                    "type": "string",
                    "enum": ["hard_contract", "chosen_contract", "preference", "out_of_scope"],
                    "default": "hard_contract",
                },
                "source_ref": {"type": "string"},
                "rationale": {"type": "string"},
                "global_only": {"type": "boolean", "description": "True only when no module or stage-local work item can own the constraint."},
                "gate_check_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Existing gate_contract refs this constraint covers. Omit unless real refs such as gate:0 are present in the active draft.",
                },
            },
            "required": ["plan_handle", "statement"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_update_constraint": {
        "name": "op_minion_plan_update_constraint",
        "description": "Update one constraint by handle. Use this for reviewer-requested changes to stale or conflicting constraints.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "constraint_handle": {"type": "string"},
                "statement": {"type": "string"},
                "kind": {"type": "string"},
                "strength": {
                    "type": "string",
                    "enum": ["hard_contract", "chosen_contract", "preference", "out_of_scope"],
                },
                "source_ref": {"type": "string"},
                "rationale": {"type": "string"},
                "global_only": {"type": "boolean"},
                "gate_check_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["constraint_handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_delete_constraint": {
        "name": "op_minion_plan_delete_constraint",
        "description": "Delete one unreferenced constraint by handle. Deletion is rejected while plan nodes still reference it.",
        "parameters_schema": {
            "type": "object",
            "properties": {"constraint_handle": {"type": "string"}},
            "required": ["constraint_handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_add_design_decision": {
        "name": "op_minion_plan_add_design_decision",
        "description": "Record an architecture/design decision with its contract strength and downstream impact.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "plan_handle": {"type": "string"},
                "question": {"type": "string"},
                "decision": {"type": "string"},
                "strength": {
                    "type": "string",
                    "enum": ["hard_contract", "chosen_contract", "preference", "out_of_scope"],
                    "default": "chosen_contract",
                },
                "rationale": {"type": "string"},
                "alternatives": {"type": "array", "items": {"type": "string"}},
                "downstream_effect": {"type": "string"},
                "linked_constraint_handles": {"type": "array", "items": {"type": "string"}},
                "gate_check_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["plan_handle", "decision"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_update_design_decision": {
        "name": "op_minion_plan_update_design_decision",
        "description": "Update one architecture/design decision by handle. Use this instead of adding a superseding decision for reviewer-requested changes.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "decision_handle": {"type": "string"},
                "question": {"type": "string"},
                "decision": {"type": "string"},
                "strength": {
                    "type": "string",
                    "enum": ["hard_contract", "chosen_contract", "preference", "out_of_scope"],
                },
                "rationale": {"type": "string"},
                "alternatives": {"type": "array", "items": {"type": "string"}},
                "downstream_effect": {"type": "string"},
                "linked_constraint_handles": {"type": "array", "items": {"type": "string"}},
                "gate_check_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["decision_handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_delete_design_decision": {
        "name": "op_minion_plan_delete_design_decision",
        "description": "Delete one unreferenced architecture/design decision by handle. Deletion is rejected while plan nodes still reference it.",
        "parameters_schema": {
            "type": "object",
            "properties": {"decision_handle": {"type": "string"}},
            "required": ["decision_handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_add_module_outline": {
        "name": "op_minion_plan_add_module_outline",
        "description": (
            "Create one closed module from a compact outline: module metadata, optional interfaces, and one or more "
            "closed milestones with acceptance criteria. Pal generates all handles and validates each boundary."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "plan_handle": {"type": "string"},
                "module_name": {
                    "type": "string",
                    "description": "Stable, human-readable module name. Pal stores this as the module_id.",
                },
                "kind": {"type": "string", "enum": ["prelude", "module", "join"], "default": "module"},
                "depends_on_module_handles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Scheduling/start blockers, not general interaction links. Use only for modules whose accepted "
                        "contract, setup, or implementation is required before this module can start. Do not use for "
                        "stubbed interfaces, declaration-only type relationships, runtime callbacks, or final integration-only checks."
                    ),
                },
                "depends_on_module_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Stable module_name values that block this module from starting. Prefer this when possible. "
                        "This is a scheduling/start dependency, not a general consumed-interface or runtime interaction relationship."
                    ),
                },
                "responsibility": {"type": "string"},
                "owned_area": _plan_write_area_schema("owned_area"),
                "ownership": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Required module ownership bullets. State the concrete files/symbols/state/public exits this module "
                        "may write, upstream artifacts it may only read, and shared facades/config/manifests it does not own."
                    ),
                },
                "lifecycle": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Required module lifecycle bullets. State where key data/resources/states are created, mutated, "
                        "validated, persisted, released, and when downstream modules may consume them."
                    ),
                },
                "invariants": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Required module invariant bullets. State conditions that must always hold, illegal states, "
                        "and cross-module assumptions this module must preserve."
                    ),
                },
                "scope_guard": {"type": "string"},
                "constraint_handles": {"type": "array", "items": {"type": "string"}},
                "decision_handles": {"type": "array", "items": {"type": "string"}},
                "gate_check_refs": {"type": "array", "items": {"type": "string"}},
                "languages": {"type": "array", "items": {"type": "string"}},
                "executor_profile": {
                    "type": "string",
                    "description": (
                        "Optional executor profile for this DAG node when it differs from the artifact default "
                        "workflow_next profile. Bare names such as review_worker resolve inside the default "
                        "executor profile group; canonical ids such as software_engineering.review_worker are also accepted."
                    ),
                },
                "module_quality_criteria": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Required for implementation modules. Semantic module-boundary checks for module_quality review, "
                        "not per-checkpoint admission. Include contract fit, public/cross-module interface closure, "
                        "type/schema boundary, corner cases, lifecycle/ownership, delivery-surface dogfood, and downstream readiness."
                    ),
                },
                "risk_surfaces": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "General risk surfaces such as public_api, cross_module_contract, delivery_surface, persistence_format, concurrency_lifecycle, security, or performance.",
                },
                "delivery_surfaces": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Declared user/downstream-facing delivery surfaces this module exposes or changes, such as public imports, "
                        "CLI commands, generated files, wrappers, APIs, service routes, package entrypoints, or integration hooks."
                    ),
                },
                "interfaces": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "direction": {"type": "string", "enum": ["provided", "consumed"]},
                            "name": {"type": "string"},
                            "shape": {"type": "string"},
                            "lifecycle": {"type": "string"},
                            "ownership": {"type": "string"},
                            "error_behavior": {"type": "string"},
                            "compatibility": {"type": "string"},
                            "producer": {"type": "string"},
                            "consumer": {"type": "string"},
                            "import_path": {"type": "string"},
                            "source_path": {"type": "string"},
                            "public_entrypoint": {"type": "string"},
                            "copy_policy": {"type": "string", "enum": ["import_only", "copy_allowed"]},
                        },
                        "required": ["direction", "name", "shape", "lifecycle", "ownership", "error_behavior", "compatibility"],
                        "additionalProperties": False,
                    },
                },
                "provided_interfaces": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "shape": {"type": "string"},
                            "lifecycle": {"type": "string"},
                            "ownership": {"type": "string"},
                            "error_behavior": {"type": "string"},
                            "compatibility": {"type": "string"},
                            "producer": {"type": "string"},
                            "consumer": {"type": "string"},
                            "import_path": {"type": "string"},
                            "source_path": {"type": "string"},
                            "public_entrypoint": {"type": "string"},
                            "copy_policy": {"type": "string", "enum": ["import_only", "copy_allowed"]},
                        },
                        "required": ["name", "shape", "lifecycle", "ownership", "error_behavior", "compatibility"],
                        "additionalProperties": False,
                    },
                    "description": (
                        "Provided module interfaces. Prefer this over adding interfaces after the outline is closed. "
                        "For shared contracts/stubs/facades consumed by other modules, include import_path, source_path, "
                        "and copy_policy=import_only so downstream coders import the shared contract instead of copying it."
                    ),
                },
                "consumed_interfaces": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "shape": {"type": "string"},
                            "lifecycle": {"type": "string"},
                            "ownership": {"type": "string"},
                            "error_behavior": {"type": "string"},
                            "compatibility": {"type": "string"},
                            "producer": {"type": "string"},
                            "consumer": {"type": "string"},
                            "import_path": {"type": "string"},
                            "source_path": {"type": "string"},
                            "public_entrypoint": {"type": "string"},
                            "copy_policy": {"type": "string", "enum": ["import_only", "copy_allowed"]},
                        },
                        "required": ["name", "shape", "lifecycle", "ownership", "error_behavior", "compatibility"],
                        "additionalProperties": False,
                    },
                    "description": (
                        "Consumed module interfaces. Prefer this over adding interfaces after the outline is closed. "
                        "When consuming a shared contract/stub/facade, cite the producer's import_path/source_path and "
                        "copy_policy=import_only."
                    ),
                },
                "milestones": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Closed milestone outlines. Each item should include title, task, and acceptance_criteria. "
                        "Acceptance criteria objects are preferred; string shorthand is accepted and Pal fills "
                        "conservative evidence/negative-case defaults."
                    ),
                },
            },
            "required": [
                "plan_handle",
                "module_name",
                "responsibility",
                "owned_area",
                "ownership",
                "lifecycle",
                "invariants",
                "milestones",
            ],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_add_module_outlines_batch": {
        "name": "op_minion_plan_add_module_outlines_batch",
        "description": (
            "Add and close multiple module outlines in one transaction. Each module uses the same shape as "
            "plan_add_module_outline except plan_handle is supplied once at the top level. Order modules by "
            "dependency so later modules can reference earlier modules through depends_on_module_names. If any "
            "module fails validation, Pal rolls the whole batch back."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "plan_handle": {"type": "string"},
                "modules": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "module_name": {
                                "type": "string",
                                "description": "Stable, human-readable module name. Prefer snake_case names.",
                            },
                            "kind": {"type": "string", "enum": ["prelude", "module", "join"], "default": "module"},
                            "depends_on_module_handles": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Scheduling/start blockers; do not use for stubbed, declaration-only, or integration-only relationships.",
                            },
                            "depends_on_module_names": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Stable module_name values that block this module from starting. Prefer this when possible.",
                            },
                            "responsibility": {"type": "string"},
                            "owned_area": _plan_write_area_schema("owned_area"),
                            "ownership": {"type": "array", "items": {"type": "string"}},
                            "lifecycle": {"type": "array", "items": {"type": "string"}},
                            "invariants": {"type": "array", "items": {"type": "string"}},
                            "scope_guard": {"type": "string"},
                            "constraint_handles": {"type": "array", "items": {"type": "string"}},
                            "decision_handles": {"type": "array", "items": {"type": "string"}},
                            "gate_check_refs": {"type": "array", "items": {"type": "string"}},
                            "languages": {"type": "array", "items": {"type": "string"}},
                            "executor_profile": {"type": "string"},
                            "module_quality_criteria": {"type": "array", "items": {"type": "string"}},
                            "risk_surfaces": {"type": "array", "items": {"type": "string"}},
                            "delivery_surfaces": {"type": "array", "items": {"type": "string"}},
                            "interfaces": {"type": "array", "items": {"type": "object"}},
                            "provided_interfaces": {"type": "array", "items": {"type": "object"}},
                            "consumed_interfaces": {"type": "array", "items": {"type": "object"}},
                            "milestones": {
                                "type": "array",
                                "items": {"type": "object"},
                                "description": "Closed milestone outlines. Each item should include title, task, and acceptance_criteria.",
                            },
                        },
                        "required": [
                            "module_name",
                            "responsibility",
                            "owned_area",
                            "ownership",
                            "lifecycle",
                            "invariants",
                            "milestones",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["plan_handle", "modules"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_begin_module": {
        "name": "op_minion_plan_begin_module",
        "description": (
            "Open one module boundary. Modules are the topology/concurrency units; milestones inside a module are "
            "linear. Use kind=prelude for setup/contracts, kind=join for final integration/verification, and kind=module "
            "for implementation work."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "plan_handle": {"type": "string"},
                "module_name": {
                    "type": "string",
                    "description": (
                        "Stable, human-readable module name. Use snake_case names such as "
                        "setup_contracts, slug_tools, implementation, or final_verification."
                    ),
                },
                "kind": {"type": "string", "enum": ["prelude", "module", "join"], "default": "module"},
                "depends_on_module_handles": {"type": "array", "items": {"type": "string"}},
                "depends_on_module_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Stable module_name values this module depends on. Prefer this when possible.",
                },
                "responsibility": {"type": "string"},
                "owned_area": _plan_write_area_schema("owned_area"),
                "ownership": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Required module ownership bullets: write ownership, read-only dependencies, and non-owned shared surfaces.",
                },
                "lifecycle": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Required module lifecycle bullets: creation, mutation, validation, persistence/release, and downstream consumption timing.",
                },
                "invariants": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Required module invariant bullets: always-true conditions, illegal states, and cross-module assumptions.",
                },
                "scope_guard": {"type": "string"},
                "constraint_handles": {"type": "array", "items": {"type": "string"}},
                "decision_handles": {"type": "array", "items": {"type": "string"}},
                "gate_check_refs": {"type": "array", "items": {"type": "string"}},
                "languages": {"type": "array", "items": {"type": "string"}},
                "executor_profile": {
                    "type": "string",
                    "description": (
                        "Optional executor profile for this DAG node when it differs from the artifact default workflow_next profile. "
                        "Bare names resolve inside the default executor profile group; canonical ids are also accepted."
                    ),
                },
                "module_quality_criteria": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Semantic module-boundary checks used by module_quality review.",
                },
                "risk_surfaces": {"type": "array", "items": {"type": "string"}},
                "delivery_surfaces": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["plan_handle", "module_name", "responsibility", "owned_area", "ownership", "lifecycle", "invariants"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_add_module_interface": {
        "name": "op_minion_plan_add_module_interface",
        "description": (
            "Attach a provided or consumed module interface with data shape, lifecycle, ownership, error behavior, "
            "and compatibility. Prefer module_name+plan_handle over copying generated module_handle strings."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "module_handle": {"type": "string"},
                "module_name": {"type": "string", "description": "Stable module_name for the target module."},
                "plan_handle": {"type": "string", "description": "Required when module_name is used and more than one draft may exist."},
                "direction": {"type": "string", "enum": ["provided", "consumed"]},
                "name": {"type": "string"},
                "shape": {"type": "string"},
                "lifecycle": {"type": "string"},
                "ownership": {"type": "string"},
                "error_behavior": {"type": "string"},
                "compatibility": {"type": "string"},
                "producer": {"type": "string"},
                "consumer": {"type": "string"},
                "import_path": {"type": "string"},
                "source_path": {"type": "string"},
                "public_entrypoint": {"type": "string"},
                "copy_policy": {"type": "string", "enum": ["import_only", "copy_allowed"]},
            },
            "required": ["direction", "name", "shape", "lifecycle", "ownership", "error_behavior", "compatibility"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_add_module_acceptance_criteria_batch": {
        "name": "op_minion_plan_add_module_acceptance_criteria_batch",
        "description": (
            "Append several module-level acceptance criteria to a sketch module. Use this for architecture-stage "
            "checks about module responsibility, public/consumed interfaces, topology handoff, ownership, lifecycle, "
            "state rules, invariants, source requirement coverage, and end-to-end readiness. These criteria are "
            "module-boundary obligations, not an internal implementation breakdown."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "module_handle": {"type": "string"},
                "module_name": {"type": "string", "description": "Stable module_name for the target module."},
                "plan_handle": {"type": "string", "description": "Required when module_name is used and more than one draft may exist."},
                "criteria": {
                    "type": "array",
                    "description": (
                        "Module-level acceptance criteria. Objects are preferred; string shorthand is accepted. "
                        "Use evidence_expectation for the proof shape and negative_cases for boundary/error examples when relevant."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "criterion": {"type": "string"},
                            "evidence_expectation": {"type": "string"},
                            "linked_constraint_handles": {"type": "array", "items": {"type": "string"}},
                            "gate_check_refs": {"type": "array", "items": {"type": "string"}},
                            "source_ref": {"type": "string"},
                            "source_refs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Reference/source locations that justify this criterion, preferably with line numbers from op_file_read.",
                            },
                            "reference_refs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Alias for source_refs when citing declared read-only reference roots.",
                            },
                            "negative_cases": {"type": "array", "items": {"type": "string"}},
                            "quantifier": {"type": "string"},
                        },
                        "required": ["criterion"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["criteria"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_add_module_acceptance_criterion": {
        "name": "op_minion_plan_add_module_acceptance_criterion",
        "description": (
            "Append one module-level acceptance criterion to a sketch module. Use this for a module-boundary "
            "contract or first-layer review check; use the batch form when adding more than one."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "module_handle": {"type": "string"},
                "module_name": {"type": "string", "description": "Stable module_name for the target module."},
                "plan_handle": {"type": "string", "description": "Required when module_name is used and more than one draft may exist."},
                "criterion": {"type": "string"},
                "evidence_expectation": {"type": "string"},
                "linked_constraint_handles": {"type": "array", "items": {"type": "string"}},
                "gate_check_refs": {"type": "array", "items": {"type": "string"}},
                "source_ref": {"type": "string"},
                "source_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Reference/source locations that justify this criterion, preferably with line numbers from op_file_read.",
                },
                "reference_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Alias for source_refs when citing declared read-only reference roots.",
                },
                "negative_cases": {"type": "array", "items": {"type": "string"}},
                "quantifier": {"type": "string"},
            },
            "required": ["criterion"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_add_milestone_outline": {
        "name": "op_minion_plan_add_milestone_outline",
        "description": (
            "Create and close one module-scoped milestone from a compact outline, including its acceptance criteria. "
            "Use this during initial planning to reduce tool turns."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "module_handle": {"type": "string"},
                "module_name": {"type": "string", "description": "Stable module_name for the target module."},
                "plan_handle": {"type": "string", "description": "Required when module_name is used and more than one draft may exist."},
                "title": {"type": "string"},
                "task": {"type": "string"},
                "scope_guard": {"type": "string"},
                "changed_area": _plan_write_area_schema("changed_area"),
                "constraint_handles": {"type": "array", "items": {"type": "string"}},
                "decision_handles": {"type": "array", "items": {"type": "string"}},
                "gate_check_refs": {"type": "array", "items": {"type": "string"}},
                "languages": {"type": "array", "items": {"type": "string"}},
                "tests_required": {"type": "array", "items": {"type": "string"}},
                "public_api_added": {"type": "string"},
                "checkpoint_admission_evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Required for every milestone. Checkpoint-admission evidence expected for this milestone: "
                        "focused tests/commands, LSP/type/build diagnostics or not-applicable reason, and changed-area evidence."
                    ),
                },
                "acceptance_criteria": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Acceptance criterion objects are preferred; string shorthand is accepted and Pal fills "
                        "conservative evidence/negative-case defaults."
                    ),
                },
            },
            "required": ["title", "task", "acceptance_criteria"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_begin_milestone": {
        "name": "op_minion_plan_begin_milestone",
        "description": "Open a module-scoped coding milestone. Keep it small enough for one coder checkpoint.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "module_handle": {"type": "string"},
                "module_name": {"type": "string", "description": "Stable module_name for the target module."},
                "plan_handle": {"type": "string", "description": "Required when module_name is used and more than one draft may exist."},
                "title": {"type": "string"},
                "task": {"type": "string"},
                "scope_guard": {"type": "string"},
                "changed_area": _plan_write_area_schema("changed_area"),
                "constraint_handles": {"type": "array", "items": {"type": "string"}},
                "decision_handles": {"type": "array", "items": {"type": "string"}},
                "gate_check_refs": {"type": "array", "items": {"type": "string"}},
                "languages": {"type": "array", "items": {"type": "string"}},
                "tests_required": {"type": "array", "items": {"type": "string"}},
                "public_api_added": {"type": "string"},
                "checkpoint_admission_evidence": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "task"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_add_acceptance_criteria_batch": {
        "name": "op_minion_plan_add_acceptance_criteria_batch",
        "description": (
            "Attach several acceptance criteria to an open milestone in one call. Each item still needs evidence "
            "expectation and negative_cases."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "milestone_handle": {"type": "string"},
                "criteria": {
                    "type": "array",
                    "description": (
                        "Acceptance criterion objects are preferred; string shorthand is accepted and Pal fills "
                        "conservative evidence/negative-case defaults."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "criterion": {"type": "string"},
                            "evidence_expectation": {"type": "string"},
                            "linked_constraint_handles": {"type": "array", "items": {"type": "string"}},
                            "gate_check_refs": {"type": "array", "items": {"type": "string"}},
                            "negative_cases": {"type": "array", "items": {"type": "string"}},
                            "quantifier": {"type": "string"},
                        },
                        "required": ["criterion", "evidence_expectation", "negative_cases"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["milestone_handle", "criteria"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_add_acceptance_criterion": {
        "name": "op_minion_plan_add_acceptance_criterion",
        "description": (
            "Attach one concrete, evidence-backed acceptance criterion to the open milestone. Acceptance criteria in "
            "one milestone must be mutually satisfiable; do not mix input-order preservation with byte-identical "
            "output regardless of input order unless the ordering contract is made consistent."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "milestone_handle": {"type": "string"},
                "criterion": {"type": "string"},
                "evidence_expectation": {"type": "string"},
                "linked_constraint_handles": {"type": "array", "items": {"type": "string"}},
                "gate_check_refs": {"type": "array", "items": {"type": "string"}},
                "negative_cases": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Explicit negative/boundary examples. Use [] only when this criterion has no meaningful "
                        "reject/error/empty/default/fallback case."
                    ),
                },
                "quantifier": {"type": "string", "description": "Concrete bound/count/range when applicable."},
            },
            "required": ["milestone_handle", "criterion", "evidence_expectation", "negative_cases"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_end_milestone": {
        "name": "op_minion_plan_end_milestone",
        "description": "Close the current milestone after adding mutually satisfiable acceptance criteria. Returns the parent module handle.",
        "parameters_schema": {
            "type": "object",
            "properties": {"milestone_handle": {"type": "string"}},
            "required": ["milestone_handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_end_module": {
        "name": "op_minion_plan_end_module",
        "description": "Close a module after all interfaces and milestones have been added. Returns the parent plan handle.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "module_handle": {"type": "string"},
                "module_name": {"type": "string"},
                "plan_handle": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    "op_minion_plan_update_plan": {
        "name": "op_minion_plan_update_plan",
        "description": (
            "Update top-level draft fields only. In revision mode, use this tool for every intentional change to "
            "summary, system tests, risks, or metadata; submit tools only freeze the edited draft. Use node-specific "
            "tools for modules, milestones, and acceptance criteria."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "plan_handle": {"type": "string"},
                "summary": {"type": "string"},
                "system_test_plan": {"type": "array", "items": {"type": "object"}},
                "risks": {"type": "array", "items": {"type": "object"}},
                "languages": {"type": "array", "items": {"type": "string"}},
                "source_refs": {"type": "array", "items": {"type": "string"}},
                "workflow_next": {
                    "type": "object",
                    "description": "Optional replacement for metadata.workflow_next; set profile/next_profile to control the next workflow step.",
                },
            },
            "required": ["plan_handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_update_module": {
        "name": "op_minion_plan_update_module",
        "description": "Update only a module's own metadata, dependency fields, and module-level quality criteria. Use stage-specific tools for child nodes.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "module_handle": {"type": "string"},
                "module_name": {"type": "string"},
                "plan_handle": {"type": "string"},
                "kind": {"type": "string", "enum": ["prelude", "module", "join"]},
                "depends_on_module_handles": {"type": "array", "items": {"type": "string"}},
                "depends_on_module_names": {"type": "array", "items": {"type": "string"}},
                "responsibility": {"type": "string"},
                "owned_area": _plan_write_area_schema("owned_area"),
                "ownership": {"type": "array", "items": {"type": "string"}},
                "lifecycle": {"type": "array", "items": {"type": "string"}},
                "invariants": {"type": "array", "items": {"type": "string"}},
                "scope_guard": {"type": "string"},
                "constraint_handles": {"type": "array", "items": {"type": "string"}},
                "decision_handles": {"type": "array", "items": {"type": "string"}},
                "gate_check_refs": {"type": "array", "items": {"type": "string"}},
                "languages": {"type": "array", "items": {"type": "string"}},
                "executor_profile": {"type": "string"},
                "module_quality_criteria": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Replace semantic module-quality criteria for this module.",
                },
                "risk_surfaces": {"type": "array", "items": {"type": "string"}},
                "delivery_surfaces": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
    },
    "op_minion_plan_delete_module": {
        "name": "op_minion_plan_delete_module",
        "description": "Delete one module. Deletion is rejected when other modules still depend on it.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "module_handle": {"type": "string"},
                "module_name": {"type": "string"},
                "plan_handle": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    "op_minion_plan_merge_modules": {
        "name": "op_minion_plan_merge_modules",
        "description": (
            "Merge one source module into a target module in the editable plan draft. Use this for plan-review fixes "
            "that say two modules should become one. Pal moves child work items, merges owned_area/interfaces/refs/languages, "
            "rewrites dependencies from source to target, deletes the source module, and keeps the draft editable."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "target_module_handle": {"type": "string"},
                "source_module_handle": {"type": "string"},
                "insert_milestones_at": {"type": "integer", "description": "Optional zero-based insertion index for source child work items in target."},
            },
            "required": ["target_module_handle", "source_module_handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_update_milestone": {
        "name": "op_minion_plan_update_milestone",
        "description": "Update only a milestone's own metadata. Use AC tools for acceptance criteria.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "milestone_handle": {"type": "string"},
                "title": {"type": "string"},
                "task": {"type": "string"},
                "scope_guard": {"type": "string"},
                "changed_area": _plan_write_area_schema("changed_area"),
                "constraint_handles": {"type": "array", "items": {"type": "string"}},
                "decision_handles": {"type": "array", "items": {"type": "string"}},
                "gate_check_refs": {"type": "array", "items": {"type": "string"}},
                "languages": {"type": "array", "items": {"type": "string"}},
                "tests_required": {"type": "array", "items": {"type": "string"}},
                "public_api_added": {"type": "string"},
                "checkpoint_admission_evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Replace checkpoint-admission evidence expected for this milestone.",
                },
            },
            "required": ["milestone_handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_delete_milestone": {
        "name": "op_minion_plan_delete_milestone",
        "description": "Delete one milestone from its parent module.",
        "parameters_schema": {
            "type": "object",
            "properties": {"milestone_handle": {"type": "string"}},
            "required": ["milestone_handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_move_milestone": {
        "name": "op_minion_plan_move_milestone",
        "description": "Move one milestone to another module. This is explicit structure editing, not module metadata update.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "milestone_handle": {"type": "string"},
                "target_module_handle": {"type": "string"},
                "index": {"type": "integer", "description": "Optional zero-based insertion index."},
            },
            "required": ["milestone_handle", "target_module_handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_update_acceptance_criterion": {
        "name": "op_minion_plan_update_acceptance_criterion",
        "description": "Update one acceptance criterion by handle.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "acceptance_handle": {"type": "string"},
                "criterion": {"type": "string"},
                "evidence_expectation": {"type": "string"},
                "linked_constraint_handles": {"type": "array", "items": {"type": "string"}},
                "gate_check_refs": {"type": "array", "items": {"type": "string"}},
                "negative_cases": {"type": "array", "items": {"type": "string"}},
                "quantifier": {"type": "string"},
            },
            "required": ["acceptance_handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_delete_acceptance_criterion": {
        "name": "op_minion_plan_delete_acceptance_criterion",
        "description": "Delete one acceptance criterion by handle.",
        "parameters_schema": {
            "type": "object",
            "properties": {"acceptance_handle": {"type": "string"}},
            "required": ["acceptance_handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_replace_milestone_acceptance_criteria": {
        "name": "op_minion_plan_replace_milestone_acceptance_criteria",
        "description": "Replace the full acceptance-criteria list for one milestone. Use only for local AC list repair.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "milestone_handle": {"type": "string"},
                "criteria": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["milestone_handle", "criteria"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_apply_revision_item": {
        "name": "op_minion_plan_apply_revision_item",
        "description": (
            "Apply or record one active plan revision checklist item by item_id. Prefer this in plan revision mode: "
            "Pal uses the checklist item's suggested_tool and target handle, so the model only supplies replacement "
            "fields and evidence. For complex repairs already completed with lower-level tools, use resolution=manual."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "plan_handle": {"type": "string"},
                "item_id": {"type": "string"},
                "resolution": {"type": "string", "enum": ["apply", "manual", "keep"], "default": "apply"},
                "replacement": {
                    "type": "object",
                    "description": "Fields to merge into suggested_args, such as decision, task, changed_area, criteria, or acceptance fields.",
                },
                "evidence": {
                    "type": "string",
                    "description": "Concrete evidence that the revision item is resolved or intentionally kept.",
                },
            },
            "required": ["plan_handle", "item_id", "evidence"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_submit_for_review": {
        "name": "op_minion_plan_submit_for_review",
        "description": (
            "Freeze the current draft snapshot for the plan_acceptance gate. This writes a reviewed draft artifact, "
            "not the final accepted plan entity. For a checked-out revision, pass only plan_handle; submit arguments "
            "must not silently replace top-level plan content."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "plan_handle": {"type": "string"},
                "summary": {"type": "string"},
                "system_test_plan": {"type": "array", "items": {"type": "object"}},
                "risks": {"type": "array", "items": {"type": "object"}},
                "assumptions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["plan_handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_finalize": {
        "name": "op_minion_plan_finalize",
        "description": (
            "Compile the structured draft into the normal primary plan.json FinalPlanArtifact and register it for the "
            "existing plan_acceptance gate. Call this only after every module and milestone is closed. For a checked-out "
            "revision, pass only plan_handle."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "plan_handle": {"type": "string"},
                "summary": {"type": "string"},
                "system_test_plan": {"type": "array", "items": {"type": "object"}},
                "risks": {"type": "array", "items": {"type": "object"}},
                "assumptions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["plan_handle"],
            "additionalProperties": False,
        },
    },
}


def _apply_sketch_start_blocker_schema(properties: dict[str, Any]) -> dict[str, Any]:
    updated = dict(properties)
    updated.pop("depends_on_module_handles", None)
    updated.pop("depends_on_module_names", None)
    updated["work_start_blocked_by_module_handles"] = {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Module handles that block this module from starting because their accepted contract, setup, or implementation "
            "must exist first. Do not use for stubbed interfaces, opaque/declaration-only type relationships, runtime callbacks, "
            "or final integration/test-only relationships."
        ),
    }
    updated["work_start_blocked_by_module_names"] = {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Stable module_name values that block this module from starting. Prefer this over handles when adding modules in "
            "dependency order. This is a work-start scheduling blocker, not a general interface, data-shape, or runtime interaction link."
        ),
    }
    return updated


def _map_sketch_module_outline_args(args: dict[str, Any]) -> dict[str, Any]:
    raw = dict(args or {})
    mapped = {
        key: value
        for key, value in raw.items()
        if key
        not in {
            "milestones",
            "work_start_blocked_by_module_handles",
            "work_start_blocked_by_module_names",
        }
    }
    blocker_handles = _string_list(raw.get("work_start_blocked_by_module_handles"))
    blocker_names = _string_list(raw.get("work_start_blocked_by_module_names"))
    if blocker_handles:
        mapped["depends_on_module_handles"] = blocker_handles
    if blocker_names:
        mapped["depends_on_module_names"] = blocker_names
    return mapped


def _map_sketch_module_outlines_batch_args(args: dict[str, Any]) -> dict[str, Any]:
    raw = dict(args or {})
    return {
        **{key: value for key, value in raw.items() if key != "modules"},
        "modules": [
            _map_sketch_module_outline_args(dict(module or {}))
            for module in list(raw.get("modules") or [])
            if isinstance(module, dict)
        ],
    }


def _sketch_module_outline_schema() -> dict[str, Any]:
    schema = deepcopy(PLAN_BUILDER_TOOL_SPECS["op_minion_plan_add_module_outline"]["parameters_schema"])
    properties = dict(schema.get("properties") or {})
    properties.pop("milestones", None)
    properties = _apply_sketch_start_blocker_schema(properties)
    schema["properties"] = properties
    schema["required"] = [item for item in list(schema.get("required") or []) if item != "milestones"]
    return schema


def _sketch_module_outlines_batch_schema() -> dict[str, Any]:
    schema = deepcopy(PLAN_BUILDER_TOOL_SPECS["op_minion_plan_add_module_outlines_batch"]["parameters_schema"])
    modules = dict(dict(schema.get("properties") or {}).get("modules") or {})
    module_items = dict(modules.get("items") or {})
    module_properties = dict(module_items.get("properties") or {})
    module_properties.pop("milestones", None)
    module_properties = _apply_sketch_start_blocker_schema(module_properties)
    module_items["properties"] = module_properties
    module_items["required"] = [item for item in list(module_items.get("required") or []) if item != "milestones"]
    modules["items"] = module_items
    properties = dict(schema.get("properties") or {})
    properties["modules"] = modules
    schema["properties"] = properties
    return schema


def _merge_sketch_modules_schema() -> dict[str, Any]:
    schema = deepcopy(PLAN_BUILDER_TOOL_SPECS["op_minion_plan_merge_modules"]["parameters_schema"])
    properties = dict(schema.get("properties") or {})
    properties.pop("insert_milestones_at", None)
    schema["properties"] = properties
    return schema


register_plan_builder_alias(
    PlanBuilderAliasSpec(
        name="op_minion_plan_add_sketch_module_outline",
        core_tool="op_minion_plan_add_module_outline",
        domain="architecture_sketch",
        description=(
            "Create one closed sketch module from a compact outline: module metadata, topology dependencies, "
            "interfaces, ownership, lifecycle, invariants, risk/delivery surfaces, and module-level quality criteria. "
            "Topology inputs are work-start blockers only; express stubbed interfaces, declaration-only data contracts, "
            "runtime callbacks, and final integration relationships through interfaces and AC instead. Use this only for "
            "first-stage architecture structure; module internals are handled by the later detail stage."
        ),
        parameters_schema=_sketch_module_outline_schema(),
        map_args=_map_sketch_module_outline_args,
    )
)


register_plan_builder_alias(
    PlanBuilderAliasSpec(
        name="op_minion_plan_add_sketch_module_outlines_batch",
        core_tool="op_minion_plan_add_module_outlines_batch",
        domain="architecture_sketch",
        description=(
            "Add and close multiple sketch module outlines in one transaction. Each module carries only architecture-level "
            "shape: responsibility, owned area, work-start blockers, interfaces, ownership, lifecycle, invariants, and "
            "module-level quality criteria. Order modules so true start blockers appear before blocked modules. Do not encode "
            "stubbed interfaces, declaration-only data contracts, runtime callbacks, or final integration relationships as start blockers."
        ),
        parameters_schema=_sketch_module_outlines_batch_schema(),
        map_args=_map_sketch_module_outlines_batch_args,
    )
)


register_plan_builder_alias(
    PlanBuilderAliasSpec(
        name="op_minion_plan_merge_sketch_modules",
        core_tool="op_minion_plan_merge_modules",
        domain="architecture_sketch",
        description=(
            "Merge one source sketch module into a target sketch module during review repair. Use this when the "
            "architecture has two module boundaries that should be one boundary."
        ),
        parameters_schema=_merge_sketch_modules_schema(),
        map_args=lambda args: dict(args or {}),
    )
)


register_plan_builder_alias(
    PlanBuilderAliasSpec(
        name="op_minion_plan_submit_sketch",
        core_tool="op_minion_plan_validate_and_submit_for_review",
        domain="architecture_sketch",
        description=(
            "Validate the architecture sketch and freeze the PlanSketchArtifact for review. Call this after modules, "
            "interfaces, work-start blocker topology, module-level acceptance criteria, and end-to-end gates are complete."
        ),
        parameters_schema=deepcopy(PLAN_BUILDER_TOOL_SPECS["op_minion_plan_validate_and_submit_for_review"]["parameters_schema"]),
        map_args=lambda args: dict(args or {}),
    )
)


def _plan_validate_result(payload: dict[str, Any], *, plan_handle: str, valid_text: str, invalid_prefix: str) -> dict[str, Any]:
    try:
        validation = dispatchable_plan_validation(payload)
    except ValueError as exc:
        message = str(exc) or exc.__class__.__name__
        return {
            "text": f"{invalid_prefix}: {message}",
            "structured": {"plan_handle": plan_handle, "plan_validation": {"status": "invalid", "errors": [message]}},
        }
    return {
        "text": valid_text,
        "structured": {"plan_handle": plan_handle, "plan_validation": validation},
    }


PLAN_BUILDER_TOOL_HANDLERS: dict[str, str] = {
    "op_minion_plan_read": "_plan_read",
    "op_minion_plan_find": "_plan_find",
    "op_minion_plan_get": "_plan_get",
    "op_minion_plan_validate": "_validate",
    "op_minion_plan_validate_and_submit_for_review": "_submit_for_review",
    "op_minion_plan_begin": "_plan_begin",
    "op_minion_plan_checkout": "_plan_checkout",
    "op_minion_plan_add_gate_check": "_add_gate_check",
    "op_minion_plan_update_gate_check": "_update_gate_check",
    "op_minion_plan_delete_gate_check": "_delete_gate_check",
    "op_minion_plan_add_constraints_batch": "_add_constraints_batch",
    "op_minion_plan_add_constraint": "_add_constraint",
    "op_minion_plan_update_constraint": "_update_constraint",
    "op_minion_plan_delete_constraint": "_delete_constraint",
    "op_minion_plan_add_design_decision": "_add_design_decision",
    "op_minion_plan_update_design_decision": "_update_design_decision",
    "op_minion_plan_delete_design_decision": "_delete_design_decision",
    "op_minion_plan_add_module_outline": "_add_module_outline",
    "op_minion_plan_add_module_outlines_batch": "_add_module_outlines_batch",
    "op_minion_plan_begin_module": "_begin_module",
    "op_minion_plan_add_module_interface": "_add_module_interface",
    "op_minion_plan_add_module_acceptance_criteria_batch": "_add_module_acceptance_criteria_batch",
    "op_minion_plan_add_module_acceptance_criterion": "_add_module_acceptance_criterion",
    "op_minion_plan_add_milestone_outline": "_add_milestone_outline",
    "op_minion_plan_begin_milestone": "_begin_milestone",
    "op_minion_plan_add_acceptance_criteria_batch": "_add_acceptance_criteria_batch",
    "op_minion_plan_add_acceptance_criterion": "_add_acceptance_criterion",
    "op_minion_plan_end_milestone": "_end_milestone",
    "op_minion_plan_end_module": "_end_module",
    "op_minion_plan_update_plan": "_update_plan",
    "op_minion_plan_update_module": "_update_module",
    "op_minion_plan_delete_module": "_delete_module",
    "op_minion_plan_merge_modules": "_merge_modules",
    "op_minion_plan_update_milestone": "_update_milestone",
    "op_minion_plan_delete_milestone": "_delete_milestone",
    "op_minion_plan_move_milestone": "_move_milestone",
    "op_minion_plan_update_acceptance_criterion": "_update_acceptance_criterion",
    "op_minion_plan_delete_acceptance_criterion": "_delete_acceptance_criterion",
    "op_minion_plan_replace_milestone_acceptance_criteria": "_replace_milestone_acceptance_criteria",
    "op_minion_plan_apply_revision_item": "_apply_revision_item",
    "op_minion_plan_submit_for_review": "_submit_for_review",
    "op_minion_plan_finalize": "_finalize",
}


@dataclass
class PlanBuilderRuntime:
    workspace: dict[str, Any]
    produced_artifacts: list[dict[str, Any]]

    def execute(self, call: CanonicalToolCall) -> CanonicalToolResult:
        try:
            result = self._execute(call.name, dict(call.args or {}))
            text = str(result.get("text") or "plan builder updated")
            structured = dict(result.get("structured") or {})
            return CanonicalToolResult(
                name=call.name,
                ok=True,
                text=text,
                structured=structured,
                call_id=call.call_id,
                llm_text=text,
                status=RuntimeStatus.OK,
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

    def _execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        name = str(name or "").strip()
        args = self._apply_stage_binders(name, dict(args or {}))
        alias = PLAN_BUILDER_ALIASES.get(name)
        if alias is not None:
            mapped_args = alias.map_args(dict(args or {}))
            if not isinstance(mapped_args, dict):
                raise ValueError(f"plan builder alias {alias.name} must return an argument object")
            result = self._execute(alias.core_tool, mapped_args)
            structured = dict(result.get("structured") or {})
            structured["plan_builder_alias"] = {
                "alias": alias.name,
                "domain": alias.domain,
                "core_tool": alias.core_tool,
            }
            result["structured"] = structured
            return result
        handler_name = PLAN_BUILDER_TOOL_HANDLERS.get(name)
        if handler_name:
            handler = getattr(self, handler_name)
            return handler(args)
        raise ValueError(f"unknown plan builder tool: {name}")

    def _apply_stage_binders(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        stage = _plan_builder_stage_from_workspace(self.workspace)
        if stage not in {"architecture_sketch", "module_detail"}:
            return args
        plan_handle = _text(self.workspace.get("plan_builder_plan_handle"))
        if plan_handle and _plan_tool_accepts_arg(name, "plan_handle") and not _text(args.get("plan_handle")):
            args["plan_handle"] = plan_handle
        if stage == "module_detail":
            module_id = _text(self.workspace.get("plan_builder_module_id") or self.workspace.get("bound_module_id"))
            if module_id and _plan_tool_accepts_arg(name, "module_name") and not _text(args.get("module_name")) and not _text(args.get("module_handle")):
                args["module_name"] = module_id
        return args

    def _plan_read(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"plan_handle", "plan_ref", "detail"})
        state = self._state_from_ref_or_handle(args)
        detail = _text(args.get("detail") or "summary").lower()
        if detail not in {"summary", "full"}:
            raise ValueError("detail must be summary or full")
        snapshot = _plan_snapshot(state, full=detail == "full")
        text = f"Plan draft read: {state['plan_handle']} ({len(state.get('modules') or [])} modules)"
        checklist = [dict(item) for item in list(state.get("plan_revision_checklist") or []) if isinstance(item, dict)]
        if checklist:
            text += "\nActive revision checklist:\n" + _revision_checklist_llm_text(checklist)
        return {
            "text": text,
            "structured": snapshot,
        }

    def _plan_find(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"plan_handle", "plan_ref", "type", "query", "limit"})
        state = self._state_from_ref_or_handle(args)
        node_type = _text(args.get("type") or "any").lower()
        if node_type not in {"plan", "gate_check", "constraint", "decision", "module", "interface", "milestone", "acceptance_criterion", "any"}:
            raise ValueError("type must be plan, gate_check, constraint, decision, module, interface, milestone, acceptance_criterion, or any")
        query = _text(args.get("query"))
        limit = max(1, min(50, _coerce_int(args.get("limit"), default=10)))
        matches: list[dict[str, Any]] = []
        for node in _iter_plan_nodes(state):
            if node_type != "any" and node.get("node_kind") != node_type:
                continue
            if query and not _query_matches_node(query, node):
                continue
            matches.append(_compact_node(node))
            if len(matches) >= limit:
                break
        return {
            "text": "\n".join(f"{item['handle']} {item['node_kind']} {item.get('path') or ''}: {item.get('summary') or ''}" for item in matches)
            or "No plan nodes matched.",
            "structured": {"plan_handle": state["plan_handle"], "matches": matches, "count": len(matches)},
        }

    def _plan_get(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"handle", "plan_handle", "plan_ref"})
        state = self._state_from_ref_or_handle(args, fallback_handle=_required(args, "handle"))
        node = _find_node(state, _required(args, "handle"))
        return {
            "text": f"Plan node read: {node['handle']} ({node['node_kind']})",
            "structured": {"plan_handle": state["plan_handle"], "node": node},
        }

    def _validate(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"plan_handle", "plan_ref"})
        state = self._state_from_ref_or_handle(args)
        stage = _plan_builder_stage(state, self.workspace)
        if stage == "architecture_sketch":
            try:
                artifact = self._compile_sketch_artifact(state, {})
                validation = validate_plan_sketch_artifact(artifact)
            except ValueError as exc:
                message = str(exc) or exc.__class__.__name__
                return {
                    "text": f"Plan sketch is invalid: {message}",
                    "structured": {
                        "plan_handle": state["plan_handle"],
                        "plan_validation": {"status": "invalid", "errors": [message]},
                    },
                }
            return {
                "text": "Plan sketch is valid.",
                "structured": {"plan_handle": state["plan_handle"], "plan_validation": validation},
            }
        if stage == "module_detail":
            try:
                artifact = self._compile_module_detail_artifact(state, {})
                validation = validate_module_detail_artifact(artifact)
            except ValueError as exc:
                message = str(exc) or exc.__class__.__name__
                return {
                    "text": f"Module detail is invalid: {message}",
                    "structured": {
                        "plan_handle": state["plan_handle"],
                        "plan_validation": {"status": "invalid", "errors": [message]},
                    },
                }
            return {
                "text": "Module detail is valid.",
                "structured": {"plan_handle": state["plan_handle"], "plan_validation": validation},
            }
        if _text(state.get("lifecycle")).lower() in {"submitted", "finalized"} and isinstance(state.get("source_plan_ref"), dict):
            source_payload = state.get("_source_plan_payload")
            if isinstance(source_payload, dict):
                return _plan_validate_result(source_payload, plan_handle=state["plan_handle"], valid_text="Submitted plan is dispatchable.", invalid_prefix="Submitted plan is invalid")
            try:
                artifact = self._compile_artifact(state, {})
            except ValueError as exc:
                message = str(exc) or exc.__class__.__name__
                return {
                    "text": f"Submitted plan is invalid: {message}",
                    "structured": {
                        "plan_handle": state["plan_handle"],
                        "plan_validation": {"status": "invalid", "errors": [message]},
                    },
                }
            payload = artifact
            return _plan_validate_result(payload, plan_handle=state["plan_handle"], valid_text="Submitted plan is dispatchable.", invalid_prefix="Submitted plan is invalid")
        try:
            artifact = self._compile_artifact(state, {})
        except ValueError as exc:
            message = str(exc) or exc.__class__.__name__
            return {
                "text": f"Plan draft is invalid: {message}",
                "structured": {
                    "plan_handle": state["plan_handle"],
                    "plan_validation": {"status": "invalid", "errors": [message]},
                },
            }
        validation_result = _plan_validate_result(artifact, plan_handle=state["plan_handle"], valid_text="Plan draft is dispatchable.", invalid_prefix="Plan draft is invalid")
        validation = dict(validation_result.get("structured", {}).get("plan_validation") or {})
        if str(validation.get("status") or "").strip().lower() == "invalid":
            return validation_result
        revision_errors = _revision_checklist_submit_errors(state)
        if revision_errors:
            validation = _validation_with_extra_errors(validation, revision_errors)
            return {
                "text": "Plan draft topology is dispatchable, but revision checklist is not satisfied: " + "; ".join(revision_errors),
                "structured": {"plan_handle": state["plan_handle"], "plan_validation": validation},
            }
        return validation_result

    def _plan_begin(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"goal", "plan_id", "summary", "languages", "source_refs", "workflow_next"})
        goal = _text(args.get("goal") or self.workspace.get("goal") or "")
        plan_id = _safe_id(args.get("plan_id"), default_prefix="plan")
        plan_handle = plan_id if plan_id.startswith("plan_") else f"plan_{plan_id}"
        gate_contract = _gate_contract_from_workspace(self.workspace)
        state = {
            "plan_handle": plan_handle,
            "plan_id": plan_id,
            "task_id": _text(self.workspace.get("task_id")),
            "goal": goal,
            "summary": _text(args.get("summary") or goal),
            "languages": _normalize_language_ids(args.get("languages")),
            "source_refs": _string_list(args.get("source_refs")),
            "workflow_next": _workflow_next_payload(args.get("workflow_next")),
            "gate_contract": gate_contract,
            "locked_gate_check_refs": [f"gate:{int(check.get('index') or 0)}" for check in _gate_checks({"gate_contract": gate_contract})],
            "constraints": [],
            "design_decisions": [],
            "modules": [],
            "system_test_plan": [],
            "risks": [],
            "closed": False,
            "lifecycle": "editing",
            "plan_revision": _coerce_int(self.workspace.get("planner_plan_revision"), default=0),
            "handle_counters": {"constraint": 0, "decision": 0, "module": 0, "milestone": 0, "ac": 0},
        }
        self._save_state(state)
        return {
            "text": f"Plan draft started: {plan_handle}",
            "structured": {"plan_handle": plan_handle, "plan_id": plan_id},
        }

    def _plan_checkout(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"source_plan_ref", "review_gate_ref", "plan_handle"})
        source_ref = self._resolve_source_plan_ref(dict(args.get("source_plan_ref") or {}))
        payload, digest, normalized_ref = self._load_plan_payload(source_ref)
        source_revision = plan_revision_from_payload(payload, normalized_ref)
        target_revision = _coerce_int(self.workspace.get("planner_plan_revision"), default=source_revision + 1)
        requested_handle = _text(args.get("plan_handle"))
        default_handle = f"plan_{_safe_id(payload.get('plan_id') or normalized_ref.get('plan_id'), default_prefix='plan')}_r{target_revision}"
        plan_handle = requested_handle or default_handle
        if not plan_handle.startswith("plan_"):
            plan_handle = f"plan_{plan_handle}"
        state = _state_from_plan_payload(
            payload,
            plan_handle=plan_handle,
            source_plan_ref={**normalized_ref, "sha256": digest},
            plan_revision=target_revision,
        )
        state["lifecycle"] = "editing"
        if isinstance(args.get("review_gate_ref"), dict):
            state["review_gate_ref"] = dict(args.get("review_gate_ref") or {})
        source_plan_handle = _source_plan_handle_from_payload(payload, normalized_ref)
        revision_checklist = _remap_plan_revision_checklist(
            _revision_checklist_from_workspace(self.workspace),
            source_plan_handle=source_plan_handle,
            target_plan_handle=plan_handle,
        )
        if revision_checklist:
            state["plan_revision_checklist"] = revision_checklist
        self._save_state(state)
        structured = {
            "plan_handle": plan_handle,
            "plan_id": state.get("plan_id"),
            "task_id": state.get("task_id"),
            "source_plan_ref": {**normalized_ref, "sha256": digest},
            "plan_revision": target_revision,
            "handle_tree": _plan_snapshot(state, full=False).get("handle_tree"),
        }
        if revision_checklist:
            structured["plan_revision_checklist"] = revision_checklist
        text = f"Plan draft checked out for revision: {plan_handle}"
        if revision_checklist:
            text += "\nRevision checklist:\n" + _revision_checklist_llm_text(revision_checklist)
        return {"text": text, "structured": structured}

    def _resolve_source_plan_ref(self, requested_ref: dict[str, Any]) -> dict[str, Any]:
        source_ref = dict(requested_ref or {})
        if str(source_ref.get("path") or "").strip():
            return source_ref
        for key in ("source_plan_ref", "review_target_plan_ref"):
            candidate = self.workspace.get(key)
            if not isinstance(candidate, dict) or not candidate:
                continue
            if _plan_ref_is_compatible(source_ref, candidate):
                return _merge_plan_ref(candidate, source_ref)
        return source_ref

    def _add_gate_check(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"plan_handle", "claim", "priority", "kind", "source_ref", "rationale", "mechanical_check"})
        state = self._load_state(_required(args, "plan_handle"))
        _assert_open_plan(state)
        check = _normalize_gate_check(
            {
                "claim": _required(args, "claim"),
                "priority": args.get("priority"),
                "kind": args.get("kind"),
                "source_ref": args.get("source_ref"),
                "rationale": args.get("rationale"),
                "mechanical_check": args.get("mechanical_check"),
            },
            index=len(_all_gate_checks(state)),
        )
        state.setdefault("gate_contract", {}).setdefault("checks", []).append(check)
        self._save_state(state)
        return {
            "text": f"Gate check added: gate:{check['index']}",
            "structured": {"plan_handle": state["plan_handle"], "check_index": check["index"], "check_ref": f"gate:{check['index']}"},
        }

    def _update_gate_check(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(
            args,
            {"plan_handle", "check_index", "check_ref", "claim", "priority", "kind", "source_ref", "rationale", "mechanical_check"},
        )
        state = self._load_state(_required(args, "plan_handle"))
        _assert_editable_plan(state)
        index = _gate_check_index(args)
        checks = _all_gate_checks(state)
        if index < 0 or index >= len(checks) or bool(checks[index].get("deleted")):
            raise ValueError(f"unknown gate check index: {index}")
        _assert_gate_check_mutable(state, index)
        updated = dict(checks[index])
        for key in ("claim", "priority", "kind", "source_ref", "rationale", "mechanical_check"):
            if key in args:
                updated[key] = args.get(key)
        checks[index] = _normalize_gate_check(updated, index=index)
        state.setdefault("gate_contract", {})["checks"] = checks
        self._save_state(state)
        return {
            "text": f"Gate check updated: gate:{index}",
            "structured": {"plan_handle": state["plan_handle"], "check_index": index, "check_ref": f"gate:{index}"},
        }

    def _delete_gate_check(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"plan_handle", "check_index", "check_ref"})
        state = self._load_state(_required(args, "plan_handle"))
        _assert_editable_plan(state)
        index = _gate_check_index(args)
        checks = _all_gate_checks(state)
        if index < 0 or index >= len(checks) or bool(checks[index].get("deleted")):
            raise ValueError(f"unknown gate check index: {index}")
        _assert_gate_check_mutable(state, index)
        checks[index] = {**dict(checks[index]), "deleted": True}
        state.setdefault("gate_contract", {})["checks"] = checks
        self._save_state(state)
        return {
            "text": f"Gate check deleted: gate:{index}",
            "structured": {"plan_handle": state["plan_handle"], "deleted_check_index": index, "deleted_check_ref": f"gate:{index}"},
        }

    def _add_constraints_batch(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"plan_handle", "constraints"})
        plan_handle = _required(args, "plan_handle")
        constraints = _dict_list(args.get("constraints"))
        if not constraints:
            raise ValueError("constraints must contain at least one constraint object")
        before = self._load_state(plan_handle)
        handles: list[str] = []
        try:
            for raw in constraints:
                payload = {"plan_handle": plan_handle}
                for key in ("statement", "kind", "strength", "source_ref", "rationale", "global_only", "gate_check_refs"):
                    if key in raw:
                        payload[key] = raw.get(key)
                result = self._add_constraint(payload)
                handles.append(str((result.get("structured") or {}).get("constraint_handle") or ""))
        except Exception:
            self._save_state(before)
            raise
        visible_handles = [item for item in handles if item]
        return {
            "text": (
                f"Constraints added: {len(visible_handles)}. "
                f"Use these real constraint_handles for linked_constraint_handles: {', '.join(visible_handles)}"
            ),
            "structured": {"plan_handle": plan_handle, "constraint_handles": visible_handles},
        }

    def _add_constraint(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"plan_handle", "statement", "kind", "strength", "source_ref", "rationale", "global_only", "gate_check_refs"})
        state = self._load_state(_required(args, "plan_handle"))
        _assert_open_plan(state)
        statement = _required(args, "statement")
        handle = self._next_handle(state, "constraint", "constraint")
        item = {
            "handle": handle,
            "id": _public_id("C", len(state["constraints"]) + 1),
            "kind": _text(args.get("kind") or "contract"),
            "strength": _strength(args.get("strength"), default="hard_contract"),
            "statement": statement,
            "source_ref": _text(args.get("source_ref")),
            "rationale": _text(args.get("rationale")),
            "global_only": bool(args.get("global_only")),
            "gate_check_refs": _known_gate_check_refs(state, args.get("gate_check_refs")),
        }
        state["constraints"].append(item)
        self._save_state(state)
        return {
            "text": f"Constraint added: {handle} (public id {item['id']}; use the real handle for links)",
            "structured": {"plan_handle": state["plan_handle"], "constraint_handle": handle, "constraint_id": item["id"]},
        }

    def _update_constraint(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(
            args,
            {"constraint_handle", "statement", "kind", "strength", "source_ref", "rationale", "global_only", "gate_check_refs"},
        )
        state, item = self._load_constraint(_required(args, "constraint_handle"))
        _assert_editable_plan(state)
        if "statement" in args:
            item["statement"] = _required(args, "statement")
        if "kind" in args:
            item["kind"] = _text(args.get("kind"))
        if "strength" in args:
            item["strength"] = _strength(args.get("strength"), default=_text(item.get("strength") or "hard_contract"))
        if "source_ref" in args:
            item["source_ref"] = _text(args.get("source_ref"))
        if "rationale" in args:
            item["rationale"] = _text(args.get("rationale"))
        if "global_only" in args:
            item["global_only"] = bool(args.get("global_only"))
        if "gate_check_refs" in args:
            item["gate_check_refs"] = _known_gate_check_refs(state, args.get("gate_check_refs"))
        self._save_state(state)
        return {
            "text": f"Constraint updated: {item['handle']}",
            "structured": {"plan_handle": state["plan_handle"], "constraint_handle": item["handle"]},
        }

    def _delete_constraint(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"constraint_handle"})
        constraint_handle = _required(args, "constraint_handle")
        state, _item = self._load_constraint(constraint_handle)
        _assert_editable_plan(state)
        referrers = _constraint_referrers(state, constraint_handle)
        if referrers:
            raise ValueError("cannot delete referenced constraint: " + ", ".join(referrers[:8]))
        state["constraints"] = [
            item for item in list(state.get("constraints") or []) if _text(item.get("handle")) != constraint_handle
        ]
        self._save_state(state)
        return {
            "text": f"Constraint deleted: {constraint_handle}",
            "structured": {"plan_handle": state["plan_handle"], "deleted_handle": constraint_handle},
        }

    def _add_design_decision(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(
            args,
            {
                "plan_handle",
                "question",
                "decision",
                "strength",
                "rationale",
                "alternatives",
                "downstream_effect",
                "linked_constraint_handles",
                "gate_check_refs",
            },
        )
        state = self._load_state(_required(args, "plan_handle"))
        _assert_open_plan(state)
        decision = _required(args, "decision")
        linked = _known_handles(state, _string_list(args.get("linked_constraint_handles")), expected_prefix="constraint")
        handle = self._next_handle(state, "decision", "decision")
        strength = _strength(args.get("strength"), default="chosen_contract")
        rationale = _text(args.get("rationale"))
        if strength in {"hard_contract", "chosen_contract"} and not rationale:
            raise ValueError("hard/chosen design decisions require rationale")
        item = {
            "handle": handle,
            "id": _public_id("D", len(state["design_decisions"]) + 1),
            "question": _text(args.get("question")),
            "decision": decision,
            "strength": strength,
            "rationale": rationale,
            "alternatives": _string_list(args.get("alternatives")),
            "downstream_effect": _text(args.get("downstream_effect")),
            "linked_constraint_handles": linked,
            "gate_check_refs": _known_gate_check_refs(state, args.get("gate_check_refs")),
        }
        state["design_decisions"].append(item)
        self._save_state(state)
        return {"text": f"Design decision added: {handle}", "structured": {"plan_handle": state["plan_handle"], "decision_handle": handle}}

    def _update_design_decision(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(
            args,
            {
                "decision_handle",
                "question",
                "decision",
                "strength",
                "rationale",
                "alternatives",
                "downstream_effect",
                "linked_constraint_handles",
                "gate_check_refs",
            },
        )
        state, item = self._load_design_decision(_required(args, "decision_handle"))
        _assert_editable_plan(state)
        if "question" in args:
            item["question"] = _text(args.get("question"))
        if "decision" in args:
            item["decision"] = _required(args, "decision")
        if "strength" in args:
            item["strength"] = _strength(args.get("strength"), default=_text(item.get("strength") or "chosen_contract"))
        if "rationale" in args:
            item["rationale"] = _text(args.get("rationale"))
        if "alternatives" in args:
            item["alternatives"] = _string_list(args.get("alternatives"))
        if "downstream_effect" in args:
            item["downstream_effect"] = _text(args.get("downstream_effect"))
        if "linked_constraint_handles" in args:
            item["linked_constraint_handles"] = _known_handles(
                state,
                _string_list(args.get("linked_constraint_handles")),
                expected_prefix="constraint",
            )
        if "gate_check_refs" in args:
            item["gate_check_refs"] = _known_gate_check_refs(state, args.get("gate_check_refs"))
        strength = _text(item.get("strength"))
        if strength in {"hard_contract", "chosen_contract"} and not _text(item.get("rationale")):
            raise ValueError("hard/chosen design decisions require rationale")
        self._save_state(state)
        return {
            "text": f"Design decision updated: {item['handle']}",
            "structured": {"plan_handle": state["plan_handle"], "decision_handle": item["handle"]},
        }

    def _delete_design_decision(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"decision_handle"})
        decision_handle = _required(args, "decision_handle")
        state, _item = self._load_design_decision(decision_handle)
        _assert_editable_plan(state)
        referrers = _decision_referrers(state, decision_handle)
        if referrers:
            raise ValueError("cannot delete referenced design decision: " + ", ".join(referrers[:8]))
        state["design_decisions"] = [
            item for item in list(state.get("design_decisions") or []) if _text(item.get("handle")) != decision_handle
        ]
        self._save_state(state)
        return {
            "text": f"Design decision deleted: {decision_handle}",
            "structured": {"plan_handle": state["plan_handle"], "deleted_handle": decision_handle},
        }

    def _add_module_outline(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(
            args,
            {
                "plan_handle",
                "module_name",
                "kind",
                "depends_on_module_handles",
                "depends_on_module_names",
                "responsibility",
                "owned_area",
                "ownership",
                "lifecycle",
                "invariants",
                "scope_guard",
                "constraint_handles",
                "decision_handles",
                "gate_check_refs",
                "languages",
                "executor_profile",
                "module_quality_criteria",
                "risk_surfaces",
                "delivery_surfaces",
                "interfaces",
                "provided_interfaces",
                "consumed_interfaces",
                "milestones",
            },
        )
        plan_handle = _required(args, "plan_handle")
        milestones = _dict_list(args.get("milestones"))
        state_for_stage = self._load_state(plan_handle)
        sketch_stage = _plan_builder_stage(state_for_stage, self.workspace) == "architecture_sketch"
        if not milestones and not sketch_stage:
            raise ValueError("milestones must contain at least one milestone outline")
        before = state_for_stage
        milestone_handles: list[str] = []
        acceptance_handles: list[str] = []
        module_handle = ""
        try:
            module_args = {"plan_handle": plan_handle}
            for key in (
                "module_name",
                "kind",
                "depends_on_module_handles",
                "depends_on_module_names",
                "responsibility",
                "owned_area",
                "ownership",
                "lifecycle",
                "invariants",
                "scope_guard",
                "constraint_handles",
                "decision_handles",
                "gate_check_refs",
                "languages",
                "executor_profile",
                "module_quality_criteria",
                "risk_surfaces",
                "delivery_surfaces",
            ):
                if key in args:
                    module_args[key] = args.get(key)
            opened = self._begin_module(module_args)
            module_handle = str((opened.get("structured") or {}).get("module_handle") or "")
            module_name = str((opened.get("structured") or {}).get("module_name") or "")
            for raw_interface in _module_outline_interfaces(args):
                interface_args = {"module_handle": module_handle}
                interface_args.update(raw_interface)
                self._add_module_interface(interface_args)
            for raw_milestone in milestones:
                milestone_args = {"module_handle": module_handle}
                for key in (
                    "title",
                    "task",
                    "scope_guard",
                    "changed_area",
                    "constraint_handles",
                    "decision_handles",
                    "gate_check_refs",
                    "languages",
                    "tests_required",
                    "public_api_added",
                    "checkpoint_admission_evidence",
                ):
                    if key in raw_milestone:
                        milestone_args[key] = raw_milestone.get(key)
                milestone_args["acceptance_criteria"] = (
                    raw_milestone.get("acceptance_criteria") or raw_milestone.get("acceptance") or raw_milestone.get("criteria")
                )
                result = self._add_milestone_outline(milestone_args)
                structured = dict(result.get("structured") or {})
                milestone_handles.append(str(structured.get("milestone_handle") or ""))
                acceptance_handles.extend(str(item) for item in list(structured.get("acceptance_handles") or []) if item)
            self._end_module({"module_handle": module_handle})
        except Exception:
            self._save_state(before)
            raise
        return {
            "text": (
                f"Module outline added and closed: module_name={module_name}; "
                f"module_handle={module_handle}; "
                f"milestone_handles={', '.join(item for item in milestone_handles if item) or '(none)'}; "
                f"acceptance_handles={', '.join(item for item in acceptance_handles if item) or '(none)'}. "
                f"This module is closed for milestone edits. Use plan_handle={plan_handle} to add the next module, "
                "or use plan_add_module_interface with module_name only for later interface repair."
            ),
            "structured": {
                "plan_handle": plan_handle,
                "parent_plan_handle": plan_handle,
                "module_handle": module_handle,
                "module_name": module_name,
                "module_closed": True,
                "milestone_handles": [item for item in milestone_handles if item],
                "acceptance_handles": [item for item in acceptance_handles if item],
                "next_tool_hint": "Use plan_handle with plan_add_module_outline for the next module. Do not add milestones to this closed module.",
            },
        }

    def _add_module_outlines_batch(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"plan_handle", "modules"})
        plan_handle = _required(args, "plan_handle")
        modules = _dict_list(args.get("modules"))
        if not modules:
            raise ValueError("modules must contain at least one module outline")
        before = self._load_state(plan_handle)
        results: list[dict[str, Any]] = []
        try:
            for index, raw_module in enumerate(modules, start=1):
                if "plan_handle" in raw_module:
                    raise ValueError(f"modules[{index}].plan_handle is not allowed; pass top-level plan_handle")
                module_args = {"plan_handle": plan_handle}
                module_args.update(raw_module)
                result = self._add_module_outline(module_args)
                structured = dict(result.get("structured") or {})
                results.append(
                    {
                        "module_name": str(structured.get("module_name") or _text(raw_module.get("module_name")) or ""),
                        "module_handle": str(structured.get("module_handle") or ""),
                        "module_closed": bool(structured.get("module_closed", True)),
                        "milestone_handles": [str(item) for item in list(structured.get("milestone_handles") or []) if str(item or "").strip()],
                        "acceptance_handles": [str(item) for item in list(structured.get("acceptance_handles") or []) if str(item or "").strip()],
                    }
                )
        except Exception:
            self._save_state(before)
            raise
        return {
            "text": (
                f"Module outline batch added and closed: {len(results)} module(s). "
                "Use returned module_name/module_handle mappings for later repair or dependency references."
            ),
            "structured": {
                "plan_handle": plan_handle,
                "modules": results,
                "module_handles": [item["module_handle"] for item in results if item["module_handle"]],
                "module_names": [item["module_name"] for item in results if item["module_name"]],
            },
        }

    def _begin_module(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(
            args,
            {
                "plan_handle",
                "module_name",
                "kind",
                "depends_on_module_handles",
                "depends_on_module_names",
                "responsibility",
                "owned_area",
                "ownership",
                "lifecycle",
                "invariants",
                "scope_guard",
                "constraint_handles",
                "decision_handles",
                "gate_check_refs",
                "languages",
                "executor_profile",
                "module_quality_criteria",
                "risk_surfaces",
                "delivery_surfaces",
            },
        )
        state = self._load_state(_required(args, "plan_handle"))
        _assert_open_plan(state)
        if _open_module(state):
            raise ValueError("close the open module before beginning another module")
        kind = _module_kind(args.get("kind"))
        dependencies = _known_module_refs(
            state,
            [
                *_string_list(args.get("depends_on_module_handles")),
                *_string_list(args.get("depends_on_module_names")),
            ],
        )
        for dependency in dependencies:
            module = _find_module_by_handle(state, dependency)
            if not module.get("closed"):
                raise ValueError(f"depends_on_module_handles includes an open module: {dependency}")
        module_id = _explicit_module_id(args.get("module_name"))
        if any(_text(module.get("module_id")) == module_id for module in state["modules"]):
            raise ValueError(f"module_name is duplicated: {module_id}")
        handle = self._next_handle(state, "module", "module")
        item = {
            "handle": handle,
            "module_id": module_id,
            "kind": kind,
            "depends_on_module_handles": dependencies,
            "responsibility": _required(args, "responsibility"),
            "owned_area": normalize_plan_write_areas(args.get("owned_area"), field_name="owned_area"),
            "ownership": _string_list(args.get("ownership")),
            "lifecycle": _string_list(args.get("lifecycle")),
            "invariants": _string_list(args.get("invariants")),
            "scope_guard": _text(args.get("scope_guard")),
            "constraint_handles": _known_handles(state, _string_list(args.get("constraint_handles")), expected_prefix="constraint"),
            "decision_handles": _known_handles(state, _string_list(args.get("decision_handles")), expected_prefix="decision"),
            "gate_check_refs": _known_gate_check_refs(state, args.get("gate_check_refs")),
            "languages": _normalize_language_ids(args.get("languages")),
            "executor_profile": _text(args.get("executor_profile")),
            "module_quality_criteria": _string_list(args.get("module_quality_criteria")),
            "risk_surfaces": _string_list(args.get("risk_surfaces")),
            "delivery_surfaces": _string_list(args.get("delivery_surfaces")),
            "provided_interfaces": [],
            "consumed_interfaces": [],
            "internal_milestones": [],
            "closed": False,
        }
        if not item["owned_area"]:
            raise ValueError("owned_area must contain at least one canonical repo-relative path or explicit logical area")
        if not item["ownership"]:
            raise ValueError("ownership must contain at least one module ownership bullet")
        if not item["lifecycle"]:
            raise ValueError("lifecycle must contain at least one module lifecycle bullet")
        if not item["invariants"]:
            raise ValueError("invariants must contain at least one module invariant bullet")
        state["modules"].append(item)
        self._save_state(state)
        return {
            "text": f"Module opened: {module_id} ({handle}). Use module_name={module_id} with later module-scoped tools.",
            "structured": {"module_handle": handle, "module_name": module_id, "plan_handle": state["plan_handle"]},
        }

    def _add_module_interface(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(
            args,
            {
                "module_handle",
                "module_name",
                "plan_handle",
                "direction",
                "name",
                "shape",
                "lifecycle",
                "ownership",
                "error_behavior",
                "compatibility",
                "producer",
                "consumer",
                "import_path",
                "source_path",
                "public_entrypoint",
                "copy_policy",
            },
        )
        state, module = self._load_module_from_args(args)
        _assert_editable_plan(state)
        direction = _text(args.get("direction")).lower()
        if direction not in {"provided", "consumed"}:
            raise ValueError("direction must be provided or consumed")
        item = {
            "name": _required(args, "name"),
            "shape": _required(args, "shape"),
            "lifecycle": _required(args, "lifecycle"),
            "ownership": _required(args, "ownership"),
            "error_behavior": _required(args, "error_behavior"),
            "compatibility": _required(args, "compatibility"),
            "producer": _text(args.get("producer") or module.get("module_id")),
            "consumer": _text(args.get("consumer")),
        }
        for key in ("import_path", "source_path", "public_entrypoint"):
            value = _text(args.get(key))
            if value:
                item[key] = value
        copy_policy = _text(args.get("copy_policy"))
        if copy_policy:
            if copy_policy not in {"import_only", "copy_allowed"}:
                raise ValueError("copy_policy must be import_only or copy_allowed")
            item["copy_policy"] = copy_policy
        target = "provided_interfaces" if direction == "provided" else "consumed_interfaces"
        module[target].append(item)
        self._save_state(state)
        return {
            "text": f"Module interface added: {item['name']}",
            "structured": {"module_handle": module["handle"], "module_name": module.get("module_id"), "plan_handle": state["plan_handle"]},
        }

    def _add_module_acceptance_criteria_batch(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"module_handle", "module_name", "plan_handle", "criteria"})
        criteria = _module_acceptance_criteria_list(args.get("criteria"))
        if not criteria:
            raise ValueError("criteria must contain at least one module-level acceptance criterion")
        state, module = self._load_module_from_args(args)
        _assert_editable_plan(state)
        added: list[str] = []
        added_indexes: list[int] = []
        linked_constraints: list[str] = []
        linked_gates: list[str] = []
        for raw in criteria:
            constraint_handles = _known_handles(
                state,
                _string_list(raw.get("linked_constraint_handles")),
                expected_prefix="constraint",
            )
            gate_check_refs = _known_gate_check_refs(state, raw.get("gate_check_refs"))
            criterion = _required(raw, "criterion")
            existing = _string_list(module.get("module_quality_criteria"))
            if criterion not in existing:
                existing.append(criterion)
                added.append(criterion)
                added_indexes.append(len(existing) - 1)
            module["module_quality_criteria"] = existing
            detail = _module_acceptance_criterion_detail(
                raw,
                constraint_handles=constraint_handles,
                gate_check_refs=gate_check_refs,
            )
            if detail:
                existing_details = _dict_list(module.get("module_quality_criteria_details"))
                if detail not in existing_details:
                    existing_details.append(detail)
                    module["module_quality_criteria_details"] = existing_details
            linked_constraints.extend(constraint_handles)
            linked_gates.extend(gate_check_refs)
        if linked_constraints:
            module["constraint_handles"] = _dedupe_strings([*_string_list(module.get("constraint_handles")), *linked_constraints])
        if linked_gates:
            module["gate_check_refs"] = _dedupe_strings([*_string_list(module.get("gate_check_refs")), *linked_gates])
        self._save_state(state)
        return {
            "text": f"Module-level acceptance criteria added: {len(added)} for {module.get('module_id')}.",
            "structured": {
                "plan_handle": state["plan_handle"],
                "module_handle": module["handle"],
                "module_name": module.get("module_id"),
                "module_acceptance_indexes": added_indexes,
                "module_quality_criteria": _string_list(module.get("module_quality_criteria")),
                "module_quality_criteria_details": _dict_list(module.get("module_quality_criteria_details")),
            },
        }

    def _add_module_acceptance_criterion(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(
            args,
            {
                "module_handle",
                "module_name",
                "plan_handle",
                "criterion",
                "evidence_expectation",
                "linked_constraint_handles",
                "gate_check_refs",
                "source_ref",
                "source_refs",
                "reference_refs",
                "negative_cases",
                "quantifier",
            },
        )
        payload = {
            "module_handle": args.get("module_handle"),
            "module_name": args.get("module_name"),
            "plan_handle": args.get("plan_handle"),
            "criteria": [
                {
                    "criterion": args.get("criterion"),
                    "evidence_expectation": args.get("evidence_expectation"),
                    "linked_constraint_handles": args.get("linked_constraint_handles"),
                    "gate_check_refs": args.get("gate_check_refs"),
                    "source_ref": args.get("source_ref"),
                    "source_refs": args.get("source_refs"),
                    "reference_refs": args.get("reference_refs"),
                    "negative_cases": args.get("negative_cases"),
                    "quantifier": args.get("quantifier"),
                }
            ],
        }
        return self._add_module_acceptance_criteria_batch({key: value for key, value in payload.items() if value is not None})

    def _add_milestone_outline(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(
            args,
            {
                "module_handle",
                "module_name",
                "plan_handle",
                "title",
                "task",
                "scope_guard",
                "changed_area",
                "constraint_handles",
                "decision_handles",
                "gate_check_refs",
                "languages",
                "tests_required",
                "public_api_added",
                "checkpoint_admission_evidence",
                "acceptance_criteria",
            },
        )
        state, module = self._load_module_from_args(args)
        module_handle = _text(module.get("handle"))
        criteria = _acceptance_criteria_list(args.get("acceptance_criteria"))
        if not criteria:
            raise ValueError("acceptance_criteria must contain at least one criterion object or string")
        before = state
        milestone_handle = ""
        try:
            milestone_args = {"module_handle": module_handle}
            for key in (
                "title",
                "task",
                "scope_guard",
                "changed_area",
                "constraint_handles",
                "decision_handles",
                "gate_check_refs",
                "languages",
                "tests_required",
                "public_api_added",
                "checkpoint_admission_evidence",
            ):
                if key in args:
                    milestone_args[key] = args.get(key)
            opened = self._begin_milestone(milestone_args)
            milestone_handle = str((opened.get("structured") or {}).get("milestone_handle") or "")
            added = self._add_acceptance_criteria_batch({"milestone_handle": milestone_handle, "criteria": criteria})
            self._end_milestone({"milestone_handle": milestone_handle})
        except Exception:
            self._save_state(before)
            raise
        return {
            "text": (
                f"Milestone outline added and closed: milestone_handle={milestone_handle}; "
                f"module_name={module.get('module_id')}; "
                f"acceptance_handles={', '.join(str(item) for item in list((added.get('structured') or {}).get('acceptance_handles') or []) if item) or '(none)'}. "
                f"This milestone is closed. Use module_handle={module_handle} or module_name={module.get('module_id')} "
                "to add another milestone to this still-open module, or close the module when all milestones are present."
            ),
            "structured": {
                "plan_handle": state["plan_handle"],
                "module_handle": module_handle,
                "parent_module_handle": module_handle,
                "module_name": module.get("module_id"),
                "milestone_handle": milestone_handle,
                "milestone_closed": True,
                "acceptance_handles": list((added.get("structured") or {}).get("acceptance_handles") or []),
                "next_tool_hint": "Use module_handle/module_name with plan_add_milestone_outline for another milestone, or plan_end_module when the module is complete.",
            },
        }

    def _begin_milestone(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(
            args,
            {
                "module_handle",
                "module_name",
                "plan_handle",
                "title",
                "task",
                "scope_guard",
                "changed_area",
                "constraint_handles",
                "decision_handles",
                "gate_check_refs",
                "languages",
                "tests_required",
                "public_api_added",
                "checkpoint_admission_evidence",
            },
        )
        state, module = self._load_module_from_args(args)
        if _plan_builder_stage(state, self.workspace) == "module_detail":
            _assert_bound_module_detail(state, module)
        else:
            _assert_open_module(module)
        if _open_milestone(module):
            raise ValueError("close the open milestone before beginning another milestone")
        handle = self._next_handle(state, "milestone", "milestone")
        milestone_index = len(module["internal_milestones"]) + 1
        item = {
            "handle": handle,
            "milestone_id": f"{module['module_id']}_m{milestone_index}",
            "title": _required(args, "title"),
            "task": _required(args, "task"),
            "scope_guard": _text(args.get("scope_guard")),
            "changed_area": normalize_plan_write_areas(args.get("changed_area"), field_name="changed_area"),
            "constraint_handles": _known_handles(state, _string_list(args.get("constraint_handles")), expected_prefix="constraint"),
            "decision_handles": _known_handles(state, _string_list(args.get("decision_handles")), expected_prefix="decision"),
            "gate_check_refs": _known_gate_check_refs(state, args.get("gate_check_refs")),
            "languages": _normalize_language_ids(args.get("languages")),
            "tests_required": _string_list(args.get("tests_required")),
            "public_api_added": _text(args.get("public_api_added")),
            "checkpoint_admission_evidence": _string_list(args.get("checkpoint_admission_evidence")),
            "acceptance": [],
            "closed": False,
        }
        module["internal_milestones"].append(item)
        self._save_state(state)
        return {"text": f"Milestone opened: {handle}", "structured": {"milestone_handle": handle, "module_handle": module["handle"]}}

    def _add_acceptance_criteria_batch(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"milestone_handle", "criteria"})
        milestone_handle = _required(args, "milestone_handle")
        criteria = _acceptance_criteria_list(args.get("criteria"))
        if not criteria:
            raise ValueError("criteria must contain at least one acceptance criterion object or string")
        before, _module, _milestone = self._load_milestone(milestone_handle)
        handles: list[str] = []
        try:
            for raw in criteria:
                payload = {"milestone_handle": milestone_handle}
                for key in ("criterion", "evidence_expectation", "linked_constraint_handles", "gate_check_refs", "negative_cases", "quantifier"):
                    if key in raw:
                        payload[key] = raw.get(key)
                result = self._add_acceptance_criterion(payload)
                handles.append(str((result.get("structured") or {}).get("acceptance_handle") or ""))
            _state, _loaded_module, loaded_milestone = self._load_milestone(milestone_handle)
            _validate_acceptance_consistency(loaded_milestone)
        except Exception:
            self._save_state(before)
            raise
        visible_handles = [item for item in handles if item]
        return {
            "text": (
                f"Acceptance criteria added: {len(visible_handles)}. "
                f"acceptance_handles={', '.join(visible_handles)}"
            ),
            "structured": {"milestone_handle": milestone_handle, "acceptance_handles": visible_handles},
        }

    def _add_acceptance_criterion(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(
            args,
            {"milestone_handle", "criterion", "evidence_expectation", "linked_constraint_handles", "gate_check_refs", "negative_cases", "quantifier"},
        )
        state, module, milestone = self._load_milestone(_required(args, "milestone_handle"))
        _assert_open_milestone(milestone)
        handle = self._next_handle(state, "ac", "ac")
        criterion = _required(args, "criterion")
        evidence = _required(args, "evidence_expectation")
        negative_cases = _string_list(args.get("negative_cases"))
        if "negative_cases" not in args:
            raise ValueError("negative_cases is required; pass [] only when no meaningful negative/boundary case applies")
        if _acceptance_requires_negative_cases(criterion, evidence) and not negative_cases:
            raise ValueError(
                "negative_cases must contain concrete examples for reject/error/boundary acceptance criteria"
            )
        item = {
            "handle": handle,
            "id": _public_id("AC", len(milestone["acceptance"]) + 1),
            "criterion": criterion,
            "evidence_expectation": evidence,
            "linked_constraint_handles": _known_handles(state, _string_list(args.get("linked_constraint_handles")), expected_prefix="constraint"),
            "gate_check_refs": _known_gate_check_refs(state, args.get("gate_check_refs")),
            "negative_cases": negative_cases,
            "quantifier": _text(args.get("quantifier")),
        }
        milestone["acceptance"].append(item)
        self._save_state(state)
        return {
            "text": f"Acceptance criterion added: {handle}",
            "structured": {"acceptance_handle": handle, "milestone_handle": milestone["handle"], "module_handle": module["handle"]},
        }

    def _end_milestone(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"milestone_handle"})
        state, module, milestone = self._load_milestone(_required(args, "milestone_handle"))
        _assert_open_milestone(milestone)
        if not milestone["acceptance"]:
            raise ValueError("milestone must have at least one acceptance criterion before closing")
        for index, item in enumerate(milestone["acceptance"]):
            if not _text(item.get("criterion")):
                raise ValueError(f"acceptance[{index}].criterion is required")
            if not _text(item.get("evidence_expectation")):
                raise ValueError(f"acceptance[{index}].evidence_expectation is required")
        _validate_acceptance_consistency(milestone)
        milestone["closed"] = True
        self._save_state(state)
        return {
            "text": (
                f"Milestone closed: {milestone['handle']}. "
                f"Use module_handle={module['handle']} or module_name={module.get('module_id')} to add the next milestone, "
                "or close the module when all milestones are present."
            ),
            "structured": {
                "module_handle": module["handle"],
                "parent_module_handle": module["handle"],
                "module_name": module.get("module_id"),
                "plan_handle": state["plan_handle"],
                "milestone_handle": milestone["handle"],
                "milestone_closed": True,
                "next_tool_hint": "Use module_handle/module_name with plan_add_milestone_outline for another milestone, or plan_end_module when the module is complete.",
            },
        }

    def _end_module(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"module_handle", "module_name", "plan_handle"})
        state, module = self._load_module_from_args(args)
        _assert_open_module(module)
        if _open_milestone(module):
            raise ValueError("close the open milestone before closing the module")
        if not module["internal_milestones"] and _plan_builder_stage(state, self.workspace) != "architecture_sketch":
            raise ValueError("module must have at least one milestone before closing")
        module["closed"] = True
        self._save_state(state)
        return {
            "text": (
                f"Module closed: {module['module_id']} ({module['handle']}). "
                f"Use plan_handle={state['plan_handle']} to add the next module or validate/submit the plan. "
                "Do not add more milestones to this closed module."
            ),
            "structured": {
                "plan_handle": state["plan_handle"],
                "parent_plan_handle": state["plan_handle"],
                "module_handle": module["handle"],
                "module_name": module.get("module_id"),
                "module_closed": True,
                "next_tool_hint": "Use plan_handle with plan_add_module_outline for the next module, or plan_validate_and_submit_for_review when all modules are complete.",
            },
        }

    def _update_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(
            args,
            {"plan_handle", "summary", "system_test_plan", "risks", "languages", "source_refs", "workflow_next"},
        )
        state = self._load_state(_required(args, "plan_handle"))
        _assert_editable_plan(state)
        if "summary" in args:
            state["summary"] = _text(args.get("summary"))
        if "system_test_plan" in args:
            state["system_test_plan"] = _dict_list(args.get("system_test_plan"))
        if "risks" in args:
            state["risks"] = _dict_list(args.get("risks"))
        if "languages" in args:
            state["languages"] = _normalize_language_ids(args.get("languages"))
        if "source_refs" in args:
            state["source_refs"] = _string_list(args.get("source_refs"))
        if "workflow_next" in args:
            state["workflow_next"] = _workflow_next_payload(args.get("workflow_next"))
        self._save_state(state)
        return {"text": f"Plan updated: {state['plan_handle']}", "structured": {"plan_handle": state["plan_handle"]}}

    def _update_module(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(
            args,
            {
                "module_handle",
                "module_name",
                "plan_handle",
                "kind",
                "depends_on_module_handles",
                "depends_on_module_names",
                "responsibility",
                "owned_area",
                "ownership",
                "lifecycle",
                "invariants",
                "scope_guard",
                "constraint_handles",
                "decision_handles",
                "gate_check_refs",
                "languages",
                "executor_profile",
                "module_quality_criteria",
                "risk_surfaces",
                "delivery_surfaces",
            },
        )
        state, module = self._load_module_from_args(args)
        _assert_editable_plan(state)
        if "module_name" in args:
            module_id = _explicit_module_id(args.get("module_name"))
            if any(_text(item.get("module_id")) == module_id and _text(item.get("handle")) != module["handle"] for item in state["modules"]):
                raise ValueError(f"module_name is duplicated: {module_id}")
            module["module_id"] = module_id
        if "kind" in args:
            module["kind"] = _module_kind(args.get("kind"))
        if "depends_on_module_handles" in args:
            dependencies = _known_module_refs(state, _string_list(args.get("depends_on_module_handles")))
            if module["handle"] in dependencies:
                raise ValueError("module cannot depend on itself")
            module["depends_on_module_handles"] = dependencies
        if "depends_on_module_names" in args:
            dependencies = _known_module_refs(state, _string_list(args.get("depends_on_module_names")))
            if module["handle"] in dependencies:
                raise ValueError("module cannot depend on itself")
            module["depends_on_module_handles"] = dependencies
        if "responsibility" in args:
            module["responsibility"] = _required(args, "responsibility")
        if "owned_area" in args:
            owned_area = normalize_plan_write_areas(args.get("owned_area"), field_name="owned_area")
            if not owned_area:
                raise ValueError("owned_area must contain at least one canonical repo-relative path or explicit logical area")
            module["owned_area"] = owned_area
        if "ownership" in args:
            ownership = _string_list(args.get("ownership"))
            if not ownership:
                raise ValueError("ownership must contain at least one module ownership bullet")
            module["ownership"] = ownership
        if "lifecycle" in args:
            lifecycle = _string_list(args.get("lifecycle"))
            if not lifecycle:
                raise ValueError("lifecycle must contain at least one module lifecycle bullet")
            module["lifecycle"] = lifecycle
        if "invariants" in args:
            invariants = _string_list(args.get("invariants"))
            if not invariants:
                raise ValueError("invariants must contain at least one module invariant bullet")
            module["invariants"] = invariants
        if "scope_guard" in args:
            module["scope_guard"] = _text(args.get("scope_guard"))
        if "constraint_handles" in args:
            module["constraint_handles"] = _known_handles(state, _string_list(args.get("constraint_handles")), expected_prefix="constraint")
        if "decision_handles" in args:
            module["decision_handles"] = _known_handles(state, _string_list(args.get("decision_handles")), expected_prefix="decision")
        if "gate_check_refs" in args:
            module["gate_check_refs"] = _known_gate_check_refs(state, args.get("gate_check_refs"))
        if "languages" in args:
            module["languages"] = _normalize_language_ids(args.get("languages"))
        if "executor_profile" in args:
            module["executor_profile"] = _text(args.get("executor_profile"))
        if "module_quality_criteria" in args:
            module["module_quality_criteria"] = _string_list(args.get("module_quality_criteria"))
        if "risk_surfaces" in args:
            module["risk_surfaces"] = _string_list(args.get("risk_surfaces"))
        if "delivery_surfaces" in args:
            module["delivery_surfaces"] = _string_list(args.get("delivery_surfaces"))
        self._renumber_milestones(module)
        self._save_state(state)
        return {
            "text": f"Module updated: {module['handle']}",
            "structured": {"plan_handle": state["plan_handle"], "module_handle": module["handle"], "module_name": module.get("module_id")},
        }

    def _delete_module(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"module_handle", "module_name", "plan_handle"})
        state, module = self._load_module_from_args(args)
        module_handle = _text(module.get("handle"))
        _assert_editable_plan(state)
        dependents = [
            module.get("module_id") or module.get("handle")
            for module in list(state.get("modules") or [])
            if module_handle in _string_list(module.get("depends_on_module_handles"))
        ]
        if dependents:
            raise ValueError("cannot delete module while other modules depend on it: " + ", ".join(str(item) for item in dependents))
        state["modules"] = [module for module in list(state.get("modules") or []) if _text(module.get("handle")) != module_handle]
        self._save_state(state)
        return {"text": f"Module deleted: {module_handle}", "structured": {"plan_handle": state["plan_handle"], "deleted_handle": module_handle}}

    def _merge_modules(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"target_module_handle", "source_module_handle", "insert_milestones_at"})
        target_handle = _required(args, "target_module_handle")
        source_handle = _required(args, "source_module_handle")
        if target_handle == source_handle:
            raise ValueError("target_module_handle and source_module_handle must be different")
        state, target = self._load_module(target_handle)
        _assert_editable_plan(state)
        source = _find_module_by_handle(state, source_handle)
        moved_milestones = [_text(item.get("handle")) for item in list(source.get("internal_milestones") or [])]

        target_items = list(target.get("internal_milestones") or [])
        insert_at = _coerce_int(args.get("insert_milestones_at"), default=len(target_items))
        insert_at = max(0, min(len(target_items), insert_at))
        target["internal_milestones"] = (
            target_items[:insert_at]
            + [dict(item) for item in list(source.get("internal_milestones") or [])]
            + target_items[insert_at:]
        )

        target["owned_area"] = _dedupe_strings([*list(target.get("owned_area") or []), *list(source.get("owned_area") or [])])
        target["ownership"] = _dedupe_strings([*list(target.get("ownership") or []), *list(source.get("ownership") or [])])
        target["lifecycle"] = _dedupe_strings([*list(target.get("lifecycle") or []), *list(source.get("lifecycle") or [])])
        target["invariants"] = _dedupe_strings([*list(target.get("invariants") or []), *list(source.get("invariants") or [])])
        target["constraint_handles"] = _dedupe_strings(
            [*list(target.get("constraint_handles") or []), *list(source.get("constraint_handles") or [])]
        )
        target["decision_handles"] = _dedupe_strings(
            [*list(target.get("decision_handles") or []), *list(source.get("decision_handles") or [])]
        )
        target["gate_check_refs"] = _dedupe_strings([*list(target.get("gate_check_refs") or []), *list(source.get("gate_check_refs") or [])])
        target["languages"] = _dedupe_strings([*list(target.get("languages") or []), *list(source.get("languages") or [])])
        target["depends_on_module_handles"] = [
            handle
            for handle in _dedupe_strings([*list(target.get("depends_on_module_handles") or []), *list(source.get("depends_on_module_handles") or [])])
            if handle not in {target_handle, source_handle}
        ]
        target["provided_interfaces"] = _merged_module_interfaces(
            target.get("provided_interfaces"),
            source.get("provided_interfaces"),
            module_handle=target_handle,
        )
        target["consumed_interfaces"] = _merged_module_interfaces(
            target.get("consumed_interfaces"),
            source.get("consumed_interfaces"),
            module_handle=target_handle,
        )
        source_scope = _text(source.get("scope_guard"))
        if source_scope and source_scope not in _text(target.get("scope_guard")):
            target["scope_guard"] = _join_sentences(_text(target.get("scope_guard")), source_scope)
        source_responsibility = _text(source.get("responsibility"))
        if source_responsibility and source_responsibility not in _text(target.get("responsibility")):
            target["responsibility"] = _join_sentences(_text(target.get("responsibility")), source_responsibility)

        rewired_dependents: list[str] = []
        for module in list(state.get("modules") or []):
            if _text(module.get("handle")) == source_handle:
                continue
            dependencies = []
            changed = False
            for handle in _string_list(module.get("depends_on_module_handles")):
                replacement = target_handle if handle == source_handle else handle
                if replacement == _text(module.get("handle")):
                    changed = True
                    continue
                dependencies.append(replacement)
                changed = changed or replacement != handle
            if changed:
                module["depends_on_module_handles"] = _dedupe_strings(dependencies)
                rewired_dependents.append(_text(module.get("handle")))

        state["modules"] = [module for module in list(state.get("modules") or []) if _text(module.get("handle")) != source_handle]
        self._renumber_milestones(target)
        self._save_state(state)
        return {
            "text": f"Module merged: {source_handle} -> {target_handle}",
            "structured": {
                "plan_handle": state["plan_handle"],
                "target_module_handle": target_handle,
                "deleted_source_module_handle": source_handle,
                "moved_milestone_handles": moved_milestones,
                "rewired_dependent_module_handles": rewired_dependents,
                "handle_tree": _plan_snapshot(state, full=False).get("handle_tree"),
            },
        }

    def _update_milestone(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(
            args,
            {
                "milestone_handle",
                "title",
                "task",
                "scope_guard",
                "changed_area",
                "constraint_handles",
                "decision_handles",
                "gate_check_refs",
                "languages",
                "tests_required",
                "public_api_added",
                "checkpoint_admission_evidence",
            },
        )
        state, module, milestone = self._load_milestone(_required(args, "milestone_handle"))
        _assert_editable_plan(state)
        if "title" in args:
            milestone["title"] = _required(args, "title")
        if "task" in args:
            milestone["task"] = _required(args, "task")
        if "scope_guard" in args:
            milestone["scope_guard"] = _text(args.get("scope_guard"))
        if "changed_area" in args:
            milestone["changed_area"] = normalize_plan_write_areas(args.get("changed_area"), field_name="changed_area")
        if "constraint_handles" in args:
            milestone["constraint_handles"] = _known_handles(state, _string_list(args.get("constraint_handles")), expected_prefix="constraint")
        if "decision_handles" in args:
            milestone["decision_handles"] = _known_handles(state, _string_list(args.get("decision_handles")), expected_prefix="decision")
        if "gate_check_refs" in args:
            milestone["gate_check_refs"] = _known_gate_check_refs(state, args.get("gate_check_refs"))
        if "languages" in args:
            milestone["languages"] = _normalize_language_ids(args.get("languages"))
        if "tests_required" in args:
            milestone["tests_required"] = _string_list(args.get("tests_required"))
        if "public_api_added" in args:
            milestone["public_api_added"] = _text(args.get("public_api_added"))
        if "checkpoint_admission_evidence" in args:
            milestone["checkpoint_admission_evidence"] = _string_list(args.get("checkpoint_admission_evidence"))
        self._renumber_milestones(module)
        self._save_state(state)
        return {"text": f"Milestone updated: {milestone['handle']}", "structured": {"plan_handle": state["plan_handle"], "milestone_handle": milestone["handle"], "module_handle": module["handle"]}}

    def _delete_milestone(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"milestone_handle"})
        milestone_handle = _required(args, "milestone_handle")
        state, module, _milestone = self._load_milestone(milestone_handle)
        _assert_editable_plan(state)
        module["internal_milestones"] = [
            item for item in list(module.get("internal_milestones") or []) if _text(item.get("handle")) != milestone_handle
        ]
        self._renumber_milestones(module)
        self._save_state(state)
        return {"text": f"Milestone deleted: {milestone_handle}", "structured": {"plan_handle": state["plan_handle"], "deleted_handle": milestone_handle, "module_handle": module["handle"]}}

    def _move_milestone(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"milestone_handle", "target_module_handle", "index"})
        milestone_handle = _required(args, "milestone_handle")
        target_module_handle = _required(args, "target_module_handle")
        state, source_module, milestone = self._load_milestone(milestone_handle)
        _assert_editable_plan(state)
        target_module = _find_module_by_handle(state, target_module_handle)
        source_module["internal_milestones"] = [
            item for item in list(source_module.get("internal_milestones") or []) if _text(item.get("handle")) != milestone_handle
        ]
        target_items = list(target_module.get("internal_milestones") or [])
        index = _coerce_int(args.get("index"), default=len(target_items))
        index = max(0, min(len(target_items), index))
        target_items.insert(index, milestone)
        target_module["internal_milestones"] = target_items
        self._renumber_milestones(source_module)
        self._renumber_milestones(target_module)
        self._save_state(state)
        return {
            "text": f"Milestone moved: {milestone_handle} -> {target_module_handle}",
            "structured": {"plan_handle": state["plan_handle"], "milestone_handle": milestone_handle, "module_handle": target_module_handle},
        }

    def _update_acceptance_criterion(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(
            args,
            {"acceptance_handle", "criterion", "evidence_expectation", "linked_constraint_handles", "gate_check_refs", "negative_cases", "quantifier"},
        )
        state, module, milestone, ac = self._load_acceptance(_required(args, "acceptance_handle"))
        _assert_editable_plan(state)
        if "criterion" in args:
            ac["criterion"] = _required(args, "criterion")
        if "evidence_expectation" in args:
            ac["evidence_expectation"] = _required(args, "evidence_expectation")
        if "linked_constraint_handles" in args:
            ac["linked_constraint_handles"] = _known_handles(state, _string_list(args.get("linked_constraint_handles")), expected_prefix="constraint")
        if "gate_check_refs" in args:
            ac["gate_check_refs"] = _known_gate_check_refs(state, args.get("gate_check_refs"))
        if "negative_cases" in args:
            negative_cases = _string_list(args.get("negative_cases"))
            if _acceptance_requires_negative_cases(_text(ac.get("criterion")), _text(ac.get("evidence_expectation"))) and not negative_cases:
                raise ValueError("negative_cases must contain concrete examples for reject/error/boundary acceptance criteria")
            ac["negative_cases"] = negative_cases
        if "quantifier" in args:
            ac["quantifier"] = _text(args.get("quantifier"))
        _validate_acceptance_consistency(milestone)
        self._renumber_acceptance(milestone)
        self._save_state(state)
        return {
            "text": f"Acceptance criterion updated: {ac['handle']}",
            "structured": {"plan_handle": state["plan_handle"], "acceptance_handle": ac["handle"], "milestone_handle": milestone["handle"], "module_handle": module["handle"]},
        }

    def _delete_acceptance_criterion(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"acceptance_handle"})
        acceptance_handle = _required(args, "acceptance_handle")
        state, module, milestone, _ac = self._load_acceptance(acceptance_handle)
        _assert_editable_plan(state)
        milestone["acceptance"] = [item for item in list(milestone.get("acceptance") or []) if _text(item.get("handle")) != acceptance_handle]
        self._renumber_acceptance(milestone)
        self._save_state(state)
        return {
            "text": f"Acceptance criterion deleted: {acceptance_handle}",
            "structured": {"plan_handle": state["plan_handle"], "deleted_handle": acceptance_handle, "milestone_handle": milestone["handle"], "module_handle": module["handle"]},
        }

    def _replace_milestone_acceptance_criteria(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"milestone_handle", "criteria"})
        state, module, milestone = self._load_milestone(_required(args, "milestone_handle"))
        _assert_editable_plan(state)
        criteria = _acceptance_criteria_list(args.get("criteria"))
        if not criteria:
            raise ValueError("criteria must contain at least one acceptance criterion object or string")
        items: list[dict[str, Any]] = []
        for index, raw in enumerate(criteria, start=1):
            criterion = _required(raw, "criterion")
            evidence = _required(raw, "evidence_expectation")
            negative_cases = _string_list(raw.get("negative_cases"))
            if "negative_cases" not in raw:
                raise ValueError("negative_cases is required for each replacement criterion")
            if _acceptance_requires_negative_cases(criterion, evidence) and not negative_cases:
                raise ValueError("negative_cases must contain concrete examples for reject/error/boundary acceptance criteria")
            items.append(
                {
                    "handle": self._next_handle(state, "ac", "ac"),
                    "id": _public_id("AC", index),
                    "criterion": criterion,
                    "evidence_expectation": evidence,
                    "linked_constraint_handles": _known_handles(state, _string_list(raw.get("linked_constraint_handles")), expected_prefix="constraint"),
                    "gate_check_refs": _known_gate_check_refs(state, raw.get("gate_check_refs")),
                    "negative_cases": negative_cases,
                    "quantifier": _text(raw.get("quantifier")),
                }
            )
        milestone["acceptance"] = items
        _validate_acceptance_consistency(milestone)
        self._save_state(state)
        return {
            "text": f"Milestone acceptance criteria replaced: {milestone['handle']}",
            "structured": {"plan_handle": state["plan_handle"], "milestone_handle": milestone["handle"], "module_handle": module["handle"], "acceptance_handles": [item["handle"] for item in items]},
        }

    def _apply_revision_item(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"plan_handle", "item_id", "resolution", "replacement", "evidence"})
        state = self._load_state(_required(args, "plan_handle"))
        _assert_editable_plan(state)
        item = _revision_item_by_id(state, _required(args, "item_id"))
        evidence = _required(args, "evidence")
        resolution = _text(args.get("resolution") or "apply").lower()
        if resolution not in {"apply", "manual", "keep"}:
            raise ValueError("resolution must be apply, manual, or keep")
        if resolution in {"manual", "keep"}:
            _mark_revision_item_resolved(item, status=resolution, evidence=evidence)
            self._save_state(state)
            return {
                "text": f"Revision item {item.get('id')} marked {resolution}.",
                "structured": {"plan_handle": state["plan_handle"], "item_id": item.get("id"), "resolution": item.get("resolution")},
            }

        replacement = dict(args.get("replacement") or {}) if isinstance(args.get("replacement"), dict) else {}
        if bool(item.get("needs_replacement")) and not replacement:
            suggested = dict(item.get("suggested_args") or {})
            raise ValueError(
                f"{item.get('id')} requires replacement fields before apply; pass replacement to merge with suggested_args={json.dumps(suggested, ensure_ascii=False, sort_keys=True)}"
            )
        suggested_tool = _text(item.get("suggested_tool"))
        if suggested_tool not in _APPLY_REVISION_ALLOWED_TOOLS:
            raise ValueError(f"{item.get('id')} has no safe suggested_tool for automatic apply: {item.get('suggested_tool') or ''}")
        tool_args = dict(item.get("suggested_args") or {})
        tool_args.update(replacement)
        tool_schema = dict((PLAN_BUILDER_TOOL_SPECS.get(suggested_tool) or {}).get("parameters_schema") or {})
        tool_properties = dict(tool_schema.get("properties") or {})
        if "plan_handle" in tool_properties and not _text(tool_args.get("plan_handle")):
            tool_args["plan_handle"] = state["plan_handle"]
        result = self._execute(suggested_tool, tool_args)
        structured = dict(result.get("structured") or {})
        refreshed = self._load_state(_text(structured.get("plan_handle") or state["plan_handle"]))
        refreshed_item = _revision_item_by_id(refreshed, _text(item.get("id")))
        _mark_revision_item_resolved(
            refreshed_item,
            status="applied",
            evidence=evidence,
            tool=suggested_tool,
            applied_args=tool_args,
        )
        self._save_state(refreshed)
        return {
            "text": f"Revision item {item.get('id')} applied via {suggested_tool}: {result.get('text')}",
            "structured": {
                **structured,
                "plan_handle": refreshed["plan_handle"],
                "item_id": item.get("id"),
                "resolution": refreshed_item.get("resolution"),
                "applied_tool": suggested_tool,
            },
        }

    def _submit_for_review(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"plan_handle", "summary", "system_test_plan", "risks", "assumptions"})
        state = self._load_state(_required(args, "plan_handle"))
        _assert_editable_plan(state)
        _reject_revision_submit_overrides(state, args)
        stage = _plan_builder_stage(state, self.workspace)
        if _open_module(state):
            raise ValueError("close all modules before submitting the plan draft for review")
        revision_errors = _revision_checklist_submit_errors(state)
        if revision_errors:
            raise ValueError("plan revision checklist is not satisfied: " + "; ".join(revision_errors))
        if stage == "architecture_sketch":
            return self._submit_sketch_artifact(state, args)
        if stage == "module_detail":
            return self._submit_module_detail_artifact(state, args)
        artifact = self._compile_artifact(state, args)
        validation = dispatchable_plan_validation(artifact)
        plan_revision = _coerce_int(state.get("plan_revision"), default=_coerce_int(self.workspace.get("planner_plan_revision"), default=0))
        content = _final_plan_json(artifact, plan_revision=plan_revision, lifecycle="submitted")
        artifact_meta = _write_minion_artifact(
            self.workspace,
            {
                "relative_path": "plan.draft.json",
                "title": "Submitted plan draft",
                "role": "primary",
                "mime_type": "application/json",
                "overwrite": True,
                "content": content,
            },
        )
        _append_unique_artifact(self.produced_artifacts, artifact_meta)
        review_content = _plan_review_markdown(artifact, validation, plan_revision=plan_revision)
        review_artifact_meta = _write_minion_artifact(
            self.workspace,
            {
                "relative_path": "plan_review.md",
                "title": "Plan review",
                "role": "review",
                "mime_type": "text/markdown",
                "overwrite": True,
                "content": review_content,
            },
        )
        _append_unique_artifact(self.produced_artifacts, review_artifact_meta)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        state["lifecycle"] = "submitted"
        state["submitted_artifact"] = dict(artifact_meta)
        state["submitted_review_artifact"] = dict(review_artifact_meta)
        state["submitted_artifact_sha256"] = digest
        state["submitted_at_revision"] = plan_revision
        self._save_state(state)
        plan_draft_ref = {
            "ref_kind": "plan_draft",
            "path": str(artifact_meta.get("path") or ""),
            "sha256": digest,
            "plan_id": artifact["plan_id"],
            "task_id": artifact["task_id"],
            "plan_revision": plan_revision,
            "plan_handle": state["plan_handle"],
            "artifact_dir": str(self.workspace.get("artifact_dir") or ""),
            "relative_path": "plan.draft.json",
            "review_artifact_ref": dict(review_artifact_meta),
        }
        return {
            "text": "Plan draft submitted for review. The plan_acceptance gate can review this draft snapshot.",
            "structured": {
                "plan_handle": state["plan_handle"],
                "plan_draft_ref": plan_draft_ref,
                "plan_ref": plan_draft_ref,
                "artifact": artifact_meta,
                "review_artifact": review_artifact_meta,
                "plan_id": artifact["plan_id"],
                "task_id": artifact["task_id"],
                "plan_revision": plan_revision,
                "plan_validation": validation,
            },
        }

    def _submit_sketch_artifact(self, state: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        artifact = self._compile_sketch_artifact(state, args)
        validation = validate_plan_sketch_artifact(artifact)
        plan_revision = _coerce_int(state.get("plan_revision"), default=_coerce_int(self.workspace.get("planner_plan_revision"), default=0))
        content = _stage_artifact_json(artifact, plan_revision=plan_revision, lifecycle="submitted")
        artifact_meta = _write_minion_artifact(
            self.workspace,
            {
                "relative_path": "plan.sketch.json",
                "title": "Plan sketch",
                "role": "primary",
                "mime_type": "application/json",
                "overwrite": True,
                "content": content,
            },
        )
        _append_unique_artifact(self.produced_artifacts, artifact_meta)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        state["lifecycle"] = "submitted"
        state["submitted_artifact"] = dict(artifact_meta)
        state["submitted_artifact_sha256"] = digest
        state["submitted_at_revision"] = plan_revision
        self._save_state(state)
        sketch_ref = {
            "ref_kind": "plan_sketch",
            "path": str(artifact_meta.get("path") or ""),
            "sha256": digest,
            "plan_id": artifact["plan_id"],
            "sketch_id": artifact["sketch_id"],
            "task_id": artifact["task_id"],
            "plan_revision": plan_revision,
            "plan_handle": state["plan_handle"],
            "artifact_dir": str(self.workspace.get("artifact_dir") or ""),
            "relative_path": "plan.sketch.json",
        }
        return {
            "text": "Plan sketch submitted. Manager will validate the architecture skeleton before detail planning.",
            "structured": {
                "plan_handle": state["plan_handle"],
                "sketch_ref": sketch_ref,
                "artifact": artifact_meta,
                "plan_id": artifact["plan_id"],
                "task_id": artifact["task_id"],
                "plan_revision": plan_revision,
                "plan_validation": validation,
            },
        }

    def _submit_module_detail_artifact(self, state: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        artifact = self._compile_module_detail_artifact(state, args)
        validation = validate_module_detail_artifact(artifact)
        plan_revision = _coerce_int(state.get("plan_revision"), default=_coerce_int(self.workspace.get("planner_plan_revision"), default=0))
        module_id = _safe_id(artifact.get("module_id"), default_prefix="module")
        relative_path = f"module_detail.{module_id}.json"
        content = _stage_artifact_json(artifact, plan_revision=plan_revision, lifecycle="submitted")
        artifact_meta = _write_minion_artifact(
            self.workspace,
            {
                "relative_path": relative_path,
                "title": f"Module detail {module_id}",
                "role": "primary",
                "mime_type": "application/json",
                "overwrite": True,
                "content": content,
            },
        )
        _append_unique_artifact(self.produced_artifacts, artifact_meta)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        state["lifecycle"] = "submitted"
        state["submitted_artifact"] = dict(artifact_meta)
        state["submitted_artifact_sha256"] = digest
        state["submitted_at_revision"] = plan_revision
        self._save_state(state)
        detail_ref = {
            "ref_kind": "module_detail",
            "path": str(artifact_meta.get("path") or ""),
            "sha256": digest,
            "plan_id": artifact["plan_id"],
            "task_id": artifact["task_id"],
            "module_id": artifact["module_id"],
            "plan_revision": plan_revision,
            "plan_handle": state["plan_handle"],
            "artifact_dir": str(self.workspace.get("artifact_dir") or ""),
            "relative_path": relative_path,
        }
        return {
            "text": f"Module detail submitted for {artifact['module_id']}.",
            "structured": {
                "plan_handle": state["plan_handle"],
                "module_detail_ref": detail_ref,
                "artifact": artifact_meta,
                "plan_id": artifact["plan_id"],
                "task_id": artifact["task_id"],
                "module_id": artifact["module_id"],
                "plan_revision": plan_revision,
                "plan_validation": validation,
            },
        }

    def _finalize(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"plan_handle", "summary", "system_test_plan", "risks", "assumptions"})
        state = self._load_state(_required(args, "plan_handle"))
        _assert_open_plan(state)
        _reject_revision_submit_overrides(state, args)
        if _open_module(state):
            raise ValueError("close all modules before finalizing the plan")
        artifact = self._compile_artifact(state, args)
        validation = dispatchable_plan_validation(artifact)
        plan_revision = _coerce_int(state.get("plan_revision"), default=_coerce_int(self.workspace.get("planner_plan_revision"), default=0))
        content = _final_plan_json(artifact, plan_revision=plan_revision, lifecycle="finalized")
        artifact_meta = _write_minion_artifact(
            self.workspace,
            {
                "relative_path": "plan.json",
                "title": "Final plan",
                "role": "primary",
                "mime_type": "application/json",
                "overwrite": True,
                "content": content,
            },
        )
        _append_unique_artifact(self.produced_artifacts, artifact_meta)
        state["closed"] = True
        state["lifecycle"] = "finalized"
        state["final_artifact"] = artifact
        state["final_artifact_sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        state["final_artifact_path"] = artifact_meta.get("path")
        self._save_state(state)
        return {
            "text": "Plan finalized and written to plan.json. The existing plan_acceptance gate can now review it.",
            "structured": {
                "plan_handle": state["plan_handle"],
                "artifact": artifact_meta,
                "plan_id": artifact["plan_id"],
                "task_id": artifact["task_id"],
                "plan_validation": validation,
            },
        }

    def _compile_sketch_artifact(self, state: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        task_id = _text(state.get("task_id") or self.workspace.get("task_id"))
        if not task_id:
            raise ValueError("task_id is required in planner workspace before submitting sketch")
        modules = list(state.get("modules") or [])
        if not modules:
            raise ValueError("sketch must contain at least one module")
        if any(not bool(module.get("closed")) for module in modules):
            raise ValueError("all sketch modules must be closed before submitting")
        if not _string_list(state.get("languages")):
            raise ValueError("metadata.languages must contain at least one canonical implementation language id")
        self._validate_module_boundary_contracts(state)
        self._validate_module_interfaces(state)
        self._validate_design_decisions(state)
        self._validate_constraint_coverage(state)
        self._validate_gate_contract(state)
        metadata = {
            "languages": _normalize_language_ids(state.get("languages")),
            "source_refs": _string_list(state.get("source_refs")),
            "constraints": [_strip_handles(item) for item in state.get("constraints") or []],
            "design_decisions": [_strip_handles(item) for item in state.get("design_decisions") or []],
            "plan_builder": {
                "version": 1,
                "plan_handle": state["plan_handle"],
                "stage": "architecture_sketch",
                "lifecycle": _text(state.get("lifecycle") or "editing"),
            },
            "plan_revision": _coerce_int(state.get("plan_revision"), default=0),
        }
        workflow_next = _workflow_next_payload(state.get("workflow_next"))
        if workflow_next:
            metadata["workflow_next"] = workflow_next
        gate_contract = _compiled_gate_contract(state)
        if gate_contract:
            metadata["gate_contract"] = gate_contract
        source_acceptance_coverage = _gate_check_coverage_projection(state)
        if source_acceptance_coverage:
            metadata["source_acceptance_coverage"] = source_acceptance_coverage
        return {
            "type": "PlanSketchArtifact",
            "plan_id": _text(state.get("plan_id") or new_work_id("plan")),
            "sketch_id": _text(state.get("sketch_id") or f"{state.get('plan_id') or 'plan'}_sketch"),
            "task_id": task_id,
            "planning_depth": _text(state.get("planning_depth") or self.workspace.get("planning_depth") or "sketch_only"),
            "summary": _text(args.get("summary") or state.get("summary") or state.get("goal") or "Architecture sketch."),
            "modules": [self._sketch_module_payload(state, module) for module in modules],
            "cross_module_contracts": self._cross_module_contracts(state),
            "topology": self._compile_topology(state),
            "reference_map": _dict_list(state.get("reference_map")),
            "system_test_plan": _dict_list(args.get("system_test_plan")),
            "risks": _dict_list(args.get("risks")),
            "metadata": metadata,
        }

    def _sketch_module_payload(self, state: dict[str, Any], module: dict[str, Any]) -> dict[str, Any]:
        payload = self._module_payload(state, module)
        payload.pop("internal_milestones", None)
        metadata = dict(payload.get("metadata") or {})
        metadata["module_quality_criteria"] = _string_list(module.get("module_quality_criteria"))
        metadata["risk_surfaces"] = _string_list(module.get("risk_surfaces"))
        metadata["delivery_surfaces"] = _string_list(module.get("delivery_surfaces"))
        payload["metadata"] = {key: value for key, value in metadata.items() if value not in ("", [], {})}
        return payload

    def _compile_module_detail_artifact(self, state: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        task_id = _text(state.get("task_id") or self.workspace.get("task_id"))
        if not task_id:
            raise ValueError("task_id is required in planner workspace before submitting module detail")
        module = _bound_module_detail_module(state)
        if any(_open_milestone(item) for item in [module]):
            raise ValueError("all module detail milestones must be closed before submitting")
        if not list(module.get("internal_milestones") or []):
            raise ValueError("module detail must contain at least one milestone")
        for milestone in _dict_list(module.get("internal_milestones")):
            _validate_acceptance_consistency(milestone)
        payload = self._module_payload(state, module)
        return {
            "type": "ModuleDetailArtifact",
            "plan_id": _text(state.get("plan_id") or new_work_id("plan")),
            "task_id": task_id,
            "module_id": _text(module.get("module_id")),
            "summary": _text(args.get("summary") or module.get("responsibility") or "Module detail."),
            "sketch_ref": dict(state.get("sketch_ref") or {}),
            "module": payload,
            "milestones": list(payload.get("internal_milestones") or []),
            "test_strategy": dict(payload.get("test_plan") or {}),
            "positive_cases": _string_list(state.get("positive_cases")),
            "negative_cases": _string_list(state.get("negative_cases")),
            "evidence_expectations": _string_list(state.get("evidence_expectations")),
            "sketch_revision_request": dict(state.get("sketch_revision_request") or {}),
            "metadata": {
                "plan_builder": {
                    "version": 1,
                    "plan_handle": state["plan_handle"],
                    "stage": "module_detail",
                    "lifecycle": _text(state.get("lifecycle") or "editing"),
                },
                "plan_revision": _coerce_int(state.get("plan_revision"), default=0),
                "bound_module_id": _text(state.get("bound_module_id")),
            },
        }

    def _compile_artifact(self, state: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        task_id = _text(state.get("task_id") or self.workspace.get("task_id"))
        if not task_id:
            raise ValueError("task_id is required in planner workspace before finalizing")
        modules = list(state.get("modules") or [])
        if not modules:
            raise ValueError("plan must contain at least one module")
        if any(not bool(module.get("closed")) for module in modules):
            raise ValueError("all modules must be closed before finalizing")
        if any(_open_milestone(module) for module in modules):
            raise ValueError("all milestones must be closed before finalizing")
        for module in modules:
            for milestone in _dict_list(module.get("internal_milestones")):
                _validate_acceptance_consistency(milestone)
        kinds = [_text(module.get("kind")).lower() for module in modules]
        if kinds.count("prelude") > 1:
            raise ValueError("plan must contain at most one prelude module")
        if "module" not in kinds:
            raise ValueError("plan must contain at least one implementation module")
        if not _string_list(state.get("languages")):
            raise ValueError("metadata.languages must contain at least one canonical implementation language id")
        self._validate_module_boundary_contracts(state)
        self._validate_module_interfaces(state)
        self._validate_gate_evidence_handoff(state)
        self._validate_design_decisions(state)
        self._validate_constraint_coverage(state)
        self._validate_gate_contract(state)
        module_payloads = [self._module_payload(state, module) for module in modules]
        topology = self._compile_topology(state)
        is_revision = isinstance(state.get("source_plan_ref"), dict)
        assumptions = [] if is_revision else _string_list(args.get("assumptions"))
        risks = _dict_list(state.get("risks")) if is_revision else _dict_list(args.get("risks"))
        if assumptions:
            risks = [*risks, *({"kind": "assumption", "summary": item} for item in assumptions)]
        source_artifact = dict(state.get("_revision_source_artifact") or {}) if is_revision else {}
        source_metadata = dict(source_artifact.get("metadata") or {}) if isinstance(source_artifact.get("metadata"), dict) else {}
        metadata = deepcopy(source_metadata)
        metadata.update(
            {
                "languages": _normalize_language_ids(state.get("languages")),
                "source_refs": _string_list(state.get("source_refs")),
                "constraints": [_strip_handles(item) for item in state.get("constraints") or []],
                "design_decisions": [_strip_handles(item) for item in state.get("design_decisions") or []],
                "plan_revision": _coerce_int(state.get("plan_revision"), default=0),
            }
        )
        source_plan_builder = dict(source_metadata.get("plan_builder") or {}) if isinstance(source_metadata.get("plan_builder"), dict) else {}
        metadata["plan_builder"] = {
            **source_plan_builder,
            "version": 1,
            "plan_handle": state["plan_handle"],
            "lifecycle": _text(state.get("lifecycle") or "editing"),
        }
        workflow_next = _workflow_next_payload(state.get("workflow_next"))
        if workflow_next:
            metadata["workflow_next"] = workflow_next
        else:
            metadata.pop("workflow_next", None)
        gate_contract = _compiled_gate_contract(state)
        if gate_contract:
            metadata["gate_contract"] = gate_contract
        else:
            metadata.pop("gate_contract", None)
        source_acceptance_coverage = _gate_check_coverage_projection(state)
        coverage_unchanged = (
            is_revision
            and _text(state.get("_revision_gate_coverage_digest"))
            == _json_value_digest(source_acceptance_coverage)
        )
        if source_acceptance_coverage and not coverage_unchanged:
            metadata["source_acceptance_coverage"] = source_acceptance_coverage
        elif not source_acceptance_coverage:
            metadata.pop("source_acceptance_coverage", None)
        if isinstance(state.get("source_plan_ref"), dict):
            metadata["revision_of"] = dict(state.get("source_plan_ref") or {})
        orchestration = deepcopy(dict(state.get("orchestration") or {}))
        orchestration.setdefault("execution_shape", "fork_join_linear")
        orchestration["topology"] = topology
        orchestration.setdefault("coordination", "Pal manager dispatches closed module milestones through the validated plan topology.")
        orchestration.setdefault("checkpoint_policy", "Each coder milestone produces a structured checkpoint for review before the next step.")
        orchestration.setdefault("fallback_behavior", "If a gate fails, route reviewer findings into the repair/revision loop before continuing.")
        cross_module_contracts = [
            *self._cross_module_contracts(state),
            *[deepcopy(item) for item in _dict_list(state.get("_revision_cross_module_contracts"))],
        ]
        artifact = {
            "plan_id": _text(state.get("plan_id") or new_work_id("plan")),
            "task_id": task_id,
            "summary": _text(state.get("summary") if is_revision else args.get("summary") or state.get("summary") or state.get("goal") or "Dispatchable plan."),
            "modules": module_payloads,
            "cross_module_contracts": cross_module_contracts,
            "orchestration": orchestration,
            "system_test_plan": (
                _dict_list(state.get("system_test_plan"))
                if is_revision
                else _dict_list(args.get("system_test_plan"))
                or [{"level": "system", "evidence": "Run the full feature workflow or explain why dogfood is not possible."}]
            ),
            "risks": risks,
            "metadata": metadata,
        }
        if source_artifact:
            return _merge_revision_artifact(source_artifact, artifact)
        return artifact

    def _module_payload(self, state: dict[str, Any], module: dict[str, Any]) -> dict[str, Any]:
        metadata = {
            "module_kind": module.get("kind"),
            "scope_guard": module.get("scope_guard"),
            "constraint_refs": _public_refs(state, module.get("constraint_handles")),
            "decision_refs": _public_refs(state, module.get("decision_handles")),
            "gate_check_refs": _gate_check_public_refs(module.get("gate_check_refs")),
        }
        languages = _string_list(module.get("languages"))
        if languages:
            metadata["languages"] = languages
        module_quality_criteria = _string_list(module.get("module_quality_criteria"))
        if module_quality_criteria:
            metadata["module_quality_criteria"] = module_quality_criteria
        module_quality_criteria_details = _module_quality_criteria_details_payload(state, module)
        if module_quality_criteria_details:
            metadata["module_quality_criteria_details"] = module_quality_criteria_details
        risk_surfaces = _string_list(module.get("risk_surfaces"))
        if risk_surfaces:
            metadata["risk_surfaces"] = risk_surfaces
        delivery_surfaces = _string_list(module.get("delivery_surfaces"))
        if delivery_surfaces:
            metadata["delivery_surfaces"] = delivery_surfaces
        executor_profile = _text(module.get("executor_profile"))
        if executor_profile:
            metadata["executor_profile"] = executor_profile
        return {
            "module_id": _text(module.get("module_id")),
            "owned_area": _string_list(module.get("owned_area")),
            "responsibility": _text(module.get("responsibility")),
            "ownership": _string_list(module.get("ownership")),
            "lifecycle": _string_list(module.get("lifecycle")),
            "invariants": _string_list(module.get("invariants")),
            "provided_interfaces": [_public_interface_payload(item) for item in list(module.get("provided_interfaces") or [])],
            "consumed_interfaces": [_public_interface_payload(item) for item in list(module.get("consumed_interfaces") or [])],
            "internal_milestones": [self._milestone_payload(state, module, item, index=index) for index, item in enumerate(module.get("internal_milestones") or [])],
            "test_plan": self._module_test_plan(module),
            "metadata": {key: value for key, value in metadata.items() if value not in ("", [], {})},
        }

    def _milestone_payload(self, state: dict[str, Any], module: dict[str, Any], milestone: dict[str, Any], *, index: int) -> dict[str, Any]:
        acceptance_items = [dict(item) for item in list(milestone.get("acceptance") or [])]
        acceptance_checklist = [
            {
                "id": item.get("id") or _public_id("AC", item_index + 1),
                "criterion": _text(item.get("criterion")),
                "evidence_expectation": _text(item.get("evidence_expectation")),
                "linked_constraint_refs": _public_refs(state, item.get("linked_constraint_handles")),
                "gate_check_refs": _gate_check_public_refs(item.get("gate_check_refs")),
                "negative_cases": _string_list(item.get("negative_cases")),
                "quantifier": _text(item.get("quantifier")),
            }
            for item_index, item in enumerate(acceptance_items)
        ]
        metadata = {
            "scope_guard": _text(milestone.get("scope_guard")),
            "changed_area": _string_list(milestone.get("changed_area")),
            "constraint_refs": _public_refs(state, milestone.get("constraint_handles")),
            "decision_refs": _public_refs(state, milestone.get("decision_handles")),
            "gate_check_refs": _gate_check_public_refs(milestone.get("gate_check_refs")),
            "acceptance_checklist": acceptance_checklist,
            "implementation_checklist": self._implementation_checklist(module, milestone, acceptance_checklist),
        }
        languages = _string_list(milestone.get("languages"))
        if languages:
            metadata["languages"] = languages
        public_api_added = _text(milestone.get("public_api_added"))
        if public_api_added:
            metadata["public_api_added"] = public_api_added
        checkpoint_admission_evidence = _string_list(milestone.get("checkpoint_admission_evidence"))
        if checkpoint_admission_evidence:
            metadata["checkpoint_admission_evidence"] = checkpoint_admission_evidence
        test_plan = {"required": _string_list(milestone.get("tests_required"))}
        if checkpoint_admission_evidence:
            test_plan["checkpoint_admission"] = checkpoint_admission_evidence
        return {
            "milestone_id": _text(milestone.get("milestone_id") or f"{module['module_id']}_m{index + 1}"),
            "title": _text(milestone.get("title")),
            "task": _text(milestone.get("task")),
            "acceptance_criteria": [_text(item.get("criterion")) for item in acceptance_items],
            "skill_refs": _string_list(milestone.get("skill_refs")),
            "test_plan": test_plan,
            "metadata": {key: value for key, value in metadata.items() if value not in ("", [], {})},
        }

    def _module_test_plan(self, module: dict[str, Any]) -> dict[str, Any]:
        tests: list[str] = []
        for milestone in list(module.get("internal_milestones") or []):
            tests.extend(_string_list(milestone.get("tests_required")))
        module_quality_criteria = _string_list(module.get("module_quality_criteria"))
        result: dict[str, Any] = {"required": tests} if tests else {"module": ["Verify the module contract and failure behavior."]}
        if module_quality_criteria:
            result["module_quality"] = module_quality_criteria
        return result

    def _implementation_checklist(self, module: dict[str, Any], milestone: dict[str, Any], acceptance: list[dict[str, Any]]) -> list[dict[str, Any]]:
        module_id = _text(module.get("module_id"))
        milestone_id = _text(milestone.get("milestone_id"))
        first_ac = _text((acceptance[0] if acceptance else {}).get("id") or "AC-1")
        return [
            {
                "id": f"{milestone_id}.inspect",
                "kind": "inspect",
                "action": f"Inspect {module_id} owned_area and relevant contracts before editing.",
                "done_when": "The implementation path and boundary constraints are understood.",
            },
            {
                "id": f"{milestone_id}.implement",
                "kind": "implement",
                "action": _text(milestone.get("task")),
                "acceptance_ref": first_ac,
                "done_when": "The milestone behavior is implemented inside the module owned_area.",
            },
            {
                "id": f"{milestone_id}.test",
                "kind": "test",
                "action": "Run focused tests or checks that prove the milestone acceptance criteria.",
                "acceptance_ref": first_ac,
                "done_when": "Evidence covers each acceptance criterion, including declared negative cases when present.",
            },
            {
                "id": f"{milestone_id}.checkpoint",
                "kind": "checkpoint",
                "action": "Create the structured milestone checkpoint after verification passes.",
                "done_when": "The checkpoint contains only intended source/test/doc changes.",
            },
        ]

    def _compile_topology(self, state: dict[str, Any]) -> dict[str, Any]:
        modules = list(state.get("modules") or [])
        node_by_handle: dict[str, str] = {}
        for module in modules:
            kind = _text(module.get("kind")).lower()
            if kind == "prelude":
                node_by_handle[module["handle"]] = "prelude"
            elif kind == "join":
                node_by_handle[module["handle"]] = "join"
            else:
                node_by_handle[module["handle"]] = f"node_{module['module_id']}"
        nodes: list[dict[str, Any]] = []
        for module in modules:
            node_id = node_by_handle[module["handle"]]
            depends_on = [node_by_handle[handle] for handle in list(module.get("depends_on_module_handles") or [])]
            node = {
                "node_id": node_id,
                "kind": _text(module.get("kind")).lower(),
                "module_id": _text(module.get("module_id")),
                "depends_on": depends_on,
            }
            executor_profile = _text(module.get("executor_profile"))
            if executor_profile:
                node["executor_profile"] = executor_profile
            nodes.append(node)
        order = _topological_order(nodes)
        return {
            "nodes": [next(node for node in nodes if node["node_id"] == node_id) for node_id in order],
            "order": order,
        }

    def _cross_module_contracts(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        return _cross_module_contracts_from_state(state)

    def _validate_constraint_coverage(self, state: dict[str, Any]) -> None:
        referenced: set[str] = set()
        for decision in list(state.get("design_decisions") or []):
            referenced.update(_string_list(decision.get("linked_constraint_handles")))
        for module in list(state.get("modules") or []):
            referenced.update(_string_list(module.get("constraint_handles")))
            for milestone in list(module.get("internal_milestones") or []):
                referenced.update(_string_list(milestone.get("constraint_handles")))
                for ac in list(milestone.get("acceptance") or []):
                    referenced.update(_string_list(ac.get("linked_constraint_handles")))
        uncovered: list[str] = []
        for item in list(state.get("constraints") or []):
            if item.get("global_only"):
                continue
            if _text(item.get("strength")) in {"hard_contract", "chosen_contract"} and _text(item.get("handle")) not in referenced:
                uncovered.append(f"{item.get('id')}: {item.get('statement')}")
        if uncovered:
            raise ValueError("hard/chosen constraints must be linked to a decision, module, milestone, or acceptance criterion: " + "; ".join(uncovered))

    def _validate_gate_contract(self, state: dict[str, Any]) -> None:
        checks = _gate_checks(state)
        failures = _gate_check_reference_errors(state)
        if not checks:
            if failures:
                raise ValueError("gate contract validation failed: " + "; ".join(failures))
            return
        coverage = _gate_check_coverage(state)
        for check in checks:
            index = int(check.get("index") or 0)
            ref = f"gate:{index}"
            priority = _gate_priority(check.get("priority"))
            kind = _gate_check_kind(check.get("kind"))
            if priority == "hard" and kind in {"semantic", "hybrid"} and ref not in coverage:
                failures.append(
                    f"{ref} is not covered by any constraint, design decision, module, milestone, or acceptance criterion: {check.get('claim')}"
                )
            if kind in {"mechanical", "hybrid"}:
                result = _evaluate_mechanical_gate_check(state, check)
                if result:
                    failures.append(f"{ref} mechanical check failed: {result}")
        if failures:
            raise ValueError("gate contract validation failed: " + "; ".join(failures))

    def _validate_design_decisions(self, state: dict[str, Any]) -> None:
        for item in list(state.get("design_decisions") or []):
            if _text(item.get("strength")) in {"hard_contract", "chosen_contract"} and not _text(item.get("rationale")):
                raise ValueError(f"hard/chosen design decision requires rationale: {item.get('id')}")

    def _validate_module_interfaces(self, state: dict[str, Any]) -> None:
        modules = list(state.get("modules") or [])
        by_handle = {_text(module.get("handle")): module for module in modules}
        dependents: dict[str, list[dict[str, Any]]] = {_text(module.get("handle")): [] for module in modules}
        for module in modules:
            for dependency in _string_list(module.get("depends_on_module_handles")):
                if dependency in dependents:
                    dependents[dependency].append(module)
        missing: list[str] = []
        for module in modules:
            handle = _text(module.get("handle"))
            kind = _text(module.get("kind"))
            non_prelude_dependencies = [
                dependency
                for dependency in _string_list(module.get("depends_on_module_handles"))
                if _text((by_handle.get(dependency) or {}).get("kind")) != "prelude"
            ]
            if non_prelude_dependencies and not list(module.get("consumed_interfaces") or []):
                missing.append(f"{module.get('module_id')} must declare consumed_interfaces for non-prelude dependencies")
            if [item for item in dependents.get(handle, []) if _text(item.get("kind")) != "join"] and not list(module.get("provided_interfaces") or []):
                missing.append(f"{module.get('module_id')} must declare provided_interfaces for downstream implementation modules")
            if kind == "module" and [item for item in dependents.get(handle, []) if _text(item.get("kind")) == "join"] and not list(module.get("provided_interfaces") or []):
                missing.append(f"{module.get('module_id')} must declare provided_interfaces consumed by the join module")
        if missing:
            raise ValueError("module interface contracts are required for module dependencies: " + "; ".join(missing))

    def _validate_module_boundary_contracts(self, state: dict[str, Any]) -> None:
        missing: list[str] = []
        for module in list(state.get("modules") or []):
            module_id = _text(module.get("module_id") or module.get("handle"))
            if not _string_list(module.get("ownership")):
                missing.append(f"{module_id} must declare ownership bullets")
            if not _string_list(module.get("lifecycle")):
                missing.append(f"{module_id} must declare lifecycle bullets")
            if not _string_list(module.get("invariants")):
                missing.append(f"{module_id} must declare invariant bullets")
        if missing:
            raise ValueError("module boundary contracts are incomplete: " + "; ".join(missing))

    def _validate_gate_evidence_handoff(self, state: dict[str, Any]) -> None:
        missing: list[str] = []
        for module in list(state.get("modules") or []):
            module_id = _text(module.get("module_id") or module.get("handle"))
            if _text(module.get("kind")).lower() == "module" and not _string_list(module.get("module_quality_criteria")):
                missing.append(f"{module_id} must declare module_quality_criteria for module-quality review")
            for milestone in _dict_list(module.get("internal_milestones")):
                milestone_id = _text(milestone.get("milestone_id") or milestone.get("handle"))
                if not _string_list(milestone.get("checkpoint_admission_evidence")):
                    missing.append(f"{module_id}.{milestone_id} must declare checkpoint_admission_evidence")
        if missing:
            raise ValueError("gate evidence handoff is incomplete: " + "; ".join(missing))

    def _load_constraint(self, constraint_handle: str) -> tuple[dict[str, Any], dict[str, Any]]:
        state = self._load_state_for_handle(constraint_handle)
        for item in list(state.get("constraints") or []):
            if _text(item.get("handle")) == constraint_handle:
                return state, item
        raise ValueError(f"unknown constraint_handle: {constraint_handle}")

    def _load_design_decision(self, decision_handle: str) -> tuple[dict[str, Any], dict[str, Any]]:
        state = self._load_state_for_handle(decision_handle)
        for item in list(state.get("design_decisions") or []):
            if _text(item.get("handle")) == decision_handle:
                return state, item
        raise ValueError(f"unknown decision_handle: {decision_handle}")

    def _load_module(self, module_handle: str) -> tuple[dict[str, Any], dict[str, Any]]:
        state = self._load_state_for_handle(module_handle)
        module = _find_module_by_handle(state, module_handle)
        return state, module

    def _load_module_from_args(self, args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        module_handle = _text(args.get("module_handle"))
        if module_handle:
            return self._load_module(module_handle)
        module_name = _text(args.get("module_name"))
        if not module_name:
            raise ValueError("module_handle or module_name is required")
        module_id = _explicit_module_id(module_name)
        plan_handle = _text(args.get("plan_handle"))
        if plan_handle:
            state = self._load_state(plan_handle)
            return state, _find_module_by_id(state, module_id)
        matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for state in self._iter_states():
            for module in list(state.get("modules") or []):
                if _text(module.get("module_id")) == module_id:
                    matches.append((state, module))
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"unknown module_name: {module_id}")
        raise ValueError(f"module_name is ambiguous across drafts: {module_id}; pass plan_handle")

    def _load_milestone(self, milestone_handle: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        state = self._load_state_for_handle(milestone_handle)
        for module in list(state.get("modules") or []):
            for milestone in list(module.get("internal_milestones") or []):
                if _text(milestone.get("handle")) == milestone_handle:
                    return state, module, milestone
        raise ValueError(f"unknown milestone_handle: {milestone_handle}")

    def _load_acceptance(self, acceptance_handle: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        state = self._load_state_for_handle(acceptance_handle)
        for module in list(state.get("modules") or []):
            for milestone in list(module.get("internal_milestones") or []):
                for ac in list(milestone.get("acceptance") or []):
                    if _text(ac.get("handle")) == acceptance_handle:
                        return state, module, milestone, ac
        raise ValueError(f"unknown acceptance_handle: {acceptance_handle}")

    def _state_from_ref_or_handle(self, args: dict[str, Any], *, fallback_handle: str = "") -> dict[str, Any]:
        plan_handle = _text(args.get("plan_handle"))
        if plan_handle:
            try:
                return self._load_state(plan_handle)
            except ValueError as exc:
                plan_ref = self._workspace_plan_ref_for_handle(plan_handle)
                if plan_ref:
                    return self._state_from_plan_ref(plan_ref)
                raise exc
        plan_ref = args.get("plan_ref")
        if isinstance(plan_ref, dict):
            plan_ref = self._workspace_plan_ref_for_requested_ref(plan_ref) or plan_ref
            try:
                return self._state_from_plan_ref(plan_ref)
            except ValueError as exc:
                current = self._single_editing_revision_state()
                if current and "path is required" in str(exc):
                    return current
                raise
        default_ref = self._workspace_default_plan_ref()
        if default_ref:
            return self._state_from_plan_ref(default_ref)
        if fallback_handle:
            return self._load_state_for_handle(fallback_handle)
        raise ValueError("plan_handle or plan_ref is required")

    def _state_from_plan_ref(self, plan_ref: dict[str, Any]) -> dict[str, Any]:
        ref_handle = _text(plan_ref.get("plan_handle"))
        if ref_handle:
            try:
                return self._load_state(ref_handle)
            except ValueError:
                pass
        payload, _digest, normalized_ref = self._load_plan_payload(plan_ref)
        metadata = dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {}
        plan_builder = dict(metadata.get("plan_builder") or {}) if isinstance(metadata.get("plan_builder"), dict) else {}
        loaded_handle = _text(plan_builder.get("plan_handle") or normalized_ref.get("plan_handle"))
        state = _state_from_plan_payload(
            payload,
            plan_handle=loaded_handle or "",
            source_plan_ref=normalized_ref,
            plan_revision=plan_revision_from_payload(payload, normalized_ref),
        )
        state["_source_plan_payload"] = payload
        state["lifecycle"] = "submitted"
        return state

    def _workspace_plan_ref_for_handle(self, plan_handle: str) -> dict[str, Any]:
        requested = {"plan_handle": _text(plan_handle)}
        for key in ("review_target_plan_ref", "source_plan_ref"):
            candidate = self.workspace.get(key)
            if isinstance(candidate, dict) and _plan_ref_is_compatible(requested, candidate):
                return _merge_plan_ref(candidate, requested)
        return {}

    def _workspace_plan_ref_for_requested_ref(self, requested: dict[str, Any]) -> dict[str, Any]:
        for key in ("review_target_plan_ref", "source_plan_ref"):
            candidate = self.workspace.get(key)
            if isinstance(candidate, dict) and _plan_ref_is_compatible(requested, candidate):
                return _merge_plan_ref(candidate, requested)
        return {}

    def _workspace_default_plan_ref(self) -> dict[str, Any]:
        for key in ("review_target_plan_ref", "source_plan_ref"):
            candidate = self.workspace.get(key)
            if isinstance(candidate, dict):
                return dict(candidate)
        return {}

    def _load_plan_payload(self, plan_ref: dict[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any]]:
        runtime_root = _text(self.workspace.get("runtime_root"))
        if not runtime_root:
            raise ValueError("workspace.runtime_root is required to read plan_ref")
        ref = coerce_plan_ref(plan_ref)
        path = resolve_plan_ref_path(ref, runtime_root=Path(runtime_root))
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        expected = _text(ref.get("sha256"))
        if expected and expected != digest:
            raise ValueError(f"plan_ref sha256 mismatch for {path}")
        payload = json.loads(content.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("plan_ref JSON must be an object")
        artifact = validate_final_plan_artifact(payload)
        normalized_ref = {
            **dict(ref),
            "path": str(path),
            "sha256": digest,
            "plan_id": artifact.plan_id,
            "task_id": artifact.task_id,
            "plan_revision": plan_revision_from_payload(payload, ref),
        }
        metadata = dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {}
        plan_builder = dict(metadata.get("plan_builder") or {}) if isinstance(metadata.get("plan_builder"), dict) else {}
        if _text(plan_builder.get("plan_handle")):
            normalized_ref.setdefault("plan_handle", _text(plan_builder.get("plan_handle")))
        return payload, digest, normalized_ref

    def _renumber_milestones(self, module: dict[str, Any]) -> None:
        module_id = _text(module.get("module_id"))
        for index, milestone in enumerate(list(module.get("internal_milestones") or []), start=1):
            milestone["milestone_id"] = f"{module_id}_m{index}" if module_id else _text(milestone.get("milestone_id"))
            self._renumber_acceptance(milestone)

    def _renumber_acceptance(self, milestone: dict[str, Any]) -> None:
        for index, ac in enumerate(list(milestone.get("acceptance") or []), start=1):
            ac["id"] = _public_id("AC", index)

    def _load_state_for_handle(self, handle: str) -> dict[str, Any]:
        for state in self._iter_states():
            if _text(state.get("plan_handle")) == handle:
                return state
            if any(_text(item.get("ref")) == handle for item in _gate_checks(state)):
                return state
            if any(_text(item.get("handle")) == handle for item in list(state.get("constraints") or [])):
                return state
            if any(_text(item.get("handle")) == handle for item in list(state.get("design_decisions") or [])):
                return state
            if any(_text(module.get("handle")) == handle for module in list(state.get("modules") or [])):
                return state
            for module in list(state.get("modules") or []):
                if any(_text(item.get("handle")) == handle for item in list(module.get("provided_interfaces") or [])):
                    return state
                if any(_text(item.get("handle")) == handle for item in list(module.get("consumed_interfaces") or [])):
                    return state
                if any(_text(milestone.get("handle")) == handle for milestone in list(module.get("internal_milestones") or [])):
                    return state
                for milestone in list(module.get("internal_milestones") or []):
                    if any(_text(ac.get("handle")) == handle for ac in list(milestone.get("acceptance") or [])):
                        return state
        raise ValueError(f"unknown plan builder handle: {handle}")

    def _single_editing_revision_state(self) -> dict[str, Any]:
        candidates = [
            state
            for state in self._iter_states()
            if _text(state.get("lifecycle")).lower() == "editing"
            and isinstance(state.get("source_plan_ref"), dict)
        ]
        return candidates[0] if len(candidates) == 1 else {}

    def _load_state(self, plan_handle: str) -> dict[str, Any]:
        path = self._state_path(plan_handle)
        if not path.exists():
            raise ValueError(f"unknown plan_handle: {plan_handle}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_state(self, state: dict[str, Any]) -> None:
        path = self._state_path(_text(state.get("plan_handle")))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        self.workspace["plan_builder_source_acceptance_coverage"] = _gate_check_coverage_projection(state)

    def _iter_states(self) -> list[dict[str, Any]]:
        root = self._state_root()
        if not root.exists():
            return []
        states: list[dict[str, Any]] = []
        for path in sorted(root.glob("plan_*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(payload, dict):
                states.append(payload)
        return states

    def _state_root(self) -> Path:
        artifact_dir = _text(self.workspace.get("artifact_dir"))
        if not artifact_dir:
            raise ValueError("workspace.artifact_dir is required for plan builder")
        return Path(artifact_dir).expanduser().resolve() / ".plan_builder"

    def _state_path(self, plan_handle: str) -> Path:
        handle = _text(plan_handle)
        if not re.fullmatch(r"plan_[A-Za-z0-9_]+", handle):
            raise ValueError("invalid plan_handle")
        return self._state_root() / f"{handle}.json"

    def _next_handle(self, state: dict[str, Any], key: str, prefix: str) -> str:
        counters = dict(state.get("handle_counters") or {})
        counters[key] = int(counters.get(key) or 0) + 1
        state["handle_counters"] = counters
        return f"{prefix}_{state['plan_handle']}_{counters[key]}"


def plan_builder_tool_result(
    call: CanonicalToolCall,
    workspace: dict[str, Any],
    produced_artifacts: list[dict[str, Any]],
) -> CanonicalToolResult:
    return PlanBuilderRuntime(workspace=workspace, produced_artifacts=produced_artifacts).execute(call)


def initialize_plan_builder_stage_draft(workspace: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    stage_meta = dict((metadata or {}).get("staged_planning") or {})
    stage = _text(stage_meta.get("stage") or workspace.get("plan_builder_stage"))
    if stage not in {"architecture_sketch", "module_detail"}:
        return {}
    runtime = PlanBuilderRuntime(workspace=dict(workspace), produced_artifacts=[])
    task_id = _text(metadata.get("task_id") or workspace.get("task_id") or stage_meta.get("task_id"))
    plan_id = _safe_id(stage_meta.get("plan_id") or metadata.get("plan_id") or f"plan_{task_id or 'staged'}", default_prefix="plan")
    planning_depth = _text(stage_meta.get("planning_depth") or metadata.get("planning_depth") or workspace.get("planning_depth") or "sketch_only")
    if stage == "architecture_sketch":
        plan_handle = _text(stage_meta.get("plan_handle")) or (plan_id if plan_id.startswith("plan_") else f"plan_{plan_id}")
        state_path = runtime._state_path(plan_handle)
        if state_path.exists():
            return {"plan_handle": plan_handle, "plan_id": plan_id, "planning_depth": planning_depth, "stage": stage}
        gate_contract = _gate_contract_from_workspace(workspace)
        state = {
            "plan_handle": plan_handle,
            "plan_id": plan_id,
            "sketch_id": f"{plan_id}_sketch",
            "task_id": task_id,
            "goal": _text(metadata.get("planning_goal") or metadata.get("goal") or stage_meta.get("goal") or workspace.get("goal")),
            "summary": _text(metadata.get("task_title") or stage_meta.get("summary") or metadata.get("goal") or "Architecture sketch"),
            "languages": _normalize_language_ids(workspace.get("languages") or [workspace.get("primary_language")]),
            "source_refs": _string_list(stage_meta.get("source_refs")),
            "workflow_next": _workflow_next_payload(stage_meta.get("workflow_next")),
            "gate_contract": gate_contract,
            "locked_gate_check_refs": [f"gate:{int(check.get('index') or 0)}" for check in _gate_checks({"gate_contract": gate_contract})],
            "constraints": [],
            "design_decisions": [],
            "modules": [],
            "closed": False,
            "lifecycle": "editing",
            "plan_revision": _coerce_int(stage_meta.get("plan_revision"), default=0),
            "planning_depth": planning_depth,
            "plan_builder_stage": "architecture_sketch",
            "handle_counters": {"constraint": 0, "decision": 0, "module": 0, "milestone": 0, "ac": 0},
        }
        runtime._save_state(state)
        return {"plan_handle": plan_handle, "plan_id": plan_id, "planning_depth": planning_depth, "stage": stage}

    sketch = _load_stage_artifact(stage_meta.get("sketch_artifact") or stage_meta.get("sketch_ref") or workspace.get("sketch_ref"))
    validate_plan_sketch_artifact(sketch)
    module_id = _explicit_module_id(stage_meta.get("module_id") or workspace.get("plan_builder_module_id") or workspace.get("bound_module_id"))
    plan_handle = _text(stage_meta.get("plan_handle")) or f"{plan_id}_{_safe_id(module_id, default_prefix='module')}"
    if not plan_handle.startswith("plan_"):
        plan_handle = f"plan_{plan_handle}"
    state_path = runtime._state_path(plan_handle)
    if state_path.exists():
        return {"plan_handle": plan_handle, "plan_id": plan_id, "planning_depth": planning_depth, "stage": stage, "module_id": module_id}
    state = _state_from_sketch_payload(
        sketch,
        plan_handle=plan_handle,
        bound_module_id=module_id,
        sketch_ref=dict(stage_meta.get("sketch_ref") or stage_meta.get("sketch_artifact") or {}),
    )
    state["plan_builder_stage"] = "module_detail"
    state["planning_depth"] = planning_depth
    state["plan_revision"] = _coerce_int(stage_meta.get("plan_revision"), default=_coerce_int(state.get("plan_revision"), default=0))
    _bound_module_detail_module(state)
    runtime._save_state(state)
    return {"plan_handle": plan_handle, "plan_id": state.get("plan_id"), "planning_depth": planning_depth, "stage": stage, "module_id": module_id}


def validate_plan_sketch_artifact(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload or {})
    raw_type = _text(data.get("type") or data.get("output_type"))
    if raw_type and raw_type != "PlanSketchArtifact":
        raise ValueError(f"expected PlanSketchArtifact, got {raw_type}")
    errors: list[str] = []
    modules = _dict_list(data.get("modules"))
    if not _text(data.get("task_id")):
        errors.append("task_id is required")
    if not modules:
        errors.append("modules is required")
    module_ids: set[str] = {
        _text(module.get("module_id"))
        for module in modules
        if _text(module.get("module_id"))
    }
    duplicate_ids: set[str] = set()
    seen_ids: set[str] = set()
    for module in modules:
        module_id = _text(module.get("module_id"))
        if not module_id:
            continue
        if module_id in seen_ids:
            duplicate_ids.add(module_id)
        seen_ids.add(module_id)
    owned_area_owner: dict[str, str] = {}
    for index, module in enumerate(modules):
        module_id = _text(module.get("module_id"))
        if not module_id:
            errors.append(f"modules[{index}].module_id is required")
            module_id = f"module_{index}"
        if module_id in duplicate_ids:
            errors.append(f"modules[{index}].module_id is duplicated: {module_id}")
        for key in ("owned_area", "responsibility", "ownership", "lifecycle", "invariants"):
            value = module.get(key)
            if key == "responsibility":
                if not _text(value):
                    errors.append(f"modules[{index}].{key} is required")
            elif not _string_list(value):
                errors.append(f"modules[{index}].{key} is required")
        try:
            module_owned_areas = normalize_plan_write_areas(
                module.get("owned_area"),
                field_name=f"modules[{index}].owned_area",
            )
        except ValueError as exc:
            errors.append(str(exc))
            module_owned_areas = []
        for raw_area in module_owned_areas:
            normalized = _normalized_owned_area_key(raw_area)
            owner = owned_area_owner.get(normalized)
            if owner and owner != module_id:
                errors.append(f"modules[{index}].owned_area duplicates {owner}: {raw_area}")
            else:
                owned_area_owner[normalized] = module_id
        for interface in [*_dict_list(module.get("provided_interfaces")), *_dict_list(module.get("consumed_interfaces"))]:
            for key in ("name", "shape", "lifecycle", "ownership", "error_behavior", "compatibility"):
                if not _text(interface.get(key)):
                    errors.append(f"module {module_id} interface {interface.get('name') or '-'} missing {key}")
        for interface in _dict_list(module.get("consumed_interfaces")):
            producer = _text(interface.get("producer"))
            if producer and producer not in module_ids and producer.lower() not in {"external", "reference", "reference_only", "external_reference"}:
                errors.append(f"module {module_id} consumed interface {interface.get('name') or '-'} has unknown producer {producer}")
    topology_validation: dict[str, Any] = {}
    try:
        topology_validation = work_start_topology_validation(
            {"execution_shape": "fork_join_linear", "topology": dict(data.get("topology") or {})},
            known_module_ids=module_ids,
        )
    except ValueError as exc:
        errors.append(str(exc))
    gate_contract = _compiled_stage_gate_contract(data)
    coverage = _dict_list(dict(data.get("metadata") or {}).get("source_acceptance_coverage"))
    covered_refs = {
        _text(item.get("ref"))
        for item in coverage
        if _dict_list(item.get("evidence"))
    }
    for check in _dict_list(gate_contract.get("checks")):
        ref = _text(check.get("ref") or f"gate:{int(check.get('index') or 0)}")
        if _gate_priority(check.get("priority")) == "hard" and _gate_check_kind(check.get("kind")) in {"semantic", "hybrid"} and ref not in covered_refs:
            errors.append(f"{ref} is not covered by sketch evidence: {check.get('claim')}")
    if errors:
        raise ValueError("invalid PlanSketchArtifact: " + "; ".join(errors))
    return {
        "status": "valid",
        "artifact_type": "PlanSketchArtifact",
        "plan_id": _text(data.get("plan_id")),
        "task_id": _text(data.get("task_id")),
        "module_count": len(modules),
        "planning_depth": _text(data.get("planning_depth") or "sketch_only"),
        "node_order": list(topology_validation.get("node_order") or []),
        "module_order": list(topology_validation.get("module_order") or []),
    }


def validate_module_detail_artifact(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload or {})
    raw_type = _text(data.get("type") or data.get("output_type"))
    if raw_type and raw_type != "ModuleDetailArtifact":
        raise ValueError(f"expected ModuleDetailArtifact, got {raw_type}")
    errors: list[str] = []
    module_id = _text(data.get("module_id"))
    module = dict(data.get("module") or {})
    if not module_id:
        errors.append("module_id is required")
    if _text(module.get("module_id")) and _text(module.get("module_id")) != module_id:
        errors.append("module.module_id must match module_id")
    try:
        module_owned_areas = normalize_plan_write_areas(
            module.get("owned_area"),
            field_name="module.owned_area",
        )
    except ValueError as exc:
        errors.append(str(exc))
        module_owned_areas = []
    if not module_owned_areas:
        errors.append("module.owned_area is required")
    module_milestones = _dict_list(module.get("internal_milestones"))
    milestones = module_milestones or _dict_list(data.get("milestones"))
    if not module_milestones:
        errors.append("module.internal_milestones is required")
    if not milestones:
        errors.append("milestones is required")
    has_negative = False
    has_evidence = False
    for index, milestone in enumerate(milestones):
        if not _text(milestone.get("task") or milestone.get("title")):
            errors.append(f"milestones[{index}].task is required")
        if not _string_list(milestone.get("acceptance_criteria")):
            errors.append(f"milestones[{index}].acceptance_criteria is required")
        metadata = dict(milestone.get("metadata") or {})
        changed_area_field = f"milestones[{index}].metadata.changed_area"
        try:
            changed_areas = normalize_plan_write_areas(
                metadata.get("changed_area"),
                field_name=changed_area_field,
            )
        except ValueError as exc:
            errors.append(str(exc))
            changed_areas = []
        for changed_area in changed_areas:
            if module_owned_areas and not any(
                plan_write_area_covers(owned_area, changed_area)
                for owned_area in module_owned_areas
            ):
                errors.append(f"{changed_area_field} is outside module.owned_area: {changed_area}")
        checklist = _dict_list(metadata.get("acceptance_checklist"))
        for item in checklist:
            if _string_list(item.get("negative_cases")):
                has_negative = True
            if _text(item.get("evidence_expectation")):
                has_evidence = True
        if _string_list(metadata.get("checkpoint_admission_evidence")):
            has_evidence = True
    for interface in [*_dict_list(module.get("provided_interfaces")), *_dict_list(module.get("consumed_interfaces"))]:
        for key in ("name", "shape", "lifecycle", "ownership", "error_behavior", "compatibility"):
            if not _text(interface.get(key)):
                errors.append(f"interface {interface.get('name') or '-'} missing {key}")
    if not has_negative and not _string_list(data.get("negative_cases")):
        errors.append("negative_cases or milestone acceptance negative_cases are required")
    if not has_evidence and not _string_list(data.get("evidence_expectations")):
        errors.append("evidence expectations are required")
    if errors:
        raise ValueError("invalid ModuleDetailArtifact: " + "; ".join(errors))
    return {
        "status": "valid",
        "artifact_type": "ModuleDetailArtifact",
        "plan_id": _text(data.get("plan_id")),
        "task_id": _text(data.get("task_id")),
        "module_id": module_id,
        "milestone_count": len(milestones),
    }


def compile_final_plan_from_staged_artifacts(
    sketch_artifact: dict[str, Any],
    module_detail_artifacts: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    *,
    workflow_next: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sketch = dict(sketch_artifact or {})
    validate_plan_sketch_artifact(sketch)
    detail_by_module: dict[str, dict[str, Any]] = {}
    for detail in [dict(item) for item in list(module_detail_artifacts or []) if isinstance(item, dict)]:
        validate_module_detail_artifact(detail)
        detail_by_module[_text(detail.get("module_id"))] = detail
    modules: list[dict[str, Any]] = []
    for sketch_module in _dict_list(sketch.get("modules")):
        module_id = _text(sketch_module.get("module_id"))
        detail = detail_by_module.get(module_id)
        if detail:
            module = dict(detail.get("module") or {})
            module.setdefault("module_id", module_id)
            module.setdefault("owned_area", _string_list(sketch_module.get("owned_area")))
            module.setdefault("responsibility", _text(sketch_module.get("responsibility")))
            module.setdefault("ownership", _string_list(sketch_module.get("ownership")))
            module.setdefault("lifecycle", _string_list(sketch_module.get("lifecycle")))
            module.setdefault("invariants", _string_list(sketch_module.get("invariants")))
            modules.append(module)
            continue
        modules.append(_synthetic_module_from_sketch(sketch_module))
    metadata = dict(sketch.get("metadata") or {})
    metadata["plan_revision"] = _coerce_int(metadata.get("plan_revision"), default=0)
    metadata["staged_planning"] = {
        "source_artifact": "PlanSketchArtifact",
        "planning_depth": _text(sketch.get("planning_depth") or "sketch_only"),
        "module_detail_count": len(detail_by_module),
    }
    next_payload = _workflow_next_payload(workflow_next or metadata.get("workflow_next") or {"profile": "software_engineering.coder", "artifact_type": "implementation_plan", "adapter": "accepted_plan"})
    if next_payload:
        metadata["workflow_next"] = next_payload
    final_plan = {
        "type": "FinalPlanArtifact",
        "plan_id": _text(sketch.get("plan_id") or new_work_id("plan")),
        "task_id": _text(sketch.get("task_id")),
        "summary": _text(sketch.get("summary") or "Compiled implementation plan."),
        "modules": modules,
        "cross_module_contracts": _dict_list(sketch.get("cross_module_contracts")),
        "orchestration": {
            "execution_shape": "fork_join_linear",
            "topology": dict(sketch.get("topology") or {}),
            "coordination": "Pal manager dispatches modules from the staged architect plan.",
            "checkpoint_policy": "Each coder milestone produces a checkpoint before downstream work continues.",
            "fallback_behavior": "Gate failures route to repair or plan revision before continuing.",
        },
        "system_test_plan": _dict_list(sketch.get("system_test_plan")) or [{"level": "system", "evidence": "Run or explain the smallest faithful end-to-end verification path."}],
        "risks": _dict_list(sketch.get("risks")),
        "metadata": metadata,
    }
    validate_dispatchable_plan_artifact(final_plan)
    return final_plan


def _synthetic_module_from_sketch(sketch_module: dict[str, Any]) -> dict[str, Any]:
    module = dict(sketch_module or {})
    module_id = _text(module.get("module_id")) or "implementation"
    criteria = _string_list(dict(module.get("metadata") or {}).get("module_quality_criteria")) or [
        f"{module_id} satisfies its module responsibility and declared interfaces.",
    ]
    acceptance_checklist = [
        {
            "id": _public_id("AC", index + 1),
            "criterion": criterion,
            "evidence_expectation": "Focused implementation evidence and reviewer checks prove this module contract.",
            "negative_cases": ["Invalid, empty, missing, or incompatible inputs are rejected or handled according to the module contract."],
        }
        for index, criterion in enumerate(criteria)
    ]
    module["internal_milestones"] = [
        {
            "milestone_id": f"{module_id}_m1",
            "title": f"Implement {module_id}",
            "task": _text(module.get("responsibility") or f"Implement {module_id} according to the architecture sketch."),
            "acceptance_criteria": criteria,
            "skill_refs": [],
            "test_plan": {"required": ["Run focused tests or checks for the module contract."]},
            "metadata": {
                "changed_area": _string_list(module.get("owned_area")),
                "acceptance_checklist": acceptance_checklist,
                "checkpoint_admission_evidence": ["Focused tests/checks or a concrete blocker prove the milestone boundary."],
            },
        }
    ]
    return module


def load_stage_artifact_ref(ref: Any) -> dict[str, Any]:
    return _load_stage_artifact(ref)


def _plan_builder_stage(state: dict[str, Any], workspace: dict[str, Any]) -> str:
    return _text(state.get("plan_builder_stage") or _plan_builder_stage_from_workspace(workspace))


def _plan_builder_stage_from_workspace(workspace: dict[str, Any]) -> str:
    return _text(workspace.get("plan_builder_stage") or workspace.get("planning_stage")).strip().lower()


def _plan_tool_accepts_arg(tool_name: str, arg_name: str) -> bool:
    normalized = str(tool_name or "").strip()
    spec = PLAN_BUILDER_TOOL_SPECS.get(normalized)
    if spec is None and normalized in PLAN_BUILDER_ALIASES:
        spec = PLAN_BUILDER_ALIASES[normalized].tool_spec()
    schema = dict((spec or {}).get("parameters_schema") or {})
    return arg_name in dict(schema.get("properties") or {})


def _assert_bound_module_detail(state: dict[str, Any], module: dict[str, Any]) -> None:
    wanted = _text(state.get("bound_module_id"))
    if not wanted:
        raise ValueError("module_detail stage is missing bound_module_id")
    if _text(module.get("module_id")) != wanted:
        raise ValueError(f"module_detail stage is bound to {wanted}, not {module.get('module_id') or module.get('handle')}")


def _bound_module_detail_module(state: dict[str, Any]) -> dict[str, Any]:
    wanted = _text(state.get("bound_module_id"))
    if not wanted:
        raise ValueError("module_detail stage is missing bound_module_id")
    return _find_module_by_id(state, wanted)


def _stage_artifact_json(artifact: dict[str, Any], *, plan_revision: int, lifecycle: str) -> str:
    payload = dict(artifact)
    payload["plan_revision"] = max(0, int(plan_revision or 0))
    metadata = dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {}
    metadata["plan_revision"] = payload["plan_revision"]
    plan_builder = dict(metadata.get("plan_builder") or {}) if isinstance(metadata.get("plan_builder"), dict) else {}
    if lifecycle:
        plan_builder["lifecycle"] = lifecycle
    if plan_builder:
        metadata["plan_builder"] = plan_builder
    payload["metadata"] = metadata
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _load_stage_artifact(ref: Any) -> dict[str, Any]:
    if isinstance(ref, dict) and isinstance(ref.get("artifact"), dict):
        ref = ref.get("artifact")
    if isinstance(ref, dict) and isinstance(ref.get("payload"), dict):
        return dict(ref.get("payload") or {})
    path_text = ""
    if isinstance(ref, dict):
        path_text = _text(ref.get("path") or ref.get("stage_path"))
        if not path_text:
            artifact_dir = _text(ref.get("artifact_dir"))
            relative_path = _text(ref.get("relative_path"))
            if artifact_dir and relative_path:
                path_text = str(Path(artifact_dir).expanduser() / relative_path)
    elif isinstance(ref, str):
        path_text = _text(ref)
    if not path_text:
        raise ValueError("stage artifact path is required")
    path = Path(path_text).expanduser()
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if isinstance(ref, dict):
        expected = _text(ref.get("sha256"))
        if expected and expected != digest:
            raise ValueError(f"stage artifact sha256 mismatch for {path}")
    payload = json.loads(content.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("stage artifact JSON must be an object")
    return payload


def _state_from_sketch_payload(
    payload: dict[str, Any],
    *,
    plan_handle: str,
    bound_module_id: str,
    sketch_ref: dict[str, Any],
) -> dict[str, Any]:
    sketch = dict(payload or {})
    metadata = dict(sketch.get("metadata") or {}) if isinstance(sketch.get("metadata"), dict) else {}
    constraints = _state_constraints_from_metadata(metadata, plan_handle)
    constraint_by_public = {_text(item.get("id")): _text(item.get("handle")) for item in constraints}
    decisions = _state_decisions_from_metadata(metadata, plan_handle, constraints)
    gate_contract = _normalize_gate_contract(metadata.get("gate_contract"))
    modules: list[dict[str, Any]] = []
    for index, raw_module in enumerate(_dict_list(sketch.get("modules")), start=1):
        module_handle = f"module_{plan_handle}_{index}"
        module_metadata = dict(raw_module.get("metadata") or {})
        modules.append(
            {
                "handle": module_handle,
                "module_id": _text(raw_module.get("module_id")),
                "kind": _text(module_metadata.get("module_kind") or "module"),
                "depends_on_module_handles": [],
                "responsibility": _text(raw_module.get("responsibility")),
                "owned_area": _string_list(raw_module.get("owned_area")),
                "ownership": _string_list(raw_module.get("ownership")),
                "lifecycle": _string_list(raw_module.get("lifecycle")),
                "invariants": _string_list(raw_module.get("invariants")),
                "scope_guard": _text(module_metadata.get("scope_guard")),
                "constraint_handles": [],
                "decision_handles": [],
                    "gate_check_refs": _gate_check_ref_list(module_metadata.get("gate_check_refs")),
                    "languages": _normalize_language_ids(module_metadata.get("languages")),
                    "executor_profile": _text(module_metadata.get("executor_profile")),
                    "module_quality_criteria": _string_list(module_metadata.get("module_quality_criteria")),
                    "module_quality_criteria_details": _state_module_quality_criteria_details(
                        module_metadata.get("module_quality_criteria_details"),
                        constraint_by_public,
                    ),
                    "risk_surfaces": _string_list(module_metadata.get("risk_surfaces")),
                    "delivery_surfaces": _string_list(module_metadata.get("delivery_surfaces")),
                "provided_interfaces": [
                    _state_interface(item, module_handle=module_handle, direction="provided", index=interface_index)
                    for interface_index, item in enumerate(_dict_list(raw_module.get("provided_interfaces")), start=1)
                ],
                "consumed_interfaces": [
                    _state_interface(item, module_handle=module_handle, direction="consumed", index=interface_index)
                    for interface_index, item in enumerate(_dict_list(raw_module.get("consumed_interfaces")), start=1)
                ],
                "internal_milestones": [],
                "closed": True,
            }
        )
    module_by_id = {_text(module.get("module_id")): module for module in modules}
    node_modules = {
        _text(node.get("node_id")): _text(node.get("module_id"))
        for node in _dict_list(dict(sketch.get("topology") or {}).get("nodes"))
    }
    for node in _dict_list(dict(sketch.get("topology") or {}).get("nodes")):
        module = module_by_id.get(_text(node.get("module_id")))
        if not module:
            continue
        dependencies: list[str] = []
        for dep_node_id in _string_list(node.get("depends_on")):
            dep_module = module_by_id.get(node_modules.get(dep_node_id, ""))
            if dep_module:
                dependencies.append(_text(dep_module.get("handle")))
        module["depends_on_module_handles"] = _dedupe_strings(dependencies)
    return {
        "plan_handle": plan_handle,
        "plan_id": _text(sketch.get("plan_id")),
        "task_id": _text(sketch.get("task_id")),
        "goal": _text(sketch.get("summary")),
        "summary": _text(sketch.get("summary")),
        "languages": _normalize_language_ids(metadata.get("languages")),
        "source_refs": _string_list(metadata.get("source_refs")),
        "workflow_next": _workflow_next_payload(metadata.get("workflow_next")),
        "gate_contract": gate_contract,
        "constraints": constraints,
        "design_decisions": decisions,
        "modules": modules,
        "closed": False,
        "lifecycle": "editing",
        "plan_revision": _coerce_int(sketch.get("plan_revision") or metadata.get("plan_revision"), default=0),
        "plan_builder_stage": "module_detail",
        "planning_depth": _text(sketch.get("planning_depth") or "module_detail"),
        "bound_module_id": _explicit_module_id(bound_module_id),
        "sketch_ref": dict(sketch_ref or {}),
        "handle_counters": {
            "constraint": len(constraints),
            "decision": len(decisions),
            "module": len(modules),
            "milestone": 0,
            "ac": 0,
        },
    }


def _compiled_stage_gate_contract(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {}
    return _normalize_gate_contract(metadata.get("gate_contract"))


def _plan_review_markdown(artifact: dict[str, Any], validation: dict[str, Any], *, plan_revision: int) -> str:
    modules = [dict(item) for item in list(artifact.get("modules") or []) if isinstance(item, dict)]
    nodes = [dict(item) for item in list(dict(validation or {}).get("nodes") or []) if isinstance(item, dict)]
    node_by_module = {str(node.get("module_id") or ""): node for node in nodes}
    module_by_node_id = {
        str(node.get("node_id") or ""): str(node.get("module_id") or "")
        for node in nodes
        if _text(node.get("node_id")) and _text(node.get("module_id"))
    }
    metadata = dict(artifact.get("metadata") or {})
    workflow_next = dict(metadata.get("workflow_next") or {})
    next_profile = _text(workflow_next.get("profile") or workflow_next.get("next_profile"))
    lines: list[str] = [
        "# Plan Review",
        "",
        "## Summary",
        f"- Summary: {_text(artifact.get('summary') or artifact.get('plan_id') or 'Plan')}",
        f"- Plan id: `{_text(artifact.get('plan_id'))}`",
        f"- Task id: `{_text(artifact.get('task_id'))}`",
        f"- Revision: `{int(plan_revision or 0)}`",
        f"- Validation: `{_text(validation.get('status') or 'valid')}`",
    ]
    if next_profile:
        lines.append(f"- Default executor: `{next_profile}`")
    languages = _string_list(metadata.get("languages"))
    if languages:
        lines.append("- Languages: " + ", ".join(f"`{item}`" for item in languages))
    _append_plan_review_gate_contract(lines, metadata)
    _append_plan_review_constraints(lines, metadata)
    lines.extend(["", "## Modules"])
    for module in modules:
        module_id = _text(module.get("module_id"))
        node = node_by_module.get(module_id, {})
        module_metadata = dict(module.get("metadata") or {})
        kind = _text(node.get("kind") or module_metadata.get("module_kind") or "module")
        executor = _text(module_metadata.get("executor_profile") or node.get("executor_profile") or next_profile or "default")
        deps = _plan_review_dependency_labels(node.get("depends_on"), module_by_node_id)
        lines.extend(
            [
                "",
                f"### {module_id}",
                f"- Kind: `{kind}`",
                f"- Executor: `{executor}`",
                "- Depends on: " + (", ".join(f"`{item}`" for item in deps) if deps else "-"),
                "- Owned area: " + (", ".join(f"`{item}`" for item in _string_list(module.get("owned_area"))) or "-"),
                f"- Responsibility: {_text(module.get('responsibility')) or '-'}",
            ]
        )
        _append_plan_review_list(lines, "Ownership", _string_list(module.get("ownership")), indent="")
        _append_plan_review_list(lines, "Lifecycle", _string_list(module.get("lifecycle")), indent="")
        _append_plan_review_list(lines, "Invariants", _string_list(module.get("invariants")), indent="")
        _append_plan_review_list(lines, "Quality criteria", _string_list(module_metadata.get("module_quality_criteria")), indent="")
        _append_plan_review_quality_details(lines, _dict_list(module_metadata.get("module_quality_criteria_details")))
        _append_plan_review_list(lines, "Risk surfaces", _string_list(module_metadata.get("risk_surfaces")), indent="")
        _append_plan_review_list(lines, "Delivery surfaces", _string_list(module_metadata.get("delivery_surfaces")), indent="")
        provided = _dict_list(module.get("provided_interfaces"))
        consumed = _dict_list(module.get("consumed_interfaces"))
        if provided or consumed:
            lines.append("- Interfaces:")
            for item in provided:
                _append_plan_review_interface(lines, "provides", item)
            for item in consumed:
                _append_plan_review_interface(lines, "consumes", item)
        milestones = _dict_list(module.get("internal_milestones"))
        if milestones:
            lines.append("- Milestones:")
            for milestone in milestones:
                title = _text(milestone.get("title") or milestone.get("milestone_id"))
                task = _text(milestone.get("task"))
                milestone_metadata = dict(milestone.get("metadata") or {})
                lines.append(f"  - {title or 'Milestone'}")
                if task:
                    lines.append(f"    - Task: {task}")
                _append_plan_review_list(lines, "Gate refs", _string_list(milestone_metadata.get("gate_check_refs")), indent="    ")
                _append_plan_review_list(lines, "Changed area", _string_list(milestone_metadata.get("changed_area")), indent="    ")
                _append_plan_review_list(
                    lines,
                    "Checkpoint evidence",
                    _string_list(milestone_metadata.get("checkpoint_admission_evidence")),
                    indent="    ",
                )
                rendered_criteria: set[str] = set()
                checklist = _dict_list(milestone_metadata.get("acceptance_checklist"))
                for item in checklist:
                    criterion = _text(item.get("criterion"))
                    if not criterion:
                        continue
                    rendered_criteria.add(criterion)
                    ac_id = _text(item.get("id"))
                    refs = _string_list(item.get("gate_check_refs"))
                    constraints = _string_list(item.get("linked_constraint_refs"))
                    heading = f"    - AC `{ac_id}`" if ac_id else "    - AC"
                    lines.append(heading)
                    lines.append(f"      - Criterion: {criterion}")
                    if refs:
                        lines.append("      - Gate refs: " + ", ".join(f"`{ref}`" for ref in refs))
                    if constraints:
                        lines.append("      - Constraint refs: " + ", ".join(f"`{ref}`" for ref in constraints))
                    evidence = _text(item.get("evidence_expectation"))
                    if evidence:
                        lines.append(f"      - Evidence: {evidence}")
                    negative_cases = _string_list(item.get("negative_cases"))
                    if negative_cases:
                        lines.append("      - Negative cases: " + "; ".join(negative_cases))
                for criterion in _string_list(milestone.get("acceptance_criteria")):
                    if criterion and criterion not in rendered_criteria:
                        rendered_criteria.add(criterion)
                        lines.append("    - AC")
                        lines.append(f"      - Criterion: {criterion}")
                test_plan = dict(milestone.get("test_plan") or {})
                _append_plan_review_list(lines, "Required tests", _string_list(test_plan.get("required")), indent="    ")
        module_test_plan = dict(module.get("test_plan") or {})
        _append_plan_review_list(lines, "Module tests", _string_list(module_test_plan.get("required") or module_test_plan.get("module")), indent="")
    contracts = _dict_list(artifact.get("cross_module_contracts"))
    if contracts:
        lines.extend(["", "## Cross Module Contracts"])
        for item in contracts:
            summary = _text(item.get("summary") or item.get("statement") or item.get("contract") or item.get("name"))
            if not summary:
                continue
            refs = _string_list(item.get("gate_check_refs") or item.get("constraint_refs") or item.get("decision_refs"))
            suffix = " (" + ", ".join(f"`{ref}`" for ref in refs) + ")" if refs else ""
            lines.append(f"- {summary}{suffix}")
    system_tests = _dict_list(artifact.get("system_test_plan"))
    if system_tests:
        lines.extend(["", "## System Verification"])
        for item in system_tests:
            lines.append(f"- {_text(item.get('level') or 'system')}: {_text(item.get('evidence') or item.get('summary')) or '-'}")
    risks = _dict_list(artifact.get("risks"))
    if risks:
        lines.extend(["", "## Risks And Assumptions"])
        for item in risks:
            summary = _text(item.get("summary") or item.get("statement") or item.get("risk") or item)
            if summary:
                lines.append(f"- {summary}")
    lines.extend(
        [
            "",
            "## Validation",
            f"- Execution shape: `{_text(validation.get('execution_shape') or 'fork_join_linear')}`",
            "- Module order: " + ", ".join(f"`{item}`" for item in _string_list(validation.get("module_order"))),
            f"- Topology hash: `{_text(validation.get('topology_hash'))}`",
            "",
        ]
    )
    return "\n".join(lines)


def _append_plan_review_list(lines: list[str], label: str, items: list[str], *, indent: str) -> None:
    if not items:
        return
    lines.append(f"{indent}- {label}:")
    for item in items:
        lines.append(f"{indent}  - {item}")


def _append_plan_review_quality_details(lines: list[str], details: list[dict[str, Any]]) -> None:
    if not details:
        return
    lines.append("- Quality detail refs:")
    for detail in details:
        criterion = _text(detail.get("criterion")) or "criterion"
        lines.append(f"  - {criterion}")
        source_refs = _string_list(detail.get("source_refs"))
        if source_refs:
            lines.append("    - Source refs: " + ", ".join(f"`{item}`" for item in source_refs))
        gate_refs = _gate_check_ref_list(detail.get("gate_check_refs"))
        if gate_refs:
            lines.append("    - Gate refs: " + ", ".join(f"`{item}`" for item in gate_refs))
        constraint_refs = _string_list(detail.get("constraint_refs") or detail.get("linked_constraint_refs"))
        if constraint_refs:
            lines.append("    - Constraint refs: " + ", ".join(f"`{item}`" for item in constraint_refs))
        evidence = _text(detail.get("evidence_expectation"))
        if evidence:
            lines.append(f"    - Evidence: {evidence}")


def _plan_review_dependency_labels(value: Any, module_by_node_id: dict[str, str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in _string_list(value):
        label = module_by_node_id.get(item, item)
        if not label or label in seen:
            continue
        seen.add(label)
        result.append(label)
    return result


def _append_plan_review_interface(lines: list[str], direction: str, item: dict[str, Any]) -> None:
    name = _text(item.get("name") or "-")
    shape = _text(item.get("shape") or item.get("contract")) or "-"
    lines.append(f"  - {direction} `{name}`: {shape}")
    for label, key in (
        ("Lifecycle", "lifecycle"),
        ("Ownership", "ownership"),
        ("Error behavior", "error_behavior"),
        ("Compatibility", "compatibility"),
        ("Import path", "import_path"),
        ("Source path", "source_path"),
        ("Public entrypoint", "public_entrypoint"),
        ("Copy policy", "copy_policy"),
    ):
        value = _text(item.get(key))
        if value:
            lines.append(f"    - {label}: {value}")


def _append_plan_review_gate_contract(lines: list[str], metadata: dict[str, Any]) -> None:
    checks = _dict_list(dict(metadata.get("gate_contract") or {}).get("checks"))
    if checks:
        lines.extend(["", "## Source Requirements"])
        for check in checks:
            ref = _text(check.get("ref") or f"gate:{int(check.get('index') or 0)}")
            claim = _text(check.get("claim")) or "-"
            priority = _text(check.get("priority"))
            kind = _text(check.get("kind"))
            source_ref = _text(check.get("source_ref"))
            meta = ", ".join(item for item in (priority, kind, source_ref) if item)
            suffix = f" ({meta})" if meta else ""
            lines.append(f"- `{ref}`: {claim}{suffix}")
    coverage = _dict_list(metadata.get("source_acceptance_coverage"))
    if coverage:
        if not checks:
            lines.extend(["", "## Source Requirements"])
        lines.append("")
        lines.append("### Coverage")
        for item in coverage:
            ref = _text(item.get("ref"))
            claim = _text(item.get("claim"))
            evidence = _dict_list(item.get("evidence"))
            if not ref:
                continue
            lines.append(f"- `{ref}`: {claim or '-'}")
            if not evidence:
                lines.append("  - Evidence: -")
                continue
            for entry in evidence:
                path = _text(entry.get("path"))
                node_kind = _text(entry.get("node_kind"))
                module_id = _text(entry.get("module_id"))
                milestone_id = _text(entry.get("milestone_id"))
                summary = _text(entry.get("summary"))
                where = " / ".join(item for item in (path, module_id, milestone_id, node_kind) if item)
                lines.append(f"  - {where or 'plan node'}: {summary or '-'}")


def _append_plan_review_constraints(lines: list[str], metadata: dict[str, Any]) -> None:
    constraints = _dict_list(metadata.get("constraints"))
    decisions = _dict_list(metadata.get("design_decisions"))
    if constraints:
        lines.extend(["", "## Constraints"])
        for item in constraints:
            statement = _text(item.get("statement") or item.get("summary"))
            if not statement:
                continue
            strength = _text(item.get("strength"))
            refs = _string_list(item.get("gate_check_refs"))
            suffix_parts = [part for part in (strength, ", ".join(refs)) if part]
            suffix = f" ({'; '.join(suffix_parts)})" if suffix_parts else ""
            lines.append(f"- {statement}{suffix}")
    if decisions:
        lines.extend(["", "## Design Decisions"])
        for item in decisions:
            decision = _text(item.get("decision") or item.get("statement") or item.get("summary"))
            rationale = _text(item.get("rationale") or item.get("reason"))
            if not decision:
                continue
            lines.append(f"- {decision}" + (f" - {rationale}" if rationale else ""))


def enrich_plan_review_gate_node_refs(args: dict[str, Any], workspace: dict[str, Any]) -> dict[str, Any]:
    payload = dict(args or {})
    if _text(payload.get("gate_kind")) != "plan_acceptance":
        return payload
    target = dict(payload.get("target") or {})
    plan_ref = target.get("plan_ref") or workspace.get("review_target_plan_ref")
    if not isinstance(plan_ref, dict):
        return payload
    runtime = PlanBuilderRuntime(workspace=workspace, produced_artifacts=[])
    try:
        state = runtime._state_from_ref_or_handle({"plan_ref": plan_ref})
    except Exception:
        return payload
    changed = False
    for section in ("findings", "required_fixes"):
        items: list[Any] = []
        for item in list(payload.get(section) or []):
            if not isinstance(item, dict):
                items.append(item)
                continue
            enriched = dict(item)
            handle = _text(enriched.get("target_handle"))
            target_node = enriched.get("target_node")
            if not handle and isinstance(target_node, dict):
                handle = _text(target_node.get("handle") or target_node.get("target_handle"))
            if handle:
                node = _compact_node(_find_node(state, handle))
                enriched["target_handle"] = _text(node.get("handle") or handle)
                enriched["target_node"] = node
                changed = True
            items.append(enriched)
        if changed:
            payload[section] = items
    return payload


def normalize_gate_contract_payload(value: Any) -> dict[str, Any]:
    return _normalize_gate_contract(value)


def _final_plan_json(artifact: dict[str, Any], *, plan_revision: int, lifecycle: str) -> str:
    payload = {"type": "FinalPlanArtifact", **dict(artifact), "plan_revision": max(0, int(plan_revision))}
    metadata = dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {}
    metadata["plan_revision"] = max(0, int(plan_revision))
    plan_builder = dict(metadata.get("plan_builder") or {}) if isinstance(metadata.get("plan_builder"), dict) else {}
    if lifecycle:
        plan_builder["lifecycle"] = lifecycle
    if plan_builder:
        metadata["plan_builder"] = plan_builder
    payload["metadata"] = metadata
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _reject_revision_submit_overrides(state: dict[str, Any], args: dict[str, Any]) -> None:
    if not isinstance(state.get("source_plan_ref"), dict):
        return
    overrides = [key for key in ("summary", "system_test_plan", "risks", "assumptions") if key in args]
    if overrides:
        raise ValueError(
            "revision submit only freezes the checked-out draft; update intentional top-level changes with "
            f"plan_update_plan first and submit only plan_handle (unexpected submit fields: {', '.join(overrides)})"
        )


def _json_value_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cross_module_contracts_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    contracts: list[dict[str, Any]] = []
    for item in list(state.get("constraints") or []):
        if _text(item.get("strength")) in {"hard_contract", "chosen_contract"}:
            contracts.append(
                {
                    "contract_id": _text(item.get("id")),
                    "kind": _text(item.get("kind")),
                    "strength": _text(item.get("strength")),
                    "statement": _text(item.get("statement")),
                    "source_ref": _text(item.get("source_ref")),
                }
            )
    for item in list(state.get("design_decisions") or []):
        if _text(item.get("strength")) in {"hard_contract", "chosen_contract"}:
            contracts.append(
                {
                    "contract_id": _text(item.get("id")),
                    "kind": "design_decision",
                    "strength": _text(item.get("strength")),
                    "statement": _text(item.get("decision")),
                    "rationale": _text(item.get("rationale")),
                }
            )
    return contracts


_REVISION_KEYED_LIST_FIELDS: dict[str, str] = {
    "modules": "module_id",
    "internal_milestones": "milestone_id",
    "provided_interfaces": "name",
    "consumed_interfaces": "name",
    "cross_module_contracts": "contract_id",
    "constraints": "id",
    "design_decisions": "id",
    "acceptance_checklist": "id",
    "implementation_checklist": "id",
    "nodes": "node_id",
}


def _merge_revision_artifact(source: dict[str, Any], compiled: dict[str, Any]) -> dict[str, Any]:
    merged = _merge_revision_value(source, compiled)
    return dict(merged) if isinstance(merged, dict) else deepcopy(compiled)


def _merge_revision_value(source: Any, compiled: Any, *, field_name: str = "") -> Any:
    if isinstance(source, dict) and isinstance(compiled, dict):
        merged = deepcopy(source)
        for key, value in compiled.items():
            merged[key] = _merge_revision_value(source.get(key), value, field_name=str(key))
        return merged
    if isinstance(source, list) and isinstance(compiled, list):
        identity_key = _REVISION_KEYED_LIST_FIELDS.get(field_name)
        if not identity_key:
            return deepcopy(compiled)
        source_by_id = {
            _text(item.get(identity_key)): item
            for item in source
            if isinstance(item, dict) and _text(item.get(identity_key))
        }
        merged_items: list[Any] = []
        for item in compiled:
            if not isinstance(item, dict):
                merged_items.append(deepcopy(item))
                continue
            identity = _text(item.get(identity_key))
            baseline = source_by_id.get(identity)
            merged_items.append(
                _merge_revision_value(baseline, item)
                if isinstance(baseline, dict)
                else deepcopy(item)
            )
        return merged_items
    return deepcopy(compiled)


def _state_from_plan_payload(
    payload: dict[str, Any],
    *,
    plan_handle: str = "",
    source_plan_ref: dict[str, Any] | None = None,
    plan_revision: int = 0,
) -> dict[str, Any]:
    artifact = validate_final_plan_artifact(payload)
    metadata = dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {}
    plan_builder = dict(metadata.get("plan_builder") or {}) if isinstance(metadata.get("plan_builder"), dict) else {}
    resolved_handle = _text(plan_handle or plan_builder.get("plan_handle"))
    if not resolved_handle:
        safe_plan_id = _safe_id(artifact.plan_id, default_prefix="plan")
        resolved_handle = safe_plan_id if safe_plan_id.startswith("plan_") else f"plan_{safe_plan_id}"
    constraints = _state_constraints_from_metadata(metadata, resolved_handle)
    decisions = _state_decisions_from_metadata(metadata, resolved_handle, constraints)
    gate_contract = _normalize_gate_contract(metadata.get("gate_contract"))
    constraint_by_public = {_text(item.get("id")): _text(item.get("handle")) for item in constraints}
    decision_by_public = {_text(item.get("id")): _text(item.get("handle")) for item in decisions}
    module_kind_by_id, dependency_ids_by_module_id, executor_by_module_id = _artifact_topology_maps(artifact)
    module_handle_by_id = {
        module.module_id: f"module_{resolved_handle}_{index}"
        for index, module in enumerate(artifact.modules, start=1)
    }
    modules: list[dict[str, Any]] = []
    milestone_count = 0
    ac_count = 0
    for module_index, module in enumerate(artifact.modules, start=1):
        module_handle = module_handle_by_id[module.module_id]
        module_metadata = dict(module.metadata or {})
        milestones: list[dict[str, Any]] = []
        for milestone in module.internal_milestones:
            milestone_count += 1
            milestone_handle = f"milestone_{resolved_handle}_{milestone_count}"
            milestone_metadata = dict(milestone.metadata or {})
            checklist = _dict_list(milestone_metadata.get("acceptance_checklist"))
            if not checklist:
                checklist = [
                    {
                        "id": _public_id("AC", index),
                        "criterion": criterion,
                        "evidence_expectation": "",
                        "negative_cases": [],
                    }
                    for index, criterion in enumerate(milestone.acceptance_criteria, start=1)
                ]
            acceptance: list[dict[str, Any]] = []
            for ac_index, raw_ac in enumerate(checklist, start=1):
                ac_count += 1
                acceptance.append(
                    {
                        "handle": f"ac_{resolved_handle}_{ac_count}",
                        "id": _text(raw_ac.get("id") or _public_id("AC", ac_index)),
                        "criterion": _text(raw_ac.get("criterion")),
                        "evidence_expectation": _text(raw_ac.get("evidence_expectation")),
                        "linked_constraint_handles": _handles_from_public_refs(raw_ac.get("linked_constraint_refs"), constraint_by_public),
                        "gate_check_refs": _gate_check_ref_list(raw_ac.get("gate_check_refs")),
                        "negative_cases": _string_list(raw_ac.get("negative_cases")),
                        "quantifier": _text(raw_ac.get("quantifier")),
                    }
                )
            milestones.append(
                {
                    "handle": milestone_handle,
                    "milestone_id": milestone.milestone_id,
                    "title": milestone.title,
                    "task": milestone.task,
                    "scope_guard": _text(milestone_metadata.get("scope_guard")),
                    "changed_area": _string_list(milestone_metadata.get("changed_area")),
                    "constraint_handles": _handles_from_public_refs(milestone_metadata.get("constraint_refs"), constraint_by_public),
                    "decision_handles": _handles_from_public_refs(milestone_metadata.get("decision_refs"), decision_by_public),
                    "gate_check_refs": _gate_check_ref_list(milestone_metadata.get("gate_check_refs")),
                    "languages": _normalize_language_ids(milestone_metadata.get("languages")),
                    "tests_required": _string_list((milestone.test_plan or {}).get("required")),
                    "skill_refs": list(milestone.skill_refs),
                    "public_api_added": _text(milestone_metadata.get("public_api_added")),
                    "checkpoint_admission_evidence": _string_list(milestone_metadata.get("checkpoint_admission_evidence"))
                    or _string_list((milestone.test_plan or {}).get("checkpoint_admission")),
                    "acceptance": acceptance,
                    "closed": True,
                }
            )
        modules.append(
            {
                "handle": module_handle,
                "module_id": module.module_id,
                "kind": _text(module_metadata.get("module_kind") or module_kind_by_id.get(module.module_id) or "module"),
                "depends_on_module_handles": [
                    module_handle_by_id[module_id]
                    for module_id in dependency_ids_by_module_id.get(module.module_id, [])
                    if module_id in module_handle_by_id
                ],
                "responsibility": module.responsibility,
                "owned_area": list(module.owned_area),
                "ownership": list(module.ownership),
                "lifecycle": list(module.lifecycle),
                "invariants": list(module.invariants),
                "scope_guard": _text(module_metadata.get("scope_guard")),
                "constraint_handles": _handles_from_public_refs(module_metadata.get("constraint_refs"), constraint_by_public),
                    "decision_handles": _handles_from_public_refs(module_metadata.get("decision_refs"), decision_by_public),
                    "gate_check_refs": _gate_check_ref_list(module_metadata.get("gate_check_refs")),
                    "languages": _normalize_language_ids(module_metadata.get("languages")),
                    "executor_profile": _text(module_metadata.get("executor_profile") or executor_by_module_id.get(module.module_id)),
                    "module_quality_criteria": _string_list(module_metadata.get("module_quality_criteria"))
                    or _string_list((module.test_plan or {}).get("module_quality")),
                    "module_quality_criteria_details": _state_module_quality_criteria_details(
                        module_metadata.get("module_quality_criteria_details"),
                        constraint_by_public,
                    ),
                    "risk_surfaces": _string_list(module_metadata.get("risk_surfaces")),
                    "delivery_surfaces": _string_list(module_metadata.get("delivery_surfaces")),
                "provided_interfaces": [_state_interface(item, module_handle=module_handle, direction="provided", index=index) for index, item in enumerate(module.provided_interfaces, start=1)],
                "consumed_interfaces": [_state_interface(item, module_handle=module_handle, direction="consumed", index=index) for index, item in enumerate(module.consumed_interfaces, start=1)],
                "internal_milestones": milestones,
                "closed": True,
            }
        )
    state = {
        "plan_handle": resolved_handle,
        "plan_id": artifact.plan_id,
        "task_id": artifact.task_id,
        "goal": artifact.summary,
        "summary": artifact.summary,
        "languages": _normalize_language_ids(metadata.get("languages")),
        "source_refs": _string_list(metadata.get("source_refs")),
        "workflow_next": _workflow_next_payload(metadata.get("workflow_next")),
        "gate_contract": gate_contract,
        "constraints": constraints,
        "design_decisions": decisions,
        "modules": modules,
        "system_test_plan": [deepcopy(item) for item in _dict_list(payload.get("system_test_plan"))],
        "risks": [deepcopy(item) for item in _dict_list(payload.get("risks"))],
        "orchestration": deepcopy(dict(payload.get("orchestration") or {})),
        "closed": False,
        "lifecycle": _text(plan_builder.get("lifecycle") or "editing"),
        "plan_revision": max(0, int(plan_revision or 0)),
        "handle_counters": {
            "constraint": len(constraints),
            "decision": len(decisions),
            "module": len(modules),
            "milestone": milestone_count,
            "ac": ac_count,
        },
    }
    if source_plan_ref:
        state["source_plan_ref"] = dict(source_plan_ref)
        state["_revision_source_artifact"] = deepcopy(payload)
        known_contract_ids = {
            _text(item.get("contract_id"))
            for item in _cross_module_contracts_from_state(state)
            if _text(item.get("contract_id"))
        }
        state["_revision_cross_module_contracts"] = [
            deepcopy(item)
            for item in _dict_list(payload.get("cross_module_contracts"))
            if _text(item.get("contract_id")) not in known_contract_ids
        ]
        state["_revision_gate_coverage_digest"] = _json_value_digest(_gate_check_coverage_projection(state))
    return state


def _source_plan_handle_from_payload(payload: dict[str, Any], ref: dict[str, Any] | None = None) -> str:
    metadata = dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {}
    plan_builder = dict(metadata.get("plan_builder") or {}) if isinstance(metadata.get("plan_builder"), dict) else {}
    handle = _text(plan_builder.get("plan_handle"))
    if handle:
        return handle
    plan_id = _text(payload.get("plan_id") or (ref or {}).get("plan_id"))
    safe_plan_id = _safe_id(plan_id, default_prefix="plan")
    return safe_plan_id if safe_plan_id.startswith("plan_") else f"plan_{safe_plan_id}"


def _revision_checklist_from_workspace(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[Any] = [workspace.get("plan_revision_checklist")]
    planner_work_order = workspace.get("planner_work_order")
    if isinstance(planner_work_order, dict):
        revision_source = planner_work_order.get("revision_source")
        if isinstance(revision_source, dict):
            candidates.append(revision_source.get("plan_revision_checklist"))
    for value in candidates:
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
    return []


def _remap_plan_revision_checklist(
    checklist: list[dict[str, Any]],
    *,
    source_plan_handle: str,
    target_plan_handle: str,
) -> list[dict[str, Any]]:
    if not checklist:
        return []
    return [
        _remap_plan_handle_value(dict(item), source_plan_handle=source_plan_handle, target_plan_handle=target_plan_handle)
        for item in checklist
    ]


def _remap_plan_handle_value(value: Any, *, source_plan_handle: str, target_plan_handle: str) -> Any:
    if not source_plan_handle or not target_plan_handle or source_plan_handle == target_plan_handle:
        return value
    if isinstance(value, str):
        if value == source_plan_handle:
            return target_plan_handle
        return value.replace(f"_{source_plan_handle}_", f"_{target_plan_handle}_")
    if isinstance(value, dict):
        return {
            key: _remap_plan_handle_value(item, source_plan_handle=source_plan_handle, target_plan_handle=target_plan_handle)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _remap_plan_handle_value(item, source_plan_handle=source_plan_handle, target_plan_handle=target_plan_handle)
            for item in value
        ]
    return value


_APPLY_REVISION_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "op_minion_plan_update_plan",
        "op_minion_plan_add_constraint",
        "op_minion_plan_update_constraint",
        "op_minion_plan_delete_constraint",
        "op_minion_plan_add_design_decision",
        "op_minion_plan_update_design_decision",
        "op_minion_plan_delete_design_decision",
        "op_minion_plan_add_module_outline",
        "op_minion_plan_update_module",
        "op_minion_plan_delete_module",
        "op_minion_plan_merge_modules",
        "op_minion_plan_add_milestone_outline",
        "op_minion_plan_update_milestone",
        "op_minion_plan_delete_milestone",
        "op_minion_plan_add_acceptance_criterion",
        "op_minion_plan_update_acceptance_criterion",
        "op_minion_plan_delete_acceptance_criterion",
        "op_minion_plan_replace_milestone_acceptance_criteria",
    }
)


def _revision_item_by_id(state: dict[str, Any], item_id: str) -> dict[str, Any]:
    requested = _text(item_id)
    for item in [raw for raw in list(state.get("plan_revision_checklist") or []) if isinstance(raw, dict)]:
        if _text(item.get("id")) == requested:
            return item
    raise ValueError(f"unknown plan revision checklist item_id: {requested}")


def _mark_revision_item_resolved(
    item: dict[str, Any],
    *,
    status: str,
    evidence: str,
    tool: str = "",
    applied_args: dict[str, Any] | None = None,
) -> None:
    resolution = {
        "status": _text(status),
        "evidence": _text(evidence)[:700],
    }
    if tool:
        resolution["tool"] = _text(tool)
    if applied_args:
        resolution["applied_args"] = dict(applied_args)
    item["resolution"] = {key: value for key, value in resolution.items() if value not in ("", [], {}, None)}


def _revision_checklist_submit_errors(state: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    modules = [dict(item) for item in list(state.get("modules") or []) if isinstance(item, dict)]
    implementation_modules = [
        item for item in modules if _module_kind(item.get("kind")) == "module"
    ]
    for item in [dict(raw) for raw in list(state.get("plan_revision_checklist") or []) if isinstance(raw, dict)]:
        text = json.dumps(item, ensure_ascii=False, sort_keys=True).lower()
        requires_merge = (
            "plan_merge_modules" in text
            or "exactly one implementation module" in text
            or "single implementation module" in text
            or ("consolidat" in text and "implementation module" in text)
        )
        if not requires_merge:
            continue
        if len(implementation_modules) <= 1:
            continue
        target_handle = _text(item.get("target_handle"))
        source_candidates = [
            module
            for module in implementation_modules
            if not target_handle or _text(module.get("handle")) != target_handle
        ]
        source_hint = ", ".join(
            f"{_text(module.get('module_id'))}({_text(module.get('handle'))})"
            for module in source_candidates[:4]
        )
        target_hint = target_handle or _text(implementation_modules[0].get("handle"))
        current = ", ".join(
            f"{_text(module.get('module_id'))}({_text(module.get('handle'))})"
            for module in implementation_modules
        )
        errors.append(
            f"{_text(item.get('id') or 'PRC')} requires merging implementation modules; "
            f"current implementation modules: {current}. "
            f"Call plan_merge_modules with target_module_handle={target_hint}"
            + (f" and source_module_handle from: {source_hint}" if source_hint else "")
            + "."
        )
    return errors


def _validation_with_extra_errors(validation: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    payload = dict(validation or {})
    merged_errors = [str(item) for item in list(payload.get("errors") or []) if str(item).strip()]
    merged_errors.extend(str(item) for item in errors if str(item).strip())
    payload["errors"] = merged_errors
    if merged_errors and str(payload.get("status") or "").strip().lower() == "valid":
        payload["status"] = "invalid"
    return payload


def _revision_checklist_llm_text(checklist: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for raw in checklist[:8]:
        item = dict(raw)
        item_id = _text(item.get("id") or "PRC")
        action = _text(item.get("action")) or _text(item.get("summary")) or "Address reviewer finding."
        kind = _text(item.get("kind") or item.get("revision_kind"))
        reject_reason = _text(item.get("reject_reason"))
        target = _text(item.get("target_handle"))
        target_path = _text(item.get("target_path") or dict(item.get("target_node") or {}).get("path"))
        suggested_tool = _text(item.get("suggested_tool"))
        suggested_args = dict(item.get("suggested_args") or {}) if isinstance(item.get("suggested_args"), dict) else {}
        resolution = dict(item.get("resolution") or {}) if isinstance(item.get("resolution"), dict) else {}
        route = " -> ".join(_string_list(item.get("suggested_tool_route")))
        suffixes: list[str] = []
        if kind:
            suffixes.append(f"kind={kind}")
        if target:
            suffixes.append(f"target={target}")
        if target_path:
            suffixes.append(f"path={target_path}")
        if suggested_tool:
            suffixes.append(f"tool={suggested_tool.replace('op_minion_', '')}")
        if suggested_args:
            suffixes.append("args=" + json.dumps(suggested_args, ensure_ascii=False, sort_keys=True))
        if route:
            suffixes.append(f"route={route}")
        if resolution:
            suffixes.append(f"resolution={_text(resolution.get('status')) or 'set'}")
        suffix = f" ({'; '.join(suffixes)})" if suffixes else ""
        reason = f" Reject reason: {reject_reason}" if reject_reason else ""
        lines.append(f"- {item_id}: {action}{reason}{suffix}")
    return "\n".join(lines) or "- No revision checklist items."


def _plan_ref_is_compatible(requested: dict[str, Any], candidate: dict[str, Any]) -> bool:
    for key in ("plan_id", "task_id", "sha256"):
        requested_value = _text(requested.get(key))
        candidate_value = _text(candidate.get(key))
        if requested_value and candidate_value and requested_value != candidate_value:
            return False
    requested_revision = _text(requested.get("plan_revision"))
    candidate_revision = _text(candidate.get("plan_revision"))
    if requested_revision and candidate_revision and requested_revision != candidate_revision:
        return False
    return True


def _merge_plan_ref(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base or {})
    for key, value in dict(override or {}).items():
        if value not in (None, "", []):
            merged[key] = value
    return merged


def _state_constraints_from_metadata(metadata: dict[str, Any], plan_handle: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(_dict_list(metadata.get("constraints")), start=1):
        result.append(
            {
                "handle": f"constraint_{plan_handle}_{index}",
                "id": _text(raw.get("id") or raw.get("contract_id") or _public_id("C", index)),
                "kind": _text(raw.get("kind") or "contract"),
                "strength": _strength(raw.get("strength"), default="hard_contract"),
                "statement": _text(raw.get("statement")),
                "source_ref": _text(raw.get("source_ref")),
                "rationale": _text(raw.get("rationale")),
                "global_only": bool(raw.get("global_only")),
                "gate_check_refs": _gate_check_ref_list(raw.get("gate_check_refs")),
            }
        )
    return result


def _state_decisions_from_metadata(metadata: dict[str, Any], plan_handle: str, constraints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    constraint_by_public = {_text(item.get("id")): _text(item.get("handle")) for item in constraints}
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(_dict_list(metadata.get("design_decisions")), start=1):
        result.append(
            {
                "handle": f"decision_{plan_handle}_{index}",
                "id": _text(raw.get("id") or raw.get("contract_id") or _public_id("D", index)),
                "question": _text(raw.get("question")),
                "decision": _text(raw.get("decision") or raw.get("statement")),
                "strength": _strength(raw.get("strength"), default="chosen_contract"),
                "rationale": _text(raw.get("rationale")),
                "alternatives": _string_list(raw.get("alternatives")),
                "downstream_effect": _text(raw.get("downstream_effect")),
                "linked_constraint_handles": _handles_from_public_refs(raw.get("linked_constraint_refs"), constraint_by_public),
                "gate_check_refs": _gate_check_ref_list(raw.get("gate_check_refs")),
            }
        )
    return result


def _artifact_topology_maps(artifact: PlanArtifact) -> tuple[dict[str, str], dict[str, list[str]], dict[str, str]]:
    nodes = list((artifact.orchestration or {}).get("topology", {}).get("nodes") or [])
    node_module_by_id: dict[str, str] = {}
    kind_by_module_id: dict[str, str] = {}
    depends_by_module_id: dict[str, list[str]] = {}
    executor_by_module_id: dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        module_id = _text(node.get("module_id"))
        node_id = _text(node.get("node_id"))
        if module_id and node_id:
            node_module_by_id[node_id] = module_id
            kind_by_module_id[module_id] = _module_kind(node.get("kind"))
            raw_executor = node.get("executor")
            executor_profile = _text(node.get("executor_profile") or (raw_executor if isinstance(raw_executor, str) else ""))
            if not executor_profile and isinstance(raw_executor, dict):
                executor_profile = _text(
                    raw_executor.get("profile")
                    or raw_executor.get("executor_profile")
                    or raw_executor.get("minion_profile")
                    or raw_executor.get("dispatch_profile")
                )
                if not executor_profile:
                    profile_group = _text(raw_executor.get("profile_group") or raw_executor.get("group"))
                    profile_name = _text(raw_executor.get("profile_name") or raw_executor.get("name"))
                    if profile_group or profile_name:
                        executor_profile = ".".join(item for item in (profile_group, profile_name) if item)
            if executor_profile:
                executor_by_module_id[module_id] = executor_profile
    for node in nodes:
        if not isinstance(node, dict):
            continue
        module_id = _text(node.get("module_id"))
        if not module_id:
            continue
        depends_by_module_id[module_id] = [
            node_module_by_id[item]
            for item in _string_list(node.get("depends_on"))
            if item in node_module_by_id
        ]
    return kind_by_module_id, depends_by_module_id, executor_by_module_id


def _handles_from_public_refs(value: Any, mapping: dict[str, str]) -> list[str]:
    result: list[str] = []
    for ref in _string_list(value):
        handle = mapping.get(ref)
        if handle:
            result.append(handle)
    return result


def _state_interface(raw: dict[str, Any], *, module_handle: str, direction: str, index: int) -> dict[str, Any]:
    item = dict(raw or {})
    item.setdefault("producer", "" if direction == "consumed" else module_handle)
    item.setdefault("consumer", "" if direction == "provided" else module_handle)
    item["handle"] = f"interface_{module_handle}_{direction}_{index}"
    item["direction"] = direction
    return item


def _plan_snapshot(state: dict[str, Any], *, full: bool) -> dict[str, Any]:
    handle_tree = []
    for module in list(state.get("modules") or []):
        module_entry = {
            "handle": module.get("handle"),
            "module_id": module.get("module_id"),
            "kind": module.get("kind"),
            "milestones": [],
        }
        for milestone in list(module.get("internal_milestones") or []):
            milestone_entry = {
                "handle": milestone.get("handle"),
                "milestone_id": milestone.get("milestone_id"),
                "title": milestone.get("title"),
                "acceptance": [
                    {"handle": ac.get("handle"), "id": ac.get("id"), "criterion": ac.get("criterion")}
                    for ac in list(milestone.get("acceptance") or [])
                ],
            }
            module_entry["milestones"].append(milestone_entry)
        handle_tree.append(module_entry)
    result = {
        "plan_handle": state.get("plan_handle"),
        "plan_id": state.get("plan_id"),
        "task_id": state.get("task_id"),
        "plan_revision": state.get("plan_revision"),
        "lifecycle": state.get("lifecycle"),
        "summary": state.get("summary"),
        "languages": list(state.get("languages") or []),
        "gate_contract": _compiled_gate_contract(state),
        "source_acceptance_coverage": _gate_check_coverage_projection(state),
        "handle_tree": handle_tree,
    }
    if full:
        result["state"] = {
            key: value
            for key, value in state.items()
            if not str(key).startswith("_revision_")
        }
    return result


def _iter_plan_nodes(state: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = [
        {
            "node_kind": "plan",
            "handle": state.get("plan_handle"),
            "path": str(state.get("plan_id") or ""),
            "summary": _text(state.get("summary")),
            "fields": {"plan_id": state.get("plan_id"), "task_id": state.get("task_id")},
        }
    ]
    for item in _gate_checks(state):
        nodes.append(
            {
                "node_kind": "gate_check",
                "handle": _text(item.get("ref")),
                "path": _text(item.get("ref")),
                "summary": _text(item.get("claim")),
                "fields": dict(item),
            }
        )
    for item in list(state.get("constraints") or []):
        nodes.append(
            {
                "node_kind": "constraint",
                "handle": item.get("handle"),
                "path": item.get("id"),
                "summary": item.get("statement"),
                "fields": dict(item),
            }
        )
    for item in list(state.get("design_decisions") or []):
        nodes.append(
            {
                "node_kind": "decision",
                "handle": item.get("handle"),
                "path": item.get("id"),
                "summary": item.get("decision"),
                "fields": dict(item),
            }
        )
    for module in list(state.get("modules") or []):
        module_path = _text(module.get("module_id"))
        nodes.append(
            {
                "node_kind": "module",
                "handle": module.get("handle"),
                "path": module_path,
                "summary": module.get("responsibility"),
                "module_handle": module.get("handle"),
                "module_id": module.get("module_id"),
                "fields": dict(module),
            }
        )
        for direction, key in (("provided", "provided_interfaces"), ("consumed", "consumed_interfaces")):
            for interface in list(module.get(key) or []):
                nodes.append(
                    {
                        "node_kind": "interface",
                        "handle": interface.get("handle") or f"interface_{module.get('handle')}_{direction}_{interface.get('name')}",
                        "path": f"{module_path} > {direction}:{interface.get('name')}",
                        "summary": interface.get("shape") or interface.get("name"),
                        "module_handle": module.get("handle"),
                        "module_id": module.get("module_id"),
                        "fields": dict(interface),
                    }
                )
        for milestone in list(module.get("internal_milestones") or []):
            milestone_path = f"{module_path} > {milestone.get('milestone_id')}"
            nodes.append(
                {
                    "node_kind": "milestone",
                    "handle": milestone.get("handle"),
                    "path": milestone_path,
                    "summary": milestone.get("task") or milestone.get("title"),
                    "module_handle": module.get("handle"),
                    "module_id": module.get("module_id"),
                    "milestone_handle": milestone.get("handle"),
                    "milestone_id": milestone.get("milestone_id"),
                    "fields": dict(milestone),
                }
            )
            for ac in list(milestone.get("acceptance") or []):
                nodes.append(
                    {
                        "node_kind": "acceptance_criterion",
                        "handle": ac.get("handle"),
                        "path": f"{milestone_path} > {ac.get('id')}",
                        "summary": ac.get("criterion"),
                        "module_handle": module.get("handle"),
                        "module_id": module.get("module_id"),
                        "milestone_handle": milestone.get("handle"),
                        "milestone_id": milestone.get("milestone_id"),
                        "acceptance_handle": ac.get("handle"),
                        "acceptance_id": ac.get("id"),
                        "fields": dict(ac),
                    }
                )
    return nodes


def _find_node(state: dict[str, Any], handle: str) -> dict[str, Any]:
    requested = _text(handle)
    for node in _iter_plan_nodes(state):
        if _text(node.get("handle")) == requested:
            return node
    matches = _plan_node_alias_matches(state, requested)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        kinds = ", ".join(f"{item.get('node_kind')}:{item.get('path') or item.get('handle')}" for item in matches[:8])
        raise ValueError(f"ambiguous plan node ref: {requested}; matches: {kinds}")
    raise ValueError(f"unknown plan node handle: {requested}")


def _plan_node_alias_matches(state: dict[str, Any], requested: str) -> list[dict[str, Any]]:
    requested = _text(requested)
    if not requested:
        return []
    nodes = _iter_plan_nodes(state)
    tokens = _plan_ref_tokens(requested)
    direct = _matching_plan_nodes(nodes, requested)
    if direct:
        return _prefer_node_kind_for_ref(requested, direct)
    for token in reversed(tokens):
        direct = _matching_plan_nodes(nodes, token)
        if direct:
            return _prefer_node_kind_for_ref(requested, direct)
    normalized = _normalize_plan_ref(requested)
    fuzzy = [
        node
        for node in nodes
        if normalized
        and normalized
        in {
            _normalize_plan_ref(node.get("handle")),
            _normalize_plan_ref(node.get("path")),
            _normalize_plan_ref(node.get("module_id")),
            _normalize_plan_ref(node.get("milestone_id")),
            _normalize_plan_ref(node.get("acceptance_id")),
        }
    ]
    return _prefer_node_kind_for_ref(requested, _dedupe_nodes(fuzzy))


def _matching_plan_nodes(nodes: list[dict[str, Any]], requested: str) -> list[dict[str, Any]]:
    return _dedupe_nodes(
        [
            node
            for node in nodes
            if requested
            in {
                _text(node.get("handle")),
                _text(node.get("path")),
                _text(node.get("module_id")),
                _text(node.get("milestone_id")),
                _text(node.get("acceptance_id")),
            }
        ]
    )


def _plan_ref_tokens(value: str) -> list[str]:
    return [item for item in re.findall(r"[A-Za-z0-9_:\-.]+", _text(value)) if item]


def _normalize_plan_ref(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _text(value).lower())


def _prefer_node_kind_for_ref(requested: str, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(nodes) <= 1:
        return nodes
    lowered = requested.lower()
    preferences: list[str] = []
    if "acceptance" in lowered or re.search(r"\bac\b", lowered):
        preferences.append("acceptance_criterion")
    if "milestone" in lowered or "m_" in lowered or re.search(r"_m\d+\b", lowered):
        preferences.append("milestone")
    if "module" in lowered:
        preferences.append("module")
    for kind in preferences:
        filtered = [node for node in nodes if _text(node.get("node_kind")) == kind]
        if len(filtered) == 1:
            return filtered
    return nodes


def _dedupe_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for node in nodes:
        key = _text(node.get("handle")) or f"{node.get('node_kind')}:{node.get('path')}"
        if key in seen:
            continue
        seen.add(key)
        result.append(node)
    return result


def _query_matches_node(query: str, node: dict[str, Any]) -> bool:
    terms = [term for term in re.split(r"\s+", query.lower()) if term]
    if not terms:
        return True
    haystack = json.dumps(node, ensure_ascii=False, sort_keys=True).lower()
    return all(term in haystack for term in terms)


def _compact_node(node: dict[str, Any]) -> dict[str, Any]:
    return {
        key: node.get(key)
        for key in (
            "node_kind",
            "handle",
            "path",
            "summary",
            "module_handle",
            "module_id",
            "milestone_handle",
            "milestone_id",
            "acceptance_handle",
            "acceptance_id",
        )
        if node.get(key) not in (None, "", [], {})
    }


def _gate_contract_from_workspace(workspace: dict[str, Any]) -> dict[str, Any]:
    for key in ("gate_contract", "source_gate_contract", "review_target_gate_contract"):
        value = workspace.get(key)
        if isinstance(value, (dict, list, tuple)):
            return _normalize_gate_contract(value)
    return {"checks": []}


def _normalize_gate_contract(value: Any) -> dict[str, Any]:
    raw_checks: Any
    if isinstance(value, dict):
        raw_checks = value.get("checks") or value.get("checklist") or value.get("items") or []
    else:
        raw_checks = value
    checks: list[dict[str, Any]] = []
    for index, raw in enumerate(list(raw_checks or [])):
        if not isinstance(raw, dict):
            continue
        checks.append(_normalize_gate_check(raw, index=index))
    return {"checks": checks}


def _normalize_gate_check(raw: dict[str, Any], *, index: int) -> dict[str, Any]:
    data = dict(raw or {})
    claim = _text(data.get("claim") or data.get("text") or data.get("source_text") or data.get("summary"))
    if not claim and not bool(data.get("deleted")):
        raise ValueError("gate check claim is required")
    raw_kind = _text(data.get("kind") or data.get("check_kind")).lower()
    kind = _gate_check_kind(data.get("kind") or data.get("check_kind"))
    raw_mechanical = data.get("mechanical_check")
    try:
        mechanical = _normalize_mechanical_check(raw_mechanical)
    except ValueError as exc:
        if _should_downgrade_loose_mechanical_check(raw_mechanical, exc):
            mechanical = {}
            if kind in {"mechanical", "hybrid"}:
                kind = "semantic"
        else:
            raise
    if mechanical and kind == "semantic" and raw_kind in {"", "requirement", "contract", "hard_requirement", "source_requirement"}:
        kind = "hybrid"
    if kind == "hybrid" and not mechanical and not raw_mechanical and not bool(data.get("deleted")):
        kind = "semantic"
    if kind in {"mechanical", "hybrid"} and not mechanical and not bool(data.get("deleted")):
        raise ValueError("mechanical or hybrid gate checks require mechanical_check")
    result = {
        "index": int(index),
        "ref": f"gate:{int(index)}",
        "priority": _gate_priority(data.get("priority") or data.get("strength")),
        "kind": kind,
        "claim": claim,
        "source_ref": _text(data.get("source_ref")),
        "rationale": _text(data.get("rationale")),
        "mechanical_check": mechanical,
        "deleted": bool(data.get("deleted")),
    }
    return {key: value for key, value in result.items() if value not in ("", [], {})}


def _gate_priority(value: Any) -> str:
    text = _text(value or "hard").lower()
    if text in {"hard_contract", "chosen_contract", "required", "must", "blocker", "high", "critical", "p0", "p1"}:
        text = "hard"
    elif text in {"medium", "should", "preferred", "p2"}:
        text = "preference"
    elif text in {"low", "info", "informational", "note", "p3", "p4"}:
        text = "advisory"
    if text not in {"hard", "preference", "advisory", "out_of_scope"}:
        raise ValueError("gate check priority must be hard, preference, advisory, or out_of_scope")
    return text


def _gate_check_kind(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", _text(value or "semantic").lower()).strip("_")
    if text in {"mechanical_check", "mechanical_count", "mechanical_requirement"}:
        text = "mechanical"
    if text in {
        "requirement",
        "contract",
        "hard_requirement",
        "source_requirement",
        "preference",
        "design_preference",
        "advisory",
        "note",
        "risk",
        "test_requirement",
        "api_requirement",
    }:
        text = "semantic"
    if text not in {"semantic", "mechanical", "hybrid"}:
        raise ValueError("gate check kind must be semantic, mechanical, or hybrid")
    return text


def _normalize_mechanical_check(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = _mechanical_check_from_text(value)
    if not isinstance(value, dict):
        return {}
    data = dict(value or {})
    check_type = _text(data.get("type") or data.get("check") or data.get("predicate") or data.get("metric")).lower()
    if check_type in _LOOSE_NONCOUNT_MECHANICAL_TYPES:
        raise ValueError(f"unsupported loose non-count mechanical_check.type: {check_type}")
    if check_type in {"count", "count_equals", "count_eq", "exact_count", "count_at_most", "count_lte", "count_at_least", "count_gte", "count_between"}:
        data = _mechanical_check_from_loose_mapping({**data, "kind": check_type})
        check_type = _text(data.get("type") or data.get("check") or data.get("predicate") or data.get("metric")).lower()
    elif not check_type:
        data = _mechanical_check_from_loose_mapping(data)
        check_type = _text(data.get("type") or data.get("check") or data.get("predicate") or data.get("metric")).lower()
    if not check_type:
        return {}
    check_type = _MECHANICAL_CHECK_TYPE_ALIASES.get(check_type, check_type)
    if check_type not in _SUPPORTED_MECHANICAL_CHECK_TYPES:
        supported = ", ".join(sorted(_SUPPORTED_MECHANICAL_CHECK_TYPES))
        raise ValueError(f"unsupported mechanical_check.type: {check_type}; supported: {supported}")
    op = _normalize_mechanical_operator(data.get("op") or data.get("operator") or "eq")
    if op not in {"eq", "lte", "gte", "between"}:
        raise ValueError("mechanical_check.op must be eq, lte, gte, or between")
    min_value = data.get("min")
    max_value = data.get("max")
    value = data.get("value")
    if value is None:
        value = data.get("expected")
    if op == "between" and min_value is None and max_value is None and isinstance(value, list | tuple) and len(value) == 2:
        min_value = value[0]
        max_value = value[1]
        value = None
    if op in {"eq", "lte", "gte"} and value is None:
        raise ValueError(f"mechanical_check.value is required when op is {op}")
    if op == "between" and (min_value is None or max_value is None):
        raise ValueError("mechanical_check.min and mechanical_check.max are required when op is between")
    result = {
        "type": check_type,
        "op": op,
        "module_kind": _text(data.get("module_kind") or data.get("kind")),
        "module_id": _text(data.get("module_id")),
        "value": value,
        "min": min_value,
        "max": max_value,
    }
    return {key: value for key, value in result.items() if value not in ("", [], None)}


def _mechanical_check_from_loose_mapping(data: dict[str, Any]) -> dict[str, Any]:
    kind = _text(data.get("kind") or data.get("operator")).lower()
    if data.get("path") is not None:
        target = _mechanical_count_target_from_path(_text(data.get("path")))
        return {
            **target,
            "op": _normalize_mechanical_operator(data.get("op") or data.get("operator") or "eq"),
            "value": _normalize_mechanical_expected_count(data.get("expected", data.get("value"))),
        }
    subject = _text(data.get("subject") or data.get("target") or data.get("metric") or data.get("name"))
    if kind not in {
        "exact_count",
        "count",
        "count_equals",
        "count_eq",
        "bounded_count",
        "range_count",
        "count_at_most",
        "count_lte",
        "count_at_least",
        "count_gte",
        "count_between",
    }:
        if subject:
            try:
                target = _mechanical_count_target_from_subject(subject, data)
            except ValueError:
                return {}
            low = data.get("min")
            high = data.get("max")
            expected = _normalize_mechanical_expected_count(data.get("expected", data.get("value")))
            if low is not None or high is not None:
                return {**target, "op": "between", "min": low if low is not None else expected, "max": high if high is not None else expected}
            return {**target, "op": _normalize_mechanical_operator(data.get("op") or data.get("operator") or "eq"), "value": expected}
        return {}
    target = _mechanical_count_target_from_subject(subject, data)
    low = data.get("min")
    high = data.get("max")
    expected = _normalize_mechanical_expected_count(data.get("expected", data.get("value")))
    if low is None and high is None and isinstance(expected, list | tuple) and len(expected) == 2:
        low = expected[0]
        high = expected[1]
        expected = None
    if low is not None or high is not None:
        return {**target, "op": "between", "min": low if low is not None else expected, "max": high if high is not None else expected}
    if kind in {"count_at_most", "count_lte"}:
        return {**target, "op": "lte", "value": expected}
    if kind in {"count_at_least", "count_gte"}:
        return {**target, "op": "gte", "value": expected}
    return {**target, "op": "eq", "value": expected}


def _normalize_mechanical_operator(value: Any) -> str:
    op = _text(value or "eq").lower()
    if op in {"=", "==", "equals", "equal", "exact", "exactly", "is", "count_equals", "count_eq"}:
        return "eq"
    if op in {"<=", "max", "at_most", "lte", "less_than_or_equal"}:
        return "lte"
    if op in {">=", "min", "at_least", "gte", "greater_than_or_equal"}:
        return "gte"
    if op in {"range", "bounded", "between", "within"}:
        return "between"
    return op


def _normalize_mechanical_expected_count(value: Any) -> Any:
    if isinstance(value, dict):
        numbers = [_coerce_int(item, default=-1) for item in value.values()]
        if numbers and all(item >= 0 for item in numbers):
            return sum(numbers)
    return value


def _should_downgrade_loose_mechanical_check(value: Any, exc: ValueError) -> bool:
    message = str(exc)
    if "unsupported loose non-count mechanical_check.type" in message:
        return isinstance(value, dict)
    if "unsupported mechanical_check.type:" in message and isinstance(value, dict):
        raw_type = _text(value.get("type") or value.get("check") or value.get("predicate") or value.get("metric")).lower()
        return bool(raw_type) and (
            not re.fullmatch(r"[a-z_][a-z0-9_]*", raw_type)
            or "count(" in raw_type
            or "==" in raw_type
            or " and " in raw_type
        )
    if "unsupported mechanical_check count target" not in message:
        return False
    if isinstance(value, str):
        return True
    if not isinstance(value, dict):
        return False
    kind = _text(value.get("kind") or value.get("operator")).lower()
    predicate = _text(value.get("predicate")).lower()
    explicit_type = _text(value.get("type") or value.get("check") or value.get("metric")).lower()
    loose_count_types = {
        "count",
        "count_equals",
        "count_eq",
        "exact_count",
        "count_at_most",
        "count_lte",
        "count_at_least",
        "count_gte",
        "count_between",
    }
    if explicit_type and explicit_type not in loose_count_types:
        return False
    return kind in {"exact_count", "count", "count_eq", "bounded_count", "range_count"} or predicate in loose_count_types or explicit_type in loose_count_types


def _mechanical_check_from_text(value: str) -> dict[str, Any]:
    text = _text(value).lower()
    if not text:
        return {}
    normalized = re.sub(r"\s+", " ", text.replace("-", "_")).strip()
    between_match = re.fullmatch(r"count\(\s*([a-z0-9_ ]+)\s*\)\s+between\s+(\d+)\s+and\s+(\d+)", normalized)
    if between_match:
        target = _mechanical_count_target(between_match.group(1))
        return {**target, "op": "between", "min": int(between_match.group(2)), "max": int(between_match.group(3))}
    comparison_match = re.fullmatch(r"count\(\s*([a-z0-9_ ]+)\s*\)\s*(==|=|<=|>=)\s*(\d+)", normalized)
    if comparison_match:
        op = {"==": "eq", "=": "eq", "<=": "lte", ">=": "gte"}[comparison_match.group(2)]
        target = _mechanical_count_target(comparison_match.group(1))
        return {**target, "op": op, "value": int(comparison_match.group(3))}
    raise ValueError("mechanical_check string must use count(<supported-target>) == N, <= N, >= N, or between LOW and HIGH")


def _mechanical_count_target(value: str) -> dict[str, Any]:
    target = re.sub(r"[^a-z0-9_]+", "_", _text(value).lower()).strip("_")
    target = re.sub(r"_+", "_", target)
    result = _MECHANICAL_COUNT_TARGETS.get(target)
    if result is None:
        supported = ", ".join(sorted(_MECHANICAL_COUNT_TARGETS))
        raise ValueError(f"unsupported mechanical_check count target: {target}; supported: {supported}")
    return dict(result)


def _mechanical_count_target_from_subject(subject: str, data: dict[str, Any]) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", _text(subject).lower().replace("-", "_")).strip()
    breakdown = data.get("breakdown")
    if isinstance(breakdown, dict):
        keys = {re.sub(r"[^a-z0-9_]+", "_", _text(key).lower()).strip("_") for key in breakdown}
        if {"prelude", "implementation", "join"}.issubset(keys):
            return {"type": "module_count"}
    if "acceptance" in text and ("criteria" in text or "criterion" in text):
        return {"type": "acceptance_count"}
    if "milestone" in text:
        if "implementation" in text or "internal" in text:
            return {"type": "implementation_milestone_count"}
        return {"type": "milestone_count"}
    if "module" in text:
        if "prelude" in text:
            return {"type": "module_kind_count", "module_kind": "prelude"}
        if "join" in text or "final verification" in text:
            return {"type": "module_kind_count", "module_kind": "join"}
        if "implementation" in text and "total" not in text and "plan module" not in text:
            return {"type": "implementation_module_count"}
        return {"type": "module_count"}
    return _mechanical_count_target(text)


def _mechanical_count_target_from_path(path: str) -> dict[str, Any]:
    normalized = re.sub(r"[^a-z0-9_]+", ".", _text(path).lower().replace("-", "_")).strip(".")
    segments = [segment for segment in normalized.split(".") if segment]
    segment_set = set(segments)
    if not segments:
        raise ValueError("mechanical_check.path is empty")
    if "acceptance" in segment_set or "acceptance_criteria" in segment_set or "criteria" in segment_set:
        return {"type": "acceptance_count"}
    if "milestone" in segment_set or "milestones" in segment_set:
        if "implementation" in segment_set or "internal" in segment_set:
            return {"type": "implementation_milestone_count"}
        return {"type": "milestone_count"}
    if "module" in segment_set or "modules" in segment_set:
        if "prelude" in segment_set:
            return {"type": "module_kind_count", "module_kind": "prelude"}
        if "join" in segment_set or "final" in segment_set or "final_verification" in segment_set:
            return {"type": "module_kind_count", "module_kind": "join"}
        if "implementation" in segment_set:
            return {"type": "implementation_module_count"}
        return {"type": "module_count"}
    raise ValueError(f"unsupported mechanical_check.path: {path}")


def _all_gate_checks(state: dict[str, Any]) -> list[dict[str, Any]]:
    return list((dict(state.get("gate_contract") or {}).get("checks") or []))


def _gate_checks(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in _all_gate_checks(state) if isinstance(item, dict) and not bool(item.get("deleted"))]


def _compiled_gate_contract(state: dict[str, Any]) -> dict[str, Any]:
    checks = _gate_checks(state)
    if not checks:
        return {}
    return {"checks": [_drop_empty_gate_check(check) for check in checks]}


def _gate_check_coverage_projection(state: dict[str, Any]) -> list[dict[str, Any]]:
    checks = _gate_checks(state)
    if not checks:
        return []
    projection: dict[str, dict[str, Any]] = {}
    for check in checks:
        ref = _text(check.get("ref") or f"gate:{int(check.get('index') or 0)}")
        if not ref:
            continue
        projection[ref] = {
            key: value
            for key, value in {
                "ref": ref,
                "claim": _text(check.get("claim")),
                "source_ref": _text(check.get("source_ref")),
                "priority": _text(check.get("priority")),
                "kind": _text(check.get("kind")),
                "evidence": [],
            }.items()
            if value not in ("", [], {}, None)
        }
    if not projection:
        return []
    for node in _iter_plan_nodes(state):
        fields = dict(node.get("fields") or {})
        refs = _gate_check_ref_list(fields.get("gate_check_refs"))
        if not refs:
            continue
        evidence = {
            key: value
            for key, value in {
                "node_kind": _text(node.get("node_kind")),
                "handle": _text(node.get("handle")),
                "path": _text(node.get("path")),
                "summary": _text(node.get("summary")),
                "module_id": _text(node.get("module_id")),
                "milestone_id": _text(node.get("milestone_id")),
                "acceptance_id": _text(node.get("acceptance_id")),
            }.items()
            if value not in ("", [], {}, None)
        }
        if not evidence:
            continue
        for ref in refs:
            item = projection.get(ref)
            if item is None:
                continue
            entries = list(item.get("evidence") or [])
            if evidence not in entries:
                entries.append(evidence)
            item["evidence"] = entries
    return [item for item in projection.values()]


def _drop_empty_gate_check(check: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(check).items()
        if key != "deleted" and value not in ("", [], {}, None)
    }


def _gate_check_index(args: dict[str, Any]) -> int:
    if "check_index" in args and args.get("check_index") is not None:
        return _coerce_int(args.get("check_index"), default=-1)
    ref = _text(args.get("check_ref"))
    if ref.startswith("gate:"):
        return _coerce_int(ref.split(":", 1)[1], default=-1)
    if ref.isdigit():
        return int(ref)
    raise ValueError("check_index or check_ref is required")


def _gate_check_ref_list(value: Any) -> list[str]:
    refs: list[str] = []
    raw_items = value if isinstance(value, (list, tuple, set)) else ([] if value is None else [value])
    for raw in raw_items:
        text = _text(raw)
        if not text:
            continue
        if text.startswith("gate:"):
            ref = text
        elif text.isdigit():
            ref = f"gate:{int(text)}"
        else:
            ref = text
        if ref not in refs:
            refs.append(ref)
    return refs


def _known_gate_check_refs(state: dict[str, Any], value: Any) -> list[str]:
    refs = _gate_check_ref_list(value)
    active = {f"gate:{int(item.get('index') or 0)}" for item in _gate_checks(state)}
    for ref in refs:
        if ref not in active:
            raise ValueError(f"unknown gate_check_ref: {ref}")
    return refs


def _assert_gate_check_mutable(state: dict[str, Any], index: int) -> None:
    ref = f"gate:{int(index)}"
    if ref in set(_string_list(state.get("locked_gate_check_refs"))):
        raise ValueError(
            f"{ref} comes from the source gate_contract and is immutable; "
            "link plan nodes to it or add a new planner-local gate check instead of updating/deleting it"
        )


def _gate_check_public_refs(value: Any) -> list[str]:
    return _gate_check_ref_list(value)


def _gate_check_coverage(state: dict[str, Any]) -> set[str]:
    covered: set[str] = set()
    for item in list(state.get("constraints") or []):
        covered.update(_gate_check_ref_list(item.get("gate_check_refs")))
    for item in list(state.get("design_decisions") or []):
        covered.update(_gate_check_ref_list(item.get("gate_check_refs")))
    for module in list(state.get("modules") or []):
        covered.update(_gate_check_ref_list(module.get("gate_check_refs")))
        for milestone in list(module.get("internal_milestones") or []):
            covered.update(_gate_check_ref_list(milestone.get("gate_check_refs")))
            for ac in list(milestone.get("acceptance") or []):
                covered.update(_gate_check_ref_list(ac.get("gate_check_refs")))
    return covered


def _gate_check_reference_errors(state: dict[str, Any]) -> list[str]:
    active = {f"gate:{int(item.get('index') or 0)}" for item in _gate_checks(state)}
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()

    def visit(location: str, refs_value: Any) -> None:
        for ref in _gate_check_ref_list(refs_value):
            if ref in active:
                continue
            key = (location, ref)
            if key in seen:
                continue
            seen.add(key)
            errors.append(f"{location} references unknown gate_check_ref {ref}")

    for item in list(state.get("constraints") or []):
        visit(f"constraint {item.get('id') or item.get('handle')}", item.get("gate_check_refs"))
    for item in list(state.get("design_decisions") or []):
        visit(f"decision {item.get('id') or item.get('handle')}", item.get("gate_check_refs"))
    for module in list(state.get("modules") or []):
        module_label = f"module {module.get('module_id') or module.get('handle')}"
        visit(module_label, module.get("gate_check_refs"))
        for milestone in list(module.get("internal_milestones") or []):
            milestone_label = f"milestone {milestone.get('milestone_id') or milestone.get('handle')}"
            visit(milestone_label, milestone.get("gate_check_refs"))
            for ac in list(milestone.get("acceptance") or []):
                visit(f"acceptance {ac.get('id') or ac.get('handle')}", ac.get("gate_check_refs"))
    return errors


def _evaluate_mechanical_gate_check(state: dict[str, Any], check: dict[str, Any]) -> str:
    predicate = dict(check.get("mechanical_check") or {})
    check_type = _text(predicate.get("type")).lower()
    if not check_type:
        return "mechanical_check is missing"
    modules = list(state.get("modules") or [])
    count: int
    if check_type in {"module_count", "module_kind_count", "implementation_module_count"}:
        module_kind = _text(predicate.get("module_kind"))
        if check_type == "implementation_module_count":
            module_kind = "module"
        selected = [module for module in modules if not module_kind or _text(module.get("kind")) == module_kind]
        count = len(selected)
    elif check_type in {"milestone_count", "module_milestone_count", "implementation_milestone_count"}:
        module_kind = _text(predicate.get("module_kind"))
        if check_type == "implementation_milestone_count":
            module_kind = "module"
        module_id = _text(predicate.get("module_id"))
        selected_modules = [
            module
            for module in modules
            if (not module_kind or _text(module.get("kind")) == module_kind)
            and (not module_id or _text(module.get("module_id")) == module_id)
        ]
        count = sum(len(list(module.get("internal_milestones") or [])) for module in selected_modules)
    elif check_type == "acceptance_count":
        count = sum(
            len(list(milestone.get("acceptance") or []))
            for module in modules
            for milestone in list(module.get("internal_milestones") or [])
        )
    else:
        return f"unknown mechanical_check.type: {check_type}"
    return _mechanical_count_failure(count, predicate)


def _mechanical_count_failure(count: int, predicate: dict[str, Any]) -> str:
    op = _text(predicate.get("op") or "eq").lower()
    value = _coerce_int(predicate.get("value"), default=0)
    if op == "eq" and count != value:
        return f"expected count == {value}, got {count}"
    if op == "lte" and count > value:
        return f"expected count <= {value}, got {count}"
    if op == "gte" and count < value:
        return f"expected count >= {value}, got {count}"
    if op == "between":
        low = _coerce_int(predicate.get("min"), default=value)
        high = _coerce_int(predicate.get("max"), default=value)
        if count < low or count > high:
            return f"expected {low} <= count <= {high}, got {count}"
    return ""


def _reject_unknown_args(args: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(str(key) for key in args if str(key) not in allowed)
    if unknown:
        raise ValueError(f"unknown argument(s): {', '.join(unknown)}")


def _required(args: dict[str, Any], key: str) -> str:
    value = _text(args.get(key))
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _text(value: Any) -> str:
    return str(value or "").strip()


def _workflow_next_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        profile = _text(value)
        return {"profile": profile} if profile else {}
    if not isinstance(value, dict):
        return {}
    profile = _text(value.get("profile") or value.get("next_profile"))
    if not profile and isinstance(value.get("next"), dict):
        profile = _text(dict(value.get("next") or {}).get("profile"))
    if not profile:
        return {}
    result: dict[str, Any] = {"profile": profile}
    for key in ("adapter", "artifact_type", "reason"):
        text = _text(value.get(key))
        if text:
            result[key] = text
    return result


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list | tuple):
        return []
    result: list[str] = []
    for item in value:
        text = _text(item)
        if text:
            result.append(text)
    return result


def _dedupe_strings(value: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in _string_list(value):
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _join_sentences(first: str, second: str) -> str:
    left = _text(first)
    right = _text(second)
    if not left:
        return right
    if not right:
        return left
    return f"{left.rstrip('.')} ; {right}"


def _merged_module_interfaces(first: Any, second: Any, *, module_handle: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in [*list(first or []), *list(second or [])]:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item["module_handle"] = module_handle
        if _text(item.get("producer")) and _text(item.get("producer")).startswith("module_"):
            item["producer"] = module_handle
        if _text(item.get("consumer")) and _text(item.get("consumer")).startswith("module_"):
            item["consumer"] = module_handle
        identity = json.dumps(
            {
                key: value
                for key, value in item.items()
                if key not in {"handle", "module_handle", "producer", "consumer"}
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if identity in seen:
            continue
        seen.add(identity)
        result.append(item)
    return result


def _normalize_language_ids(value: Any) -> list[str]:
    result = _string_list(value)
    if not result:
        return []
    canonical = {str(item) for item in planner_requirements().get("canonical_language_ids") or []}
    aliases = {
        "py": "python",
        "python3": "python",
        "c++": "cpp",
        "cc": "cpp",
        "cxx": "cpp",
        "objective-c": "objc",
        "objective-c++": "objcpp",
        "ts": "typescript",
        "js": "javascript",
        "bash": "shell",
        "sh": "shell",
        "yml": "yaml",
    }
    normalized: list[str] = []
    unknown: list[str] = []
    for item in result:
        language = aliases.get(item.strip().lower(), item.strip().lower())
        if language not in canonical:
            unknown.append(item)
            continue
        if language not in normalized:
            normalized.append(language)
    if unknown:
        raise ValueError(
            "unknown implementation language id(s): "
            + ", ".join(unknown)
            + "; use canonical ids such as "
            + ", ".join(sorted(canonical))
        )
    return normalized


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list | tuple):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _acceptance_criteria_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list | tuple):
        return []
    result: list[dict[str, Any]] = []
    for raw in value:
        if isinstance(raw, str):
            criterion = _text(raw)
            if criterion:
                result.append(_acceptance_criterion_defaults({"criterion": criterion}))
            continue
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        criterion = _text(
            item.get("criterion")
            or item.get("description")
            or item.get("summary")
            or item.get("text")
            or item.get("acceptance")
        )
        if criterion:
            item["criterion"] = criterion
        result.append(_acceptance_criterion_defaults(item))
    return result


def _module_acceptance_criteria_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list | tuple):
        return []
    result: list[dict[str, Any]] = []
    for raw in value:
        if isinstance(raw, str):
            criterion = _text(raw)
            if criterion:
                result.append({"criterion": criterion})
            continue
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        criterion = _text(
            item.get("criterion")
            or item.get("description")
            or item.get("summary")
            or item.get("text")
            or item.get("acceptance")
        )
        if criterion:
            item["criterion"] = criterion
            result.append(item)
    return result


def _module_acceptance_criterion_text(
    raw: dict[str, Any],
    *,
    constraint_handles: list[str],
    gate_check_refs: list[str],
) -> str:
    criterion = _required(raw, "criterion")
    details: list[str] = []
    evidence = _text(raw.get("evidence_expectation"))
    if evidence:
        details.append(f"Evidence: {evidence}")
    negative_cases = _string_list(raw.get("negative_cases"))
    if negative_cases:
        details.append("Negative/boundary cases: " + "; ".join(negative_cases))
    quantifier = _text(raw.get("quantifier"))
    if quantifier:
        details.append(f"Quantifier: {quantifier}")
    if constraint_handles:
        details.append("Constraint handles: " + ", ".join(constraint_handles))
    if gate_check_refs:
        details.append("Gate refs: " + ", ".join(gate_check_refs))
    if not details:
        return criterion
    return f"{criterion} | {' | '.join(details)}"


def _module_acceptance_criterion_detail(
    raw: dict[str, Any],
    *,
    constraint_handles: list[str],
    gate_check_refs: list[str],
) -> dict[str, Any]:
    criterion = _required(raw, "criterion")
    detail: dict[str, Any] = {"criterion": criterion}
    evidence = _text(raw.get("evidence_expectation") or raw.get("evidence") or raw.get("done_when"))
    if evidence:
        detail["evidence_expectation"] = evidence
    negative_cases = _string_list(raw.get("negative_cases"))
    if negative_cases:
        detail["negative_cases"] = negative_cases
    quantifier = _text(raw.get("quantifier"))
    if quantifier:
        detail["quantifier"] = quantifier
    if constraint_handles:
        detail["linked_constraint_handles"] = list(constraint_handles)
    if gate_check_refs:
        detail["gate_check_refs"] = list(gate_check_refs)
    source_refs = _acceptance_source_refs(raw)
    if source_refs:
        detail["source_refs"] = source_refs
    return detail if len(detail) > 1 else {}


def _module_quality_criteria_details_payload(state: dict[str, Any], module: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in _dict_list(module.get("module_quality_criteria_details")):
        criterion = _text(raw.get("criterion"))
        if not criterion:
            continue
        item: dict[str, Any] = {"criterion": criterion}
        evidence = _text(raw.get("evidence_expectation") or raw.get("evidence") or raw.get("done_when"))
        if evidence:
            item["evidence_expectation"] = evidence
        negative_cases = _string_list(raw.get("negative_cases"))
        if negative_cases:
            item["negative_cases"] = negative_cases
        quantifier = _text(raw.get("quantifier"))
        if quantifier:
            item["quantifier"] = quantifier
        constraint_refs = _public_refs(state, raw.get("linked_constraint_handles")) or _string_list(raw.get("constraint_refs"))
        if constraint_refs:
            item["constraint_refs"] = constraint_refs
        gate_check_refs = _gate_check_public_refs(raw.get("gate_check_refs"))
        if gate_check_refs:
            item["gate_check_refs"] = gate_check_refs
        source_refs = _acceptance_source_refs(raw)
        if source_refs:
            item["source_refs"] = source_refs
        result.append(item)
    return result


def _state_module_quality_criteria_details(value: Any, constraint_by_public: dict[str, str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in _dict_list(value):
        criterion = _text(raw.get("criterion"))
        if not criterion:
            continue
        item: dict[str, Any] = {"criterion": criterion}
        evidence = _text(raw.get("evidence_expectation") or raw.get("evidence") or raw.get("done_when"))
        if evidence:
            item["evidence_expectation"] = evidence
        negative_cases = _string_list(raw.get("negative_cases"))
        if negative_cases:
            item["negative_cases"] = negative_cases
        quantifier = _text(raw.get("quantifier"))
        if quantifier:
            item["quantifier"] = quantifier
        constraint_handles = _string_list(raw.get("linked_constraint_handles")) or _handles_from_public_refs(
            raw.get("constraint_refs") or raw.get("linked_constraint_refs"),
            constraint_by_public,
        )
        if constraint_handles:
            item["linked_constraint_handles"] = constraint_handles
        gate_check_refs = _gate_check_ref_list(raw.get("gate_check_refs"))
        if gate_check_refs:
            item["gate_check_refs"] = gate_check_refs
        source_refs = _acceptance_source_refs(raw)
        if source_refs:
            item["source_refs"] = source_refs
        result.append(item)
    return result


def _acceptance_source_refs(raw: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    refs.extend(_string_list(raw.get("source_refs")))
    refs.extend(_string_list(raw.get("reference_refs")))
    source_ref = _text(raw.get("source_ref"))
    if source_ref:
        refs.append(source_ref)
    return _dedupe_strings(refs)


def _acceptance_criterion_defaults(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    criterion = _text(result.get("criterion"))
    evidence = _text(result.get("evidence_expectation") or result.get("evidence") or result.get("done_when"))
    if criterion and not evidence:
        evidence = "Focused tests, inspection, or review evidence demonstrates this criterion."
    result["criterion"] = criterion
    result["evidence_expectation"] = evidence
    if "negative_cases" not in result:
        result["negative_cases"] = _default_negative_cases_for_acceptance(criterion, evidence)
    return result


def _default_negative_cases_for_acceptance(criterion: str, evidence: str) -> list[str]:
    if _acceptance_requires_negative_cases(criterion, evidence):
        return ["Concrete invalid, empty, error, or boundary input is covered by focused tests or review evidence."]
    return []


def _module_outline_interfaces(args: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw in _dict_list(args.get("interfaces")):
        items.append(dict(raw))
    for direction, key in (("provided", "provided_interfaces"), ("consumed", "consumed_interfaces")):
        for raw in _dict_list(args.get(key)):
            item = dict(raw)
            item["direction"] = direction
            items.append(item)
    return items


def _safe_id(value: Any, *, default_prefix: str) -> str:
    raw = _text(value)
    if not raw:
        return new_work_id(default_prefix)
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", raw).strip("_")
    if not safe:
        return new_work_id(default_prefix)
    if safe[0].isdigit():
        safe = f"{default_prefix}_{safe}"
    return safe.lower()


def _explicit_module_id(value: Any) -> str:
    raw = _text(value)
    if not raw:
        raise ValueError("module_name is required")
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", raw).strip("_").lower()
    if not safe:
        raise ValueError("module_name must contain letters, numbers, or underscores")
    if safe[0].isdigit():
        raise ValueError("module_name must start with a letter or underscore after normalization")
    return safe


def _acceptance_requires_negative_cases(criterion: str, evidence: str) -> bool:
    text = f"{criterion}\n{evidence}".lower()
    indicators = (
        "raises",
        "raise ",
        "reject",
        "invalid",
        "blank",
        "empty",
        "non-string",
        "non string",
        "none input",
        "missing",
        "extra key",
        "unknown",
        "malformed",
        "fallback",
        "default case",
        "out-of-range",
        "out of range",
        "typeerror",
        "valueerror",
    )
    return any(indicator in text for indicator in indicators)


def _validate_acceptance_consistency(milestone: dict[str, Any]) -> None:
    conflicts = _acceptance_consistency_conflicts(_dict_list(milestone.get("acceptance")))
    if conflicts:
        title = _text(milestone.get("title") or milestone.get("milestone_id") or milestone.get("handle") or "milestone")
        raise ValueError(f"acceptance criteria conflict in {title}: " + "; ".join(conflicts))


def _acceptance_consistency_conflicts(items: list[dict[str, Any]]) -> list[str]:
    order_preserving: list[tuple[str, str, set[str]]] = []
    order_independent: list[tuple[str, str, set[str]]] = []
    for index, item in enumerate(items):
        public_id = _text(item.get("id") or f"AC-{index + 1}")
        text = _acceptance_contract_text(item)
        domains = _acceptance_domain_tags(text)
        preserves_order = _mentions_input_order_preservation(text)
        ignores_order = _mentions_order_independent_determinism(text)
        if preserves_order and ignores_order:
            return [
                (
                    f"{public_id} requires preserving input order while also requiring byte-identical or deterministic "
                    "output regardless of input order. Choose one ordering contract or state a deterministic tie-breaker "
                    "instead of input-order preservation."
                )
            ]
        if preserves_order:
            order_preserving.append((public_id, text, domains))
        if ignores_order:
            order_independent.append((public_id, text, domains))

    conflicts: list[str] = []
    for preserve_id, _preserve_text, preserve_domains in order_preserving:
        for independent_id, _independent_text, independent_domains in order_independent:
            if preserve_id == independent_id:
                continue
            if preserve_domains and independent_domains and preserve_domains.isdisjoint(independent_domains):
                continue
            conflicts.append(
                (
                    f"{preserve_id} requires preserving input order, but {independent_id} requires byte-identical or "
                    "deterministic output regardless of input order. Replace one requirement with a single explicit "
                    "ordering rule, such as sorting bullets/messages within each version+label."
                )
            )
    return conflicts


def _acceptance_contract_text(item: dict[str, Any]) -> str:
    parts = [
        _text(item.get("criterion")),
        _text(item.get("evidence_expectation")),
        _text(item.get("quantifier")),
        *[_text(value) for value in _string_list(item.get("negative_cases"))],
    ]
    return _normalized_contract_text("\n".join(part for part in parts if part))


def _normalized_contract_text(value: str) -> str:
    text = value.lower()
    text = text.replace("‑", "-").replace("–", "-").replace("—", "-")
    text = re.sub(r"[`*_]+", " ", text)
    text = re.sub(r"[-/]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _mentions_input_order_preservation(text: str) -> bool:
    phrases = (
        "stable input order",
        "preserve input order",
        "preserves input order",
        "preserving input order",
        "preserved input order",
        "input order is preserved",
        "original input order",
        "first seen order",
        "first encounter order",
        "in the order entries are supplied",
        "in the order records are supplied",
        "same order supplied",
    )
    return any(phrase in text for phrase in phrases)


def _mentions_order_independent_determinism(text: str) -> bool:
    order_independent = (
        "regardless of input order",
        "independent of input order",
        "input order independent",
        "input order changes but output must not",
        "order entries are supplied",
        "order records are supplied",
        "shuffled input",
        "permuted input",
        "same entry set",
        "same record set",
    )
    deterministic_output = (
        "byte identical",
        "byte for byte",
        "deterministic",
        "same output",
        "stable output",
        "output must not",
        "same markdown",
        "rendered markdown",
    )
    return any(phrase in text for phrase in order_independent) and any(phrase in text for phrase in deterministic_output)


def _acceptance_domain_tags(text: str) -> set[str]:
    tags: set[str] = set()
    if any(token in text for token in ("render", "markdown", "release notes", "bullet", "message")):
        tags.add("rendering")
    if any(token in text for token in ("parse", "record", "validator", "validation")):
        tags.add("parsing")
    if any(token in text for token in ("group", "bucket", "entry list")):
        tags.add("grouping")
    if any(token in text for token in ("summary", "count", "aggregate")):
        tags.add("summary")
    return tags


def _public_id(prefix: str, index: int) -> str:
    return f"{prefix}-{index}"


def _strength(value: Any, *, default: str) -> str:
    text = _text(value or default).lower()
    if text not in {"hard_contract", "chosen_contract", "preference", "out_of_scope"}:
        raise ValueError("strength must be hard_contract, chosen_contract, preference, or out_of_scope")
    return text


def _module_kind(value: Any) -> str:
    text = _text(value or "module").lower()
    if text not in {"prelude", "module", "join"}:
        raise ValueError("kind must be prelude, module, or join")
    return text


def _coerce_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _assert_editable_plan(state: dict[str, Any]) -> None:
    if bool(state.get("closed")):
        raise ValueError("plan is finalized and cannot be edited")
    lifecycle = _text(state.get("lifecycle") or "editing")
    if lifecycle == "published":
        raise ValueError("published plan cannot be edited; check out a revision draft first")


def _assert_open_plan(state: dict[str, Any]) -> None:
    if bool(state.get("closed")):
        raise ValueError("plan is already finalized")


def _assert_open_module(module: dict[str, Any]) -> None:
    if bool(module.get("closed")):
        raise ValueError(
            "module is already closed for milestone edits; do not restart the whole plan. "
            "Use the returned plan_handle to add the next module, use plan_add_module_interface with module_name for interface repair, "
            "or check out/update a revision if the module's milestone structure itself must change."
        )


def _assert_open_milestone(milestone: dict[str, Any]) -> None:
    if bool(milestone.get("closed")):
        raise ValueError(
            "milestone is already closed; use the parent module_handle/module_name to add another milestone, "
            "or update/revise the existing milestone instead of reopening it."
        )


def _open_module(state: dict[str, Any]) -> dict[str, Any] | None:
    for module in list(state.get("modules") or []):
        if not bool(module.get("closed")):
            return module
    return None


def _open_milestone(module: dict[str, Any]) -> dict[str, Any] | None:
    for milestone in list(module.get("internal_milestones") or []):
        if not bool(milestone.get("closed")):
            return milestone
    return None


def _find_module_by_handle(state: dict[str, Any], handle: str) -> dict[str, Any]:
    for module in list(state.get("modules") or []):
        if _text(module.get("handle")) == handle:
            return module
    raise ValueError(f"unknown module_handle: {handle}")


def _find_module_by_id(state: dict[str, Any], module_id: str) -> dict[str, Any]:
    normalized = _explicit_module_id(module_id)
    for module in list(state.get("modules") or []):
        if _text(module.get("module_id")) == normalized:
            return module
    raise ValueError(f"unknown module_name: {normalized}")


def _known_module_refs(state: dict[str, Any], refs: list[str]) -> list[str]:
    modules = list(state.get("modules") or [])
    by_handle = {_text(module.get("handle")): _text(module.get("handle")) for module in modules}
    by_id = {_text(module.get("module_id")): _text(module.get("handle")) for module in modules}
    result: list[str] = []
    for ref in refs:
        text = _text(ref)
        if not text:
            continue
        handle = by_handle.get(text)
        if not handle:
            try:
                handle = by_id.get(_explicit_module_id(text))
            except ValueError:
                handle = ""
        if not handle:
            raise ValueError(f"unknown module reference: {text}; pass an existing module_handle or module_name")
        result.append(handle)
    return _dedupe_strings(result)


def _known_handles(state: dict[str, Any], handles: list[str], *, expected_prefix: str) -> list[str]:
    known: set[str] = set()
    if expected_prefix == "constraint":
        known = {_text(item.get("handle")) for item in list(state.get("constraints") or [])}
    elif expected_prefix == "decision":
        known = {_text(item.get("handle")) for item in list(state.get("design_decisions") or [])}
    elif expected_prefix == "module":
        known = {_text(item.get("handle")) for item in list(state.get("modules") or [])}
    result: list[str] = []
    for handle in handles:
        if handle not in known:
            raise ValueError(f"unknown {expected_prefix}_handle: {handle}")
        result.append(handle)
    return result


def _constraint_referrers(state: dict[str, Any], constraint_handle: str) -> list[str]:
    referrers: list[str] = []
    handle = _text(constraint_handle)
    for decision in list(state.get("design_decisions") or []):
        if handle in _string_list(decision.get("linked_constraint_handles")):
            referrers.append(f"decision {decision.get('id') or decision.get('handle')}")
    for module in list(state.get("modules") or []):
        if handle in _string_list(module.get("constraint_handles")):
            referrers.append(f"module {module.get('module_id') or module.get('handle')}")
        for milestone in list(module.get("internal_milestones") or []):
            if handle in _string_list(milestone.get("constraint_handles")):
                referrers.append(f"milestone {milestone.get('milestone_id') or milestone.get('handle')}")
            for ac in list(milestone.get("acceptance") or []):
                if handle in _string_list(ac.get("linked_constraint_handles")):
                    referrers.append(f"acceptance {ac.get('id') or ac.get('handle')}")
    return referrers


def _decision_referrers(state: dict[str, Any], decision_handle: str) -> list[str]:
    referrers: list[str] = []
    handle = _text(decision_handle)
    for module in list(state.get("modules") or []):
        if handle in _string_list(module.get("decision_handles")):
            referrers.append(f"module {module.get('module_id') or module.get('handle')}")
        for milestone in list(module.get("internal_milestones") or []):
            if handle in _string_list(milestone.get("decision_handles")):
                referrers.append(f"milestone {milestone.get('milestone_id') or milestone.get('handle')}")
    return referrers


def _public_refs(state: dict[str, Any], handles: Any) -> list[str]:
    mapping: dict[str, str] = {}
    for item in list(state.get("constraints") or []):
        mapping[_text(item.get("handle"))] = _text(item.get("id"))
    for item in list(state.get("design_decisions") or []):
        mapping[_text(item.get("handle"))] = _text(item.get("id"))
    result: list[str] = []
    for handle in _string_list(handles):
        ref = mapping.get(handle)
        if ref:
            result.append(ref)
    return result


def _strip_handles(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in dict(item).items() if key != "handle" and value not in ("", [], {})}


def _public_interface_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in dict(item).items() if key not in {"handle", "direction"} and value not in ("", [], {})}


def _topological_order(nodes: list[dict[str, Any]]) -> list[str]:
    node_ids = [_text(node.get("node_id")) for node in nodes]
    deps = {node_id: _string_list(node.get("depends_on")) for node_id, node in zip(node_ids, nodes)}
    incoming = {node_id: set(deps.get(node_id) or []) for node_id in node_ids}
    children: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for node_id, node_deps in incoming.items():
        for dep in node_deps:
            if dep not in incoming:
                raise ValueError(f"module dependency references unknown node: {dep}")
            children.setdefault(dep, []).append(node_id)
    ready = [node_id for node_id in node_ids if not incoming[node_id]]
    ordered: list[str] = []
    while ready:
        node_id = ready.pop(0)
        ordered.append(node_id)
        for child in [item for item in node_ids if item in children.get(node_id, [])]:
            incoming[child].discard(node_id)
            if not incoming[child] and child not in ordered and child not in ready:
                ready.append(child)
    if len(ordered) != len(node_ids):
        remaining = [node_id for node_id in node_ids if node_id not in ordered]
        raise ValueError("module dependency graph has a cycle involving " + ", ".join(remaining))
    return ordered
