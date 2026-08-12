from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any, Literal, Mapping

from pydantic import Field

from pal.execution.tool_facade import StrictToolModel, rejection
from pal.minion.v2.submission_drafts import (
    SubmissionDraftContext,
    SubmissionDraftStore,
    assert_authoring_schema_budget,
)
from pal.shared import RuntimeStatus, ToolExecutionResult


UPDATE_CHECKLIST_CAPABILITY = "op_minion_update_checklist"
ADD_FINDING_CAPABILITY = "op_minion_add_finding"
WORK_ITEM_DRAFT_KIND = "work_items"
WORK_ITEM_STATUSES = frozenset({"pending", "in_progress", "completed"})
FINDING_KINDS = frozenset(
    {
        "requirements_defect",
        "module_defect",
        "dependency_defect",
        "contract_defect",
        "architecture_defect",
        "sink_defect",
        "verification_defect",
    }
)
FINDING_PRIORITIES = ("p0", "p1", "p2")
FINDING_DISPOSITIONS = ("blocking", "advisory")
PRIORITY_TO_SEVERITY = {"p0": "blocker", "p1": "major", "p2": "minor"}


class MinionWorkItemStep(StrictToolModel):
    step: str = Field(min_length=1, max_length=1000)
    status: Literal["pending", "in_progress", "completed"]


class MinionUpdateChecklistInput(StrictToolModel):
    plan: list[MinionWorkItemStep] = Field(min_length=1, max_length=64)


class MinionReviewFindingLocation(StrictToolModel):
    scope: Literal["task_ledger", "workspace"]
    file: str = Field(min_length=1)
    line: int = Field(ge=1)
    symbol: str | None = Field(default=None, min_length=1)


class MinionAddFindingInput(StrictToolModel):
    finding_kind: Literal[
        "requirements_defect",
        "module_defect",
        "dependency_defect",
        "contract_defect",
        "architecture_defect",
        "sink_defect",
        "verification_defect",
    ]
    priority: Literal["p0", "p1", "p2"]
    disposition: Literal["blocking", "advisory"] = "blocking"
    summary: str = Field(min_length=1, max_length=4000)
    locations: list[MinionReviewFindingLocation] | None = Field(
        default=None,
        max_length=8,
    )


UPDATE_CHECKLIST_EXAMPLES = (
    {
        "plan": [
            {"step": "inspect the owned contract", "status": "completed"},
            {"step": "implement the bounded behavior", "status": "in_progress"},
            {"step": "run focused checks", "status": "pending"},
        ]
    },
)

ADD_FINDING_EXAMPLES = (
    {
        "finding_kind": "requirements_defect",
        "priority": "p0",
        "summary": (
            "The four-byte big-endian header decodes the example length as 584, "
            "so the supplied bytes cannot produce FRAME 4869."
        ),
        "locations": [
            {
                "scope": "task_ledger",
                "file": "task.yaml",
                "line": 78,
                "symbol": "CLI behavior / Decode",
            }
        ],
    },
    {
        "finding_kind": "module_defect",
        "priority": "p1",
        "summary": (
            "Decoder consumes later input after entering the failed state, "
            "violating the failed-until-reset contract."
        ),
        "locations": [
            {
                "scope": "workspace",
                "file": "src/framepipe.cpp",
                "line": 83,
                "symbol": "framepipe::Decoder::feed",
            }
        ],
    },
)

UPDATE_CHECKLIST_TOOL_SPEC: dict[str, Any] = {
    "alias": "update_checklist",
    "guidance": {
        "purpose": "Replace the current role's complete compact semantic work cursor.",
        "use_when": (
            "Initialize it after understanding the bounded assignment, then update statuses "
            "and Manager-routed finding repair steps as work advances."
        ),
        "do_not_use_when": (
            "Do not use it as contract truth or evidence, invent completed work, remove fixed "
            "playbook steps, or include a terminal submission call as a checklist item."
        ),
        "failure_next_steps": (
            "Correct the complete plan, preserve fixed and Manager-routed items in order, and "
            "ensure at most one item is in_progress before retrying."
        ),
    },
    "InputModel": MinionUpdateChecklistInput,
    "examples": UPDATE_CHECKLIST_EXAMPLES,
    "idempotency": "idempotent",
    "retry_policy": "automatic",
}

ADD_FINDING_TOOL_SPEC: dict[str, Any] = {
    "alias": "add_finding",
    "guidance": {
        "purpose": "Record one actionable defect in the Manager-owned WorkItem ledger.",
        "use_when": (
            "Use after reproducing or otherwise establishing one concrete correctness, "
            "contract, requirement, architecture, delivery, verification, or performance defect."
        ),
        "do_not_use_when": (
            "Do not invent or maintain finding identities, duplicate an existing semantic "
            "finding, or mark a real defect advisory. Advisory is only an optional p2 "
            "improvement whose omission satisfies every binding requirement and contract."
        ),
        "failure_next_steps": (
            "Correct the finding kind, priority, disposition, summary, or bounded locations "
            "from the returned validation error; reconcile before retrying an applied write."
        ),
    },
    "InputModel": MinionAddFindingInput,
    "examples": ADD_FINDING_EXAMPLES,
    "idempotency": "keyed_idempotent",
    "retry_policy": "reconcile_first",
}

for _capability, _model, _examples in (
    (UPDATE_CHECKLIST_CAPABILITY, MinionUpdateChecklistInput, UPDATE_CHECKLIST_EXAMPLES),
    (ADD_FINDING_CAPABILITY, MinionAddFindingInput, ADD_FINDING_EXAMPLES),
):
    assert_authoring_schema_budget(
        _model.model_json_schema(
            mode="validation",
            union_format="primitive_type_array",
        ),
        owner=_capability,
    )
    for _example in _examples:
        _model.model_validate(_example, strict=True)


def work_item_context(workspace: Mapping[str, Any]) -> SubmissionDraftContext:
    return SubmissionDraftContext.from_workspace(
        workspace,
        draft_kind=WORK_ITEM_DRAFT_KIND,
    )


def work_item_seed(workspace: Mapping[str, Any]) -> dict[str, Any]:
    binding = dict(workspace.get("minion_v2") or {})
    raw_seed = list(binding.get("work_item_seed") or [])
    items: list[dict[str, Any]] = []
    for ordinal, raw in enumerate(raw_seed):
        item = dict(raw or {})
        summary = str(item.get("summary") or "").strip()
        if not summary:
            continue
        kind = str(item.get("kind") or "phase")
        if kind not in {"phase", "task", "finding"}:
            raise ValueError(f"unsupported work item seed kind: {kind}")
        status = str(item.get("status") or "pending")
        if status not in WORK_ITEM_STATUSES:
            raise ValueError(f"unsupported work item seed status: {status}")
        items.append(
            {
                "item_id": _work_item_id(kind, summary, ordinal),
                "kind": kind,
                "status": status,
                "summary": summary,
                "ordinal": ordinal,
                "origin": str(item.get("origin") or "manager"),
                "required": bool(item.get("required", True)),
                **(
                    {"finding": normalize_finding(dict(item["finding"]))}
                    if isinstance(item.get("finding"), Mapping)
                    else {}
                ),
            }
        )
    return {"items": items}


def read_work_items(workspace: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = SubmissionDraftStore(
        _runtime_root(workspace)
    ).read(
        work_item_context(workspace),
        seed=work_item_seed(workspace),
    )
    return {
        "version": snapshot.version,
        "items": [dict(item) for item in list(snapshot.payload.get("items") or [])],
    }


def submission_work_items(value: Any) -> list[dict[str, str]]:
    """Project the Manager ledger into role-handoff checklist semantics.

    Ledger identities and ordering metadata stay inside the Manager-owned
    WorkItem store.  Role submissions only need the meaning and completion
    state of each item.
    """

    return [
        {
            "kind": str(item.get("kind") or "task"),
            "status": str(item.get("status") or ""),
            "summary": str(item.get("summary") or ""),
        }
        for raw in list(value or [])
        if isinstance(raw, Mapping)
        for item in (dict(raw),)
    ]


def update_checklist_tool_result(
    call: ToolCallIR,
    workspace: Mapping[str, Any],
) -> ToolExecutionResult:
    try:
        checklist = normalize_checklist(dict(call.args or {}))
        context = work_item_context(workspace)
        seed = work_item_seed(workspace)
        fixed = [
            dict(item)
            for item in list(seed.get("items") or [])
            if str(dict(item).get("kind") or "") == "phase"
        ]
        fixed_summaries = [str(item["summary"]) for item in fixed]
        required_summaries = [
            str(item["summary"])
            for item in list(seed.get("items") or [])
            if bool(dict(item).get("required", True))
        ]
        plan_summaries = [str(item["step"]) for item in checklist["plan"]]
        if fixed_summaries and plan_summaries[: len(fixed_summaries)] != fixed_summaries:
            raise ValueError(
                "checklist must preserve the profile playbook steps as its ordered prefix"
            )
        missing_required = [
            summary
            for summary in required_summaries
            if summary not in plan_summaries
        ]
        if missing_required:
            raise ValueError(
                "checklist must preserve Manager-routed work items: "
                + "; ".join(missing_required)
            )

        def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
            existing = [dict(item) for item in list(payload.get("items") or [])]
            findings = [
                item for item in existing if str(item.get("kind") or "") == "finding"
            ]
            next_items: list[dict[str, Any]] = []
            for ordinal, entry in enumerate(checklist["plan"]):
                summary = str(entry["step"])
                prior = next(
                    (
                        item
                        for item in existing
                        if str(item.get("kind") or "") != "finding"
                        and str(item.get("summary") or "") == summary
                    ),
                    None,
                )
                kind = "phase" if summary in fixed_summaries else "task"
                next_items.append(
                    {
                        "item_id": (
                            str(prior.get("item_id"))
                            if prior is not None
                            else _work_item_id(kind, summary, ordinal)
                        ),
                        "kind": kind,
                        "status": str(entry["status"]),
                        "summary": summary,
                        "ordinal": ordinal,
                        "origin": (
                            str(prior.get("origin") or "")
                            if prior is not None
                            else "worker"
                        )
                        or "worker",
                        "required": (
                            bool(prior.get("required", True))
                            if prior is not None
                            else summary in required_summaries
                        ),
                    }
                )
            payload["items"] = [*next_items, *findings]
            return payload, {
                "updated": True,
                "item_count": len(next_items),
                "unfinished": [
                    item["summary"]
                    for item in next_items
                    if item["status"] != "completed"
                ],
            }

        result = SubmissionDraftStore(_runtime_root(workspace)).mutate(
            context,
            operation_key=str(call.call_id or _request_key("checklist", call.args)),
            request=dict(call.args or {}),
            reducer=reducer,
            seed=seed,
        )
        unfinished = list(result.get("unfinished") or [])
        next_action = _next_action(workspace, checklist)
        result = {**dict(result), "next_action": next_action}
        text = (
            f"Checklist updated; {len(unfinished)} unfinished item(s)."
            if unfinished
            else "Checklist updated; all items are complete."
        )
        if next_action:
            text += (
                f" Next: {next_action['step']} — "
                f"{next_action['instruction']}"
            )
        return _ok(call, text, result)
    except Exception as exc:
        return _invalid(call, exc, "Correct the semantic plan and retry.")


def add_finding_tool_result(
    call: ToolCallIR,
    workspace: Mapping[str, Any],
) -> ToolExecutionResult:
    try:
        finding = normalize_finding(dict(call.args or {}))
        binding = dict(workspace.get("minion_v2") or {})
        role = str(binding.get("role") or "")
        mode = str(binding.get("mode") or "")
        if role == "reviewer" and mode == "architecture" and finding["finding_kind"] not in {
            "requirements_defect",
            "contract_defect",
            "architecture_defect",
        }:
            raise ValueError(
                "contract reviewer finding_kind must be requirements_defect, "
                "contract_defect, or architecture_defect"
            )
        context = work_item_context(workspace)
        seed = work_item_seed(workspace)
        semantic_hash = _finding_hash(finding)
        finding_id = f"finding_{semantic_hash[:16]}"

        def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
            items = [dict(item) for item in list(payload.get("items") or [])]
            existing = next(
                (
                    item
                    for item in items
                    if str(item.get("kind") or "") == "finding"
                    and str(item.get("semantic_hash") or "") == semantic_hash
                ),
                None,
            )
            if existing is not None:
                return payload, {
                    "recorded": True,
                    "deduplicated": True,
                    "finding_id": str(existing["item_id"]),
                    "finding_count": sum(
                        str(item.get("kind") or "") == "finding" for item in items
                    ),
                }
            items.append(
                {
                    "item_id": finding_id,
                    "kind": "finding",
                    "status": "completed",
                    "summary": finding["summary"],
                    "ordinal": len(items),
                    "origin": f"{role}:{mode}",
                    "semantic_hash": semantic_hash,
                    "finding": finding,
                }
            )
            payload["items"] = items
            return payload, {
                "recorded": True,
                "deduplicated": False,
                "finding_id": finding_id,
                "finding_count": sum(
                    str(item.get("kind") or "") == "finding" for item in items
                ),
            }

        result = SubmissionDraftStore(_runtime_root(workspace)).mutate(
            context,
            operation_key=str(call.call_id or f"add-finding:{semantic_hash}"),
            request=dict(call.args or {}),
            reducer=reducer,
            seed=seed,
        )
        text = (
            f"Finding {result['finding_id']} "
            f"{'already recorded' if result.get('deduplicated') else 'recorded'}; "
            f"ledger has {result.get('finding_count', 0)} finding(s)."
        )
        return _ok(call, text, result)
    except Exception as exc:
        return _invalid(call, exc, "Correct the finding and retry.")


def assert_work_items_complete(workspace: Mapping[str, Any]) -> dict[str, Any]:
    ledger = read_work_items(workspace)
    required_seed = [
        str(item.get("summary") or "")
        for item in list(work_item_seed(workspace).get("items") or [])
        if bool(dict(item).get("required", True))
    ]
    present = {
        str(item.get("summary") or "")
        for item in ledger["items"]
        if str(item.get("kind") or "") in {"phase", "task"}
    }
    missing = [summary for summary in required_seed if summary not in present]
    if missing:
        raise ValueError(
            "checklist dropped Manager-routed work items: "
            + "; ".join(missing)
        )
    planned = [
        item
        for item in ledger["items"]
        if str(item.get("kind") or "") in {"phase", "task"}
    ]
    if not planned:
        raise ValueError("initialize update_checklist before submitting")
    unfinished = [
        str(item.get("summary") or "")
        for item in planned
        if str(item.get("status") or "") != "completed"
    ]
    if unfinished:
        raise ValueError(
            "complete every checklist item before submitting: "
            + "; ".join(unfinished)
        )
    return ledger


def findings_from_work_items(workspace: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            **dict(item.get("finding") or {}),
            "finding_id": str(item.get("item_id") or ""),
        }
        for item in read_work_items(workspace)["items"]
        if str(item.get("kind") or "") == "finding"
    ]


def render_work_item_context(workspace: Mapping[str, Any]) -> str:
    ledger = read_work_items(workspace)
    required_seed = [
        str(item.get("summary") or "")
        for item in list(work_item_seed(workspace).get("items") or [])
        if bool(dict(item).get("required", True))
    ]
    items = [
        item
        for item in ledger["items"]
        if str(item.get("kind") or "") in {"phase", "task"}
    ]
    if not items:
        text = (
            "Work checklist is not initialized. Call update_checklist as soon "
            "as the bounded assignment is understood."
        )
        if required_seed:
            text += "\nRequired Manager-routed steps, in order:\n" + "\n".join(
                f"- {summary}" for summary in required_seed
            )
        return text
    lines = [
        "Manager-owned work checklist (execution cursor, not truth or evidence):"
    ]
    for item in items:
        lines.append(
            f"- {item.get('status')}: {item.get('summary')}"
        )
    present = {str(item.get("summary") or "") for item in items}
    missing = [summary for summary in required_seed if summary not in present]
    if missing:
        lines.append("Required Manager-routed steps not yet copied into the checklist:")
        lines.extend(f"- {summary}" for summary in missing)
    findings = [
        item
        for item in ledger["items"]
        if str(item.get("kind") or "") == "finding"
    ]
    if findings:
        lines.append("Recorded findings:")
        for item in findings:
            finding = dict(item.get("finding") or {})
            lines.append(
                f"- {item.get('item_id')}: {finding.get('priority')} "
                f"{finding.get('summary')}"
            )
    return "\n".join(lines)


def normalize_checklist(value: Mapping[str, Any]) -> dict[str, Any]:
    validated = MinionUpdateChecklistInput.model_validate(value, strict=True)
    plan = [
        {"step": item.step.strip(), "status": item.status}
        for item in validated.plan
    ]
    steps = [item["step"] for item in plan]
    if len(set(steps)) != len(steps):
        raise ValueError("checklist steps must be unique")
    if sum(item["status"] == "in_progress" for item in plan) > 1:
        raise ValueError("checklist allows at most one in_progress item")
    return {"plan": plan}


def normalize_finding(value: Mapping[str, Any]) -> dict[str, Any]:
    validated = MinionAddFindingInput.model_validate(value, strict=True)
    finding = validated.model_dump(mode="python", exclude_none=True)
    if finding["disposition"] == "advisory" and finding["priority"] != "p2":
        raise ValueError("advisory findings must use priority p2")
    locations: list[dict[str, Any]] = []
    for raw in list(finding.get("locations") or []):
        item = dict(raw)
        file_name = str(item.get("file") or "").replace("\\", "/").strip()
        path = PurePosixPath(file_name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("finding location file must be a safe relative path")
        item["file"] = str(path)
        locations.append(item)
    finding["locations"] = locations
    return finding


def _next_action(
    workspace: Mapping[str, Any],
    checklist: Mapping[str, Any],
) -> dict[str, str]:
    unfinished = [
        dict(item)
        for item in list(checklist.get("plan") or [])
        if str(dict(item).get("status") or "") != "completed"
    ]
    if not unfinished:
        return {
            "step": "submit",
            "instruction": "Call the bound role submit or outcome tool now.",
        }
    current = next(
        (
            item
            for item in unfinished
            if str(item.get("status") or "") == "in_progress"
        ),
        unfinished[0],
    )
    summary = str(current.get("step") or "")
    binding = dict(workspace.get("minion_v2") or {})
    protocol = dict(binding.get("role_protocol") or {})
    for raw in list(
        dict(protocol.get("playbook") or {}).get("steps") or []
    ):
        step = dict(raw or {})
        key = str(step.get("key") or "").replace("_", " ")
        if key == summary:
            return {
                "step": summary,
                "instruction": str(step.get("instruction") or ""),
                "done_when": str(step.get("done_when") or ""),
            }
    return {
        "step": summary,
        "instruction": (
            "Complete this bounded item directly, then update the checklist."
        ),
    }


def _work_item_id(kind: str, summary: str, ordinal: int) -> str:
    digest = hashlib.sha256(
        f"{kind}\0{ordinal}\0{summary}".encode("utf-8")
    ).hexdigest()[:16]
    return f"work_{digest}"


def _finding_hash(finding: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(finding),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _request_key(prefix: str, value: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _runtime_root(workspace: Mapping[str, Any]):
    from pathlib import Path

    value = str(workspace.get("runtime_root") or "").strip()
    if not value:
        raise ValueError("work item ledger requires the bound runtime_root")
    return Path(value)


def _ok(
    call: ToolCallIR,
    text: str,
    structured: Mapping[str, Any],
) -> ToolExecutionResult:
    return ToolExecutionResult(
        name=call.name,
        ok=True,
        text=text,
        llm_text=text,
        structured=dict(structured),
        call_id=call.call_id,
        status=RuntimeStatus.OK,
    )


def _invalid(
    call: ToolCallIR,
    exc: Exception,
    recovery: str,
) -> ToolExecutionResult:
    text = f"{exc.__class__.__name__}: {exc}"
    llm_text = f"{text} {recovery}"
    return ToolExecutionResult(
        name=call.name,
        ok=False,
        text=text,
        llm_text=llm_text,
        structured={"error": str(exc), "error_type": exc.__class__.__name__},
        call_id=call.call_id,
        status=RuntimeStatus.INVALID,
        invocation_result=rejection(
            "invalid_work_item",
            llm_text,
            details={
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            },
        ),
    )
