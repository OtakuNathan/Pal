from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.dag_advancer import dag_state_to_runtime_dict as _dag_state_to_runtime_dict
from pal.minion.ipc import MinionManagerClient
from pal.minion.repository import MinionTaskingRepository
from pal.minion.workspace_tools import _append_unique_artifact, _write_minion_artifact
from pal.shared import RuntimeStatus


REPAIR_BILL_BUILDER_CAPABILITIES: tuple[str, ...] = (
    "op_minion_repair_bill_begin",
    "op_minion_repair_bill_add_module_patch",
    "op_minion_repair_bill_add_module",
    "op_minion_repair_bill_add_acceptance_criteria_batch",
    "op_minion_repair_bill_add_evidence",
    "op_minion_repair_bill_validate",
    "op_minion_repair_bill_submit",
)

_DEFECT_KINDS = frozenset(
    {
        "module_defect",
        "contract_defect",
        "integration_defect",
        "architecture_defect",
        "triage_required",
    }
)


REPAIR_BILL_BUILDER_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_minion_repair_bill_begin": {
        "name": "op_minion_repair_bill_begin",
        "description": (
            "Start a structured repair bill against the current parent plan DAG. "
            "Pal loads the parent work order and accepts only existing module_name values; do not hand-write repair JSON."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "parent_work_order_id": {"type": "string", "description": "Parent plan work order id. Defaults to workspace.parent_work_order_id."},
                "source_module_id": {"type": "string", "description": "Reporting module id. Defaults to the current module binding."},
                "summary": {"type": "string"},
                "bill_handle": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    "op_minion_repair_bill_add_module_patch": {
        "name": "op_minion_repair_bill_add_module_patch",
        "description": (
            "Add or merge a repair patch for one existing module. This does not create a module; module_name must already exist "
            "in the parent DAG. Use defect_kind=module_defect or contract_defect to replay that module and downstream dependents. "
            "Use defect_kind=architecture_defect only when the current module split, dependency graph, or ownership boundary is wrong; "
            "that blocks the parent for plan review instead of replaying locally."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "bill_handle": {"type": "string"},
                "module_name": {"type": "string"},
                "defect_kind": {
                    "type": "string",
                    "enum": ["module_defect", "contract_defect", "integration_defect", "architecture_defect", "triage_required"],
                    "default": "module_defect",
                },
                "summary": {"type": "string"},
                "acceptance_criteria": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "object"}]}},
                "additional_acceptance_criteria": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "object"}]}},
                "negative_cases": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "object"}]}},
                "evidence": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "object"}]}},
            },
            "required": ["bill_handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_repair_bill_add_module": {
        "name": "op_minion_repair_bill_add_module",
        "description": "Alias for repair_bill_add_module_patch. It patches an existing module; it never adds a new DAG module.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "bill_handle": {"type": "string"},
                "module_name": {"type": "string"},
                "defect_kind": {
                    "type": "string",
                    "enum": ["module_defect", "contract_defect", "integration_defect", "architecture_defect", "triage_required"],
                    "default": "module_defect",
                },
                "summary": {"type": "string"},
                "acceptance_criteria": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "object"}]}},
                "additional_acceptance_criteria": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "object"}]}},
                "negative_cases": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "object"}]}},
                "evidence": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "object"}]}},
            },
            "required": ["bill_handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_repair_bill_add_acceptance_criteria_batch": {
        "name": "op_minion_repair_bill_add_acceptance_criteria_batch",
        "description": "Append additional acceptance criteria to one existing module patch in the current repair bill.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "bill_handle": {"type": "string"},
                "module_name": {"type": "string"},
                "criteria": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "object"}]}},
                "defect_kind": {
                    "type": "string",
                    "enum": ["module_defect", "contract_defect", "integration_defect", "architecture_defect", "triage_required"],
                    "default": "module_defect",
                },
            },
            "required": ["bill_handle", "criteria"],
            "additionalProperties": False,
        },
    },
    "op_minion_repair_bill_add_evidence": {
        "name": "op_minion_repair_bill_add_evidence",
        "description": "Append reproducer, failing command, artifact, or observation evidence to one existing module patch.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "bill_handle": {"type": "string"},
                "module_name": {"type": "string"},
                "evidence": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "object"}]}},
            },
            "required": ["bill_handle", "evidence"],
            "additionalProperties": False,
        },
    },
    "op_minion_repair_bill_validate": {
        "name": "op_minion_repair_bill_validate",
        "description": "Compile and validate the current repair bill without submitting it to the manager.",
        "parameters_schema": {
            "type": "object",
            "properties": {"bill_handle": {"type": "string"}},
            "required": ["bill_handle"],
            "additionalProperties": False,
        },
    },
    "op_minion_repair_bill_submit": {
        "name": "op_minion_repair_bill_submit",
        "description": (
            "Validate, write repair_bill.json as an artifact, then submit it to the manager. "
            "The manager owns DAG rollback, affected-module replay, and ready-module scheduling."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "bill_handle": {"type": "string"},
                "relative_path": {"type": "string", "default": "repair_bill.json"},
                "overwrite": {"type": "boolean", "default": True},
            },
            "required": ["bill_handle"],
            "additionalProperties": False,
        },
    },
}


@dataclass
class RepairBillBuilderRuntime:
    workspace: dict[str, Any]
    produced_artifacts: list[dict[str, Any]]

    async def execute(self, call: CanonicalToolCall) -> CanonicalToolResult:
        try:
            result = await self._execute(call.name, dict(call.args or {}))
            text = str(result.get("text") or "repair bill builder updated")
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

    async def _execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "op_minion_repair_bill_begin":
            return self._begin(args)
        if name in {"op_minion_repair_bill_add_module_patch", "op_minion_repair_bill_add_module"}:
            return self._add_module_patch(args)
        if name == "op_minion_repair_bill_add_acceptance_criteria_batch":
            return self._add_acceptance_criteria_batch(args)
        if name == "op_minion_repair_bill_add_evidence":
            return self._add_evidence(args)
        if name == "op_minion_repair_bill_validate":
            return self._validate(args)
        if name == "op_minion_repair_bill_submit":
            return await self._submit(args)
        raise ValueError(f"unknown repair bill builder tool: {name}")

    def _begin(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"parent_work_order_id", "source_module_id", "summary", "bill_handle"})
        parent_id = _text(args.get("parent_work_order_id") or self.workspace.get("parent_work_order_id"))
        if not parent_id:
            raise ValueError("parent_work_order_id is required")
        source_module_id = _text(
            args.get("source_module_id")
            or self.workspace.get("parent_module_id")
            or self.workspace.get("module_id")
            or self.workspace.get("module_name")
        )
        snapshot = self._repository().read_work_order(parent_id)
        if snapshot.get("status") != "ok":
            raise ValueError(f"unknown parent work order: {parent_id}")
        metadata = dict((snapshot.get("work_order") or {}).get("metadata") or {})
        plan_execution = dict(metadata.get("plan_execution") or {})
        if str(plan_execution.get("mode") or "").strip() != "module_parent_milestones":
            raise ValueError("parent_work_order_id must refer to a module-parent plan work order")
        dag = _plan_execution_dag_state(plan_execution)
        module_ids = _string_list(plan_execution.get("module_order") or dag.get("module_order"))
        if not module_ids:
            raise ValueError("parent work order has no module DAG")
        handle = _text(args.get("bill_handle")) or f"repair_bill_{_safe_id(parent_id)}_{uuid4().hex[:8]}"
        if not handle.startswith("repair_bill_"):
            handle = f"repair_bill_{_safe_id(handle)}"
        state = {
            "bill_handle": handle,
            "bill_id": handle,
            "parent_work_order_id": parent_id,
            "source_module_id": source_module_id,
            "summary": _text(args.get("summary")) or "repair bill submitted",
            "known_module_ids": module_ids,
            "module_patches": {},
            "lifecycle": "editing",
        }
        self._save_state(state)
        return {
            "text": f"Repair bill started: {handle}. Existing modules: {', '.join(module_ids)}",
            "structured": {"bill_handle": handle, "parent_work_order_id": parent_id, "known_module_ids": module_ids},
        }

    def _add_module_patch(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(
            args,
            {
                "bill_handle",
                "module_name",
                "defect_kind",
                "summary",
                "acceptance_criteria",
                "additional_acceptance_criteria",
                "negative_cases",
                "evidence",
            },
        )
        state = self._load_state(_required(args, "bill_handle"))
        module_id = self._module_id(state, args)
        patch = _normalize_module_patch(module_id, args)
        patches = dict(state.get("module_patches") or {})
        patches[module_id] = _merge_patch(dict(patches.get(module_id) or {}), patch)
        state["module_patches"] = patches
        self._save_state(state)
        return {
            "text": f"Repair patch added: {module_id}",
            "structured": {"bill_handle": state["bill_handle"], "module_name": module_id, "module_id": module_id, "patch_count": len(patches)},
        }

    def _add_acceptance_criteria_batch(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"bill_handle", "module_name", "criteria", "defect_kind"})
        state = self._load_state(_required(args, "bill_handle"))
        module_id = self._module_id(state, args)
        criteria = _acceptance_items(args.get("criteria"))
        if not criteria:
            raise ValueError("criteria must contain at least one non-empty item")
        patches = dict(state.get("module_patches") or {})
        existing = dict(patches.get(module_id) or {})
        incoming = _normalize_module_patch(
            module_id,
            {
                "defect_kind": args.get("defect_kind") or existing.get("defect_kind") or "module_defect",
                "acceptance_criteria": criteria,
            },
        )
        patches[module_id] = _merge_patch(existing, incoming)
        state["module_patches"] = patches
        self._save_state(state)
        return {
            "text": f"Repair acceptance criteria added: {module_id} ({len(criteria)})",
            "structured": {"bill_handle": state["bill_handle"], "module_name": module_id, "module_id": module_id, "added_count": len(criteria)},
        }

    def _add_evidence(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"bill_handle", "module_name", "evidence"})
        state = self._load_state(_required(args, "bill_handle"))
        module_id = self._module_id(state, args)
        evidence = _evidence_items(args.get("evidence"))
        if not evidence:
            raise ValueError("evidence must contain at least one non-empty item")
        patches = dict(state.get("module_patches") or {})
        existing = dict(patches.get(module_id) or {})
        incoming = _normalize_module_patch(
            module_id,
            {
                "defect_kind": existing.get("defect_kind") or "module_defect",
                "evidence": evidence,
            },
        )
        patches[module_id] = _merge_patch(existing, incoming)
        state["module_patches"] = patches
        self._save_state(state)
        return {
            "text": f"Repair evidence added: {module_id} ({len(evidence)})",
            "structured": {"bill_handle": state["bill_handle"], "module_name": module_id, "module_id": module_id, "added_count": len(evidence)},
        }

    def _validate(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"bill_handle"})
        state = self._load_state(_required(args, "bill_handle"))
        bill = _compile_bill(state)
        return {
            "text": f"Repair bill is valid: {bill['bill_id']} ({len(bill['module_patches'])} module patch(es))",
            "structured": {"bill_handle": state["bill_handle"], "repair_bill": bill},
        }

    async def _submit(self, args: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_args(args, {"bill_handle", "relative_path", "overwrite"})
        state = self._load_state(_required(args, "bill_handle"))
        bill = _compile_bill(state)
        relative_path = _text(args.get("relative_path") or "repair_bill.json")
        artifact = _write_minion_artifact(
            self.workspace,
            {
                "relative_path": relative_path,
                "content": json.dumps(bill, ensure_ascii=False, indent=2, sort_keys=True),
                "title": "Repair bill",
                "role": "repair_bill",
                "mime_type": "application/json",
                "overwrite": bool(args.get("overwrite", True)),
            },
        )
        _append_unique_artifact(self.produced_artifacts, artifact)
        client = MinionManagerClient(runtime_root=self._runtime_root())
        submit_result = await client.request("submit_repair_bill", {"repair_bill": bill, "artifact": dict(artifact)})
        state["lifecycle"] = "submitted"
        state["submitted_artifact"] = dict(artifact)
        state["submit_result"] = dict(submit_result)
        self._save_state(state)
        return {
            "text": f"Repair bill submitted: {bill['bill_id']} ({submit_result.get('status') or 'unknown'})",
            "structured": {"bill_handle": state["bill_handle"], "repair_bill": bill, "artifact": artifact, "submit_result": submit_result},
        }

    def _module_id(self, state: dict[str, Any], args: dict[str, Any]) -> str:
        module_id = _text(args.get("module_name"))
        known = _string_list(state.get("known_module_ids"))
        if not module_id:
            if len(known) == 1:
                module_id = known[0]
            else:
                raise ValueError("module_name is required")
        if module_id not in set(known):
            raise ValueError(f"unknown module_name for parent DAG: {module_id}")
        return module_id

    def _repository(self) -> MinionTaskingRepository:
        return MinionTaskingRepository(runtime_root=self._runtime_root())

    def _runtime_root(self) -> Path:
        runtime_root = _text(self.workspace.get("runtime_root"))
        if not runtime_root:
            raise ValueError("workspace.runtime_root is required")
        return Path(runtime_root).expanduser().resolve()

    def _state_root(self) -> Path:
        artifact_dir = _text(self.workspace.get("artifact_dir"))
        if not artifact_dir:
            raise ValueError("workspace.artifact_dir is required for repair bill builder")
        root = Path(artifact_dir).expanduser().resolve() / ".repair_bill_builder"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _state_path(self, bill_handle: str) -> Path:
        handle = _text(bill_handle)
        if not re.fullmatch(r"repair_bill_[A-Za-z0-9_]+", handle):
            raise ValueError("invalid bill_handle")
        return self._state_root() / f"{handle}.json"

    def _save_state(self, state: dict[str, Any]) -> None:
        path = self._state_path(str(state.get("bill_handle") or ""))
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def _load_state(self, bill_handle: str) -> dict[str, Any]:
        path = self._state_path(bill_handle)
        if not path.exists():
            raise ValueError(f"unknown repair bill handle: {bill_handle}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"invalid repair bill state: {bill_handle}")
        return payload


async def repair_bill_builder_tool_result(
    call: CanonicalToolCall,
    workspace: dict[str, Any],
    produced_artifacts: list[dict[str, Any]],
) -> CanonicalToolResult:
    return await RepairBillBuilderRuntime(workspace=workspace, produced_artifacts=produced_artifacts).execute(call)


def _compile_bill(state: dict[str, Any]) -> dict[str, Any]:
    patches = {
        str(module_id): dict(patch)
        for module_id, patch in dict(state.get("module_patches") or {}).items()
        if str(module_id or "").strip() and isinstance(patch, dict)
    }
    if not patches:
        raise ValueError("repair bill must contain at least one module patch")
    return {
        "bill_id": _text(state.get("bill_id") or state.get("bill_handle")),
        "parent_work_order_id": _text(state.get("parent_work_order_id")),
        "source_module_id": _text(state.get("source_module_id")),
        "summary": _text(state.get("summary")) or "repair bill submitted",
        "modules": [dict(patch) for _, patch in patches.items()],
        "module_patches": patches,
    }


def _normalize_module_patch(module_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    defect_kind = _text(raw.get("defect_kind") or "module_defect").lower()
    if defect_kind not in _DEFECT_KINDS:
        defect_kind = "triage_required"
    acceptance = _acceptance_items(
        [
            *_listish(raw.get("acceptance_criteria")),
            *_listish(raw.get("additional_acceptance_criteria")),
        ]
    )
    return {
        "module_id": module_id,
        "module_name": module_id,
        "defect_kind": defect_kind,
        "summary": _text(raw.get("summary")),
        "additional_acceptance_criteria": _dedupe_text([str(item.get("criterion") or "") for item in acceptance]),
        "acceptance_criteria": acceptance,
        "negative_cases": _dedupe_dicts(_case_items(raw.get("negative_cases"))),
        "evidence": _dedupe_dicts(_evidence_items(raw.get("evidence"))),
    }


def _merge_patch(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    if not existing:
        return dict(incoming)
    kinds = _dedupe_text([_text(existing.get("defect_kind")), _text(incoming.get("defect_kind"))])
    result = dict(existing)
    result["defect_kind"] = "contract_defect" if "contract_defect" in kinds else kinds[0]
    result["summary"] = "; ".join(_dedupe_text([_text(existing.get("summary")), _text(incoming.get("summary"))]))
    result["additional_acceptance_criteria"] = _dedupe_text(
        [*_string_list(existing.get("additional_acceptance_criteria")), *_string_list(incoming.get("additional_acceptance_criteria"))]
    )
    result["acceptance_criteria"] = _dedupe_dicts(
        [
            *[dict(item) for item in list(existing.get("acceptance_criteria") or []) if isinstance(item, dict)],
            *[dict(item) for item in list(incoming.get("acceptance_criteria") or []) if isinstance(item, dict)],
        ]
    )
    result["negative_cases"] = _dedupe_dicts(
        [
            *[dict(item) for item in list(existing.get("negative_cases") or []) if isinstance(item, dict)],
            *[dict(item) for item in list(incoming.get("negative_cases") or []) if isinstance(item, dict)],
        ]
    )
    result["evidence"] = _dedupe_dicts(
        [
            *[dict(item) for item in list(existing.get("evidence") or []) if isinstance(item, dict)],
            *[dict(item) for item in list(incoming.get("evidence") or []) if isinstance(item, dict)],
        ]
    )
    return result


def _acceptance_items(values: Any) -> list[dict[str, Any]]:
    raw_items = values if isinstance(values, (list, tuple)) else ([values] if values not in (None, "") else [])
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items, start=1):
        if isinstance(item, dict):
            criterion = _text(item.get("criterion") or item.get("summary") or item.get("text"))
            if not criterion:
                continue
            result.append(
                {
                    "id": _text(item.get("id")) or f"RAC-{index}",
                    "criterion": criterion,
                    "evidence_expectation": _text(item.get("evidence_expectation") or item.get("evidence"))
                    or "Focused repair verification covers this added criterion.",
                    "negative_cases": _string_list(item.get("negative_cases")),
                    "gate_check_refs": _string_list(item.get("gate_check_refs")),
                    "quantifier": _text(item.get("quantifier")),
                }
            )
            continue
        criterion = _text(item)
        if criterion:
            result.append(
                {
                    "id": f"RAC-{index}",
                    "criterion": criterion,
                    "evidence_expectation": "Focused repair verification covers this added criterion.",
                    "negative_cases": [],
                    "gate_check_refs": [],
                    "quantifier": "",
                }
            )
    return result


def _listish(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _case_items(values: Any) -> list[dict[str, Any]]:
    raw_items = values if isinstance(values, (list, tuple)) else ([values] if values not in (None, "") else [])
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items, start=1):
        if isinstance(item, dict):
            payload = {key: value for key, value in dict(item).items() if value not in (None, "", [], {})}
            if payload:
                payload.setdefault("id", f"NEG-{index}")
                result.append(payload)
            continue
        text = _text(item)
        if text:
            result.append({"id": f"NEG-{index}", "case": text})
    return result


def _evidence_items(values: Any) -> list[dict[str, Any]]:
    raw_items = values if isinstance(values, (list, tuple)) else ([values] if values not in (None, "") else [])
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items, start=1):
        if isinstance(item, dict):
            payload = {key: value for key, value in dict(item).items() if value not in (None, "", [], {})}
            if payload:
                payload.setdefault("id", f"EVD-{index}")
                result.append(payload)
            continue
        text = _text(item)
        if text:
            result.append({"id": f"EVD-{index}", "summary": text})
    return result


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


def _plan_execution_dag_state(plan_execution: dict[str, Any]) -> dict[str, Any]:
    return _dag_state_to_runtime_dict(dict(plan_execution.get("dag_state") or plan_execution.get("module_dag") or {}))


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, dict):
        return [str(item).strip() for item in value.values() if str(item or "").strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _dedupe_text(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _dedupe_dicts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        key = json.dumps(dict(value or {}), ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(value))
    return result


def _safe_id(value: Any) -> str:
    raw = _text(value)
    safe = "".join(char if char.isalnum() or char == "_" else "_" for char in raw)
    while "__" in safe:
        safe = safe.replace("__", "_")
    safe = safe.strip("_")
    if safe:
        return safe[:80]
    return f"id_{uuid4().hex[:8]}"
