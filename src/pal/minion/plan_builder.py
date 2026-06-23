from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.work_order import dispatchable_plan_validation, new_work_id, planner_requirements
from pal.minion.workspace_tools import _append_unique_artifact, _write_minion_artifact
from pal.shared import RuntimeStatus


PLAN_BUILDER_CAPABILITIES: tuple[str, ...] = (
    "op_minion_plan_begin",
    "op_minion_plan_add_constraint",
    "op_minion_plan_add_design_decision",
    "op_minion_plan_begin_module",
    "op_minion_plan_add_module_interface",
    "op_minion_plan_begin_milestone",
    "op_minion_plan_add_acceptance_criterion",
    "op_minion_plan_end_milestone",
    "op_minion_plan_end_module",
    "op_minion_plan_finalize",
)


PLAN_BUILDER_TOOL_SPECS: dict[str, dict[str, Any]] = {
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
            },
        },
    },
    "op_minion_plan_add_constraint": {
        "name": "op_minion_plan_add_constraint",
        "description": (
            "Record a spec-derived constraint before planning modules. Mark hard/chosen contracts explicitly so "
            "milestones and acceptance criteria can trace to them."
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
                "global_only": {"type": "boolean", "description": "True only when no module/milestone can own the constraint."},
            },
            "required": ["plan_handle", "statement"],
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
            },
            "required": ["plan_handle", "decision"],
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
                "module_key": {
                    "type": "string",
                    "description": (
                        "Required stable, human-readable module id/name. Use snake_case names such as "
                        "module_setup_contracts, module_slug_tools, or module_final_verification; Pal will not invent one."
                    ),
                },
                "kind": {"type": "string", "enum": ["prelude", "module", "join"], "default": "module"},
                "depends_on_module_handles": {"type": "array", "items": {"type": "string"}},
                "responsibility": {"type": "string"},
                "owned_area": {"type": "array", "items": {"type": "string"}},
                "scope_guard": {"type": "string"},
                "constraint_handles": {"type": "array", "items": {"type": "string"}},
                "decision_handles": {"type": "array", "items": {"type": "string"}},
                "languages": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["plan_handle", "module_key", "responsibility", "owned_area"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_add_module_interface": {
        "name": "op_minion_plan_add_module_interface",
        "description": "Attach a provided or consumed module interface with data shape, lifecycle, ownership, error behavior, and compatibility.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "module_handle": {"type": "string"},
                "direction": {"type": "string", "enum": ["provided", "consumed"]},
                "name": {"type": "string"},
                "shape": {"type": "string"},
                "lifecycle": {"type": "string"},
                "ownership": {"type": "string"},
                "error_behavior": {"type": "string"},
                "compatibility": {"type": "string"},
                "producer": {"type": "string"},
                "consumer": {"type": "string"},
            },
            "required": ["module_handle", "direction", "name", "shape", "lifecycle", "ownership", "error_behavior", "compatibility"],
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
                "title": {"type": "string"},
                "task": {"type": "string"},
                "scope_guard": {"type": "string"},
                "changed_area": {"type": "array", "items": {"type": "string"}},
                "constraint_handles": {"type": "array", "items": {"type": "string"}},
                "decision_handles": {"type": "array", "items": {"type": "string"}},
                "languages": {"type": "array", "items": {"type": "string"}},
                "tests_required": {"type": "array", "items": {"type": "string"}},
                "public_api_added": {"type": "string"},
            },
            "required": ["module_handle", "title", "task"],
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
            "properties": {"module_handle": {"type": "string"}},
            "required": ["module_handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_plan_finalize": {
        "name": "op_minion_plan_finalize",
        "description": (
            "Compile the structured draft into the normal primary plan.json FinalPlanArtifact and register it for the "
            "existing plan_acceptance gate. Call this only after every module and milestone is closed."
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
        if name == "op_minion_plan_begin":
            return self._plan_begin(args)
        if name == "op_minion_plan_add_constraint":
            return self._add_constraint(args)
        if name == "op_minion_plan_add_design_decision":
            return self._add_design_decision(args)
        if name == "op_minion_plan_begin_module":
            return self._begin_module(args)
        if name == "op_minion_plan_add_module_interface":
            return self._add_module_interface(args)
        if name == "op_minion_plan_begin_milestone":
            return self._begin_milestone(args)
        if name == "op_minion_plan_add_acceptance_criterion":
            return self._add_acceptance_criterion(args)
        if name == "op_minion_plan_end_milestone":
            return self._end_milestone(args)
        if name == "op_minion_plan_end_module":
            return self._end_module(args)
        if name == "op_minion_plan_finalize":
            return self._finalize(args)
        raise ValueError(f"unknown plan builder tool: {name}")

    def _plan_begin(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"goal", "plan_id", "summary", "languages", "source_refs"})
        goal = _text(args.get("goal") or self.workspace.get("goal") or "")
        plan_id = _safe_id(args.get("plan_id"), default_prefix="plan")
        plan_handle = plan_id if plan_id.startswith("plan_") else f"plan_{plan_id}"
        state = {
            "plan_handle": plan_handle,
            "plan_id": plan_id,
            "task_id": _text(self.workspace.get("task_id")),
            "goal": goal,
            "summary": _text(args.get("summary") or goal),
            "languages": _normalize_language_ids(args.get("languages")),
            "source_refs": _string_list(args.get("source_refs")),
            "constraints": [],
            "design_decisions": [],
            "modules": [],
            "closed": False,
            "handle_counters": {"constraint": 0, "decision": 0, "module": 0, "milestone": 0, "ac": 0},
        }
        self._save_state(state)
        return {
            "text": f"Plan draft started: {plan_handle}",
            "structured": {"plan_handle": plan_handle, "plan_id": plan_id},
        }

    def _add_constraint(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"plan_handle", "statement", "kind", "strength", "source_ref", "rationale", "global_only"})
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
        }
        state["constraints"].append(item)
        self._save_state(state)
        return {"text": f"Constraint added: {handle}", "structured": {"plan_handle": state["plan_handle"], "constraint_handle": handle}}

    def _add_design_decision(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(
            args,
            {"plan_handle", "question", "decision", "strength", "rationale", "alternatives", "downstream_effect", "linked_constraint_handles"},
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
        }
        state["design_decisions"].append(item)
        self._save_state(state)
        return {"text": f"Design decision added: {handle}", "structured": {"plan_handle": state["plan_handle"], "decision_handle": handle}}

    def _begin_module(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(
            args,
            {
                "plan_handle",
                "module_key",
                "kind",
                "depends_on_module_handles",
                "responsibility",
                "owned_area",
                "scope_guard",
                "constraint_handles",
                "decision_handles",
                "languages",
            },
        )
        state = self._load_state(_required(args, "plan_handle"))
        _assert_open_plan(state)
        if _open_module(state):
            raise ValueError("close the open module before beginning another module")
        kind = _module_kind(args.get("kind"))
        dependencies = _known_handles(state, _string_list(args.get("depends_on_module_handles")), expected_prefix="module")
        for dependency in dependencies:
            module = _find_module_by_handle(state, dependency)
            if not module.get("closed"):
                raise ValueError(f"depends_on_module_handles includes an open module: {dependency}")
        module_id = _explicit_module_id(args.get("module_key"))
        if any(_text(module.get("module_id")) == module_id for module in state["modules"]):
            raise ValueError(f"module_key/module_id is duplicated: {module_id}")
        handle = self._next_handle(state, "module", "module")
        item = {
            "handle": handle,
            "module_id": module_id,
            "kind": kind,
            "depends_on_module_handles": dependencies,
            "responsibility": _required(args, "responsibility"),
            "owned_area": _string_list(args.get("owned_area")),
            "scope_guard": _text(args.get("scope_guard")),
            "constraint_handles": _known_handles(state, _string_list(args.get("constraint_handles")), expected_prefix="constraint"),
            "decision_handles": _known_handles(state, _string_list(args.get("decision_handles")), expected_prefix="decision"),
            "languages": _normalize_language_ids(args.get("languages")),
            "provided_interfaces": [],
            "consumed_interfaces": [],
            "internal_milestones": [],
            "closed": False,
        }
        if not item["owned_area"]:
            raise ValueError("owned_area must contain at least one path, package, component, or responsibility boundary")
        state["modules"].append(item)
        self._save_state(state)
        return {"text": f"Module opened: {handle}", "structured": {"module_handle": handle, "plan_handle": state["plan_handle"]}}

    def _add_module_interface(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(
            args,
            {"module_handle", "direction", "name", "shape", "lifecycle", "ownership", "error_behavior", "compatibility", "producer", "consumer"},
        )
        state, module = self._load_module(_required(args, "module_handle"))
        _assert_open_module(module)
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
        target = "provided_interfaces" if direction == "provided" else "consumed_interfaces"
        module[target].append(item)
        self._save_state(state)
        return {"text": f"Module interface added: {item['name']}", "structured": {"module_handle": module["handle"], "plan_handle": state["plan_handle"]}}

    def _begin_milestone(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(
            args,
            {
                "module_handle",
                "title",
                "task",
                "scope_guard",
                "changed_area",
                "constraint_handles",
                "decision_handles",
                "languages",
                "tests_required",
                "public_api_added",
            },
        )
        state, module = self._load_module(_required(args, "module_handle"))
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
            "changed_area": _string_list(args.get("changed_area")),
            "constraint_handles": _known_handles(state, _string_list(args.get("constraint_handles")), expected_prefix="constraint"),
            "decision_handles": _known_handles(state, _string_list(args.get("decision_handles")), expected_prefix="decision"),
            "languages": _normalize_language_ids(args.get("languages")),
            "tests_required": _string_list(args.get("tests_required")),
            "public_api_added": _text(args.get("public_api_added")),
            "acceptance": [],
            "closed": False,
        }
        module["internal_milestones"].append(item)
        self._save_state(state)
        return {"text": f"Milestone opened: {handle}", "structured": {"milestone_handle": handle, "module_handle": module["handle"]}}

    def _add_acceptance_criterion(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"milestone_handle", "criterion", "evidence_expectation", "linked_constraint_handles", "negative_cases", "quantifier"})
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
        return {"text": f"Milestone closed: {milestone['handle']}", "structured": {"module_handle": module["handle"], "plan_handle": state["plan_handle"]}}

    def _end_module(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"module_handle"})
        state, module = self._load_module(_required(args, "module_handle"))
        _assert_open_module(module)
        if _open_milestone(module):
            raise ValueError("close the open milestone before closing the module")
        if not module["internal_milestones"]:
            raise ValueError("module must have at least one milestone before closing")
        module["closed"] = True
        self._save_state(state)
        return {"text": f"Module closed: {module['handle']}", "structured": {"plan_handle": state["plan_handle"], "module_handle": module["handle"]}}

    def _finalize(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"plan_handle", "summary", "system_test_plan", "risks", "assumptions"})
        state = self._load_state(_required(args, "plan_handle"))
        _assert_open_plan(state)
        if _open_module(state):
            raise ValueError("close all modules before finalizing the plan")
        artifact = self._compile_artifact(state, args)
        validation = dispatchable_plan_validation(artifact)
        content = json.dumps({"type": "FinalPlanArtifact", **artifact}, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
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
        if kinds.count("prelude") != 1:
            raise ValueError("plan must contain exactly one prelude module")
        if kinds.count("join") != 1:
            raise ValueError("plan must contain exactly one join module")
        if "module" not in kinds:
            raise ValueError("plan must contain at least one implementation module")
        if not _string_list(state.get("languages")):
            raise ValueError("metadata.languages must contain at least one canonical implementation language id")
        self._validate_module_interfaces(state)
        self._validate_design_decisions(state)
        self._validate_constraint_coverage(state)
        module_payloads = [self._module_payload(state, module) for module in modules]
        topology = self._compile_topology(state)
        assumptions = _string_list(args.get("assumptions"))
        risks = _dict_list(args.get("risks"))
        if assumptions:
            risks = [*risks, *({"kind": "assumption", "summary": item} for item in assumptions)]
        metadata = {
            "languages": _normalize_language_ids(state.get("languages")),
            "source_refs": _string_list(state.get("source_refs")),
            "constraints": [_strip_handles(item) for item in state.get("constraints") or []],
            "design_decisions": [_strip_handles(item) for item in state.get("design_decisions") or []],
            "plan_builder": {"version": 1, "plan_handle": state["plan_handle"]},
        }
        return {
            "plan_id": _text(state.get("plan_id") or new_work_id("plan")),
            "task_id": task_id,
            "summary": _text(args.get("summary") or state.get("summary") or state.get("goal") or "Dispatchable plan."),
            "modules": module_payloads,
            "cross_module_contracts": self._cross_module_contracts(state),
            "orchestration": {
                "execution_shape": "fork_join_linear",
                "topology": topology,
                "coordination": "Pal manager dispatches closed module milestones through the validated plan topology.",
                "checkpoint_policy": "Each coder milestone produces a structured checkpoint for review before the next step.",
                "fallback_behavior": "If a gate fails, route reviewer findings into the repair/revision loop before continuing.",
            },
            "system_test_plan": _dict_list(args.get("system_test_plan")) or [{"level": "system", "evidence": "Run the full feature workflow or explain why dogfood is not possible."}],
            "risks": risks,
            "metadata": metadata,
        }

    def _module_payload(self, state: dict[str, Any], module: dict[str, Any]) -> dict[str, Any]:
        metadata = {
            "module_kind": module.get("kind"),
            "scope_guard": module.get("scope_guard"),
            "constraint_refs": _public_refs(state, module.get("constraint_handles")),
            "decision_refs": _public_refs(state, module.get("decision_handles")),
        }
        languages = _string_list(module.get("languages"))
        if languages:
            metadata["languages"] = languages
        return {
            "module_id": _text(module.get("module_id")),
            "owned_area": _string_list(module.get("owned_area")),
            "responsibility": _text(module.get("responsibility")),
            "provided_interfaces": [dict(item) for item in list(module.get("provided_interfaces") or [])],
            "consumed_interfaces": [dict(item) for item in list(module.get("consumed_interfaces") or [])],
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
            "acceptance_checklist": acceptance_checklist,
            "implementation_checklist": self._implementation_checklist(module, milestone, acceptance_checklist),
        }
        languages = _string_list(milestone.get("languages"))
        if languages:
            metadata["languages"] = languages
        public_api_added = _text(milestone.get("public_api_added"))
        if public_api_added:
            metadata["public_api_added"] = public_api_added
        return {
            "milestone_id": _text(milestone.get("milestone_id") or f"{module['module_id']}_m{index + 1}"),
            "title": _text(milestone.get("title")),
            "task": _text(milestone.get("task")),
            "acceptance_criteria": [_text(item.get("criterion")) for item in acceptance_items],
            "skill_refs": [],
            "test_plan": {"required": _string_list(milestone.get("tests_required"))},
            "metadata": {key: value for key, value in metadata.items() if value not in ("", [], {})},
        }

    def _module_test_plan(self, module: dict[str, Any]) -> dict[str, Any]:
        tests: list[str] = []
        for milestone in list(module.get("internal_milestones") or []):
            tests.extend(_string_list(milestone.get("tests_required")))
        return {"required": tests} if tests else {"module": ["Verify the module contract and failure behavior."]}

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
            nodes.append(
                {
                    "node_id": node_id,
                    "kind": _text(module.get("kind")).lower(),
                    "module_id": _text(module.get("module_id")),
                    "depends_on": depends_on,
                }
            )
        order = _topological_order(nodes)
        return {
            "nodes": [next(node for node in nodes if node["node_id"] == node_id) for node_id in order],
            "order": order,
        }

    def _cross_module_contracts(self, state: dict[str, Any]) -> list[dict[str, Any]]:
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

    def _load_module(self, module_handle: str) -> tuple[dict[str, Any], dict[str, Any]]:
        state = self._load_state_for_handle(module_handle)
        module = _find_module_by_handle(state, module_handle)
        return state, module

    def _load_milestone(self, milestone_handle: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        state = self._load_state_for_handle(milestone_handle)
        for module in list(state.get("modules") or []):
            for milestone in list(module.get("internal_milestones") or []):
                if _text(milestone.get("handle")) == milestone_handle:
                    return state, module, milestone
        raise ValueError(f"unknown milestone_handle: {milestone_handle}")

    def _load_state_for_handle(self, handle: str) -> dict[str, Any]:
        for state in self._iter_states():
            if _text(state.get("plan_handle")) == handle:
                return state
            if any(_text(module.get("handle")) == handle for module in list(state.get("modules") or [])):
                return state
            for module in list(state.get("modules") or []):
                if any(_text(milestone.get("handle")) == handle for milestone in list(module.get("internal_milestones") or [])):
                    return state
        raise ValueError(f"unknown plan builder handle: {handle}")

    def _load_state(self, plan_handle: str) -> dict[str, Any]:
        path = self._state_path(plan_handle)
        if not path.exists():
            raise ValueError(f"unknown plan_handle: {plan_handle}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_state(self, state: dict[str, Any]) -> None:
        path = self._state_path(_text(state.get("plan_handle")))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

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
        raise ValueError("module_key is required; provide a stable human-readable module id")
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", raw).strip("_").lower()
    if not safe:
        raise ValueError("module_key must contain letters, numbers, or underscores")
    if safe[0].isdigit():
        raise ValueError("module_key must start with a letter or underscore after normalization")
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


def _assert_open_plan(state: dict[str, Any]) -> None:
    if bool(state.get("closed")):
        raise ValueError("plan is already finalized")


def _assert_open_module(module: dict[str, Any]) -> None:
    if bool(module.get("closed")):
        raise ValueError("module is already closed")


def _assert_open_milestone(milestone: dict[str, Any]) -> None:
    if bool(milestone.get("closed")):
        raise ValueError("milestone is already closed")


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
