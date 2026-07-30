from __future__ import annotations

from pal.execution.generated_tool_models import (
    MinionV2SkeletonBuilderOpMinionAskQuestionInput,
    MinionV2SkeletonBuilderOpMinionArchitectureReviewFailInput,
    MinionV2SkeletonBuilderOpMinionArchitectureSubmitInput,
)

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, Literal, Mapping, Sequence

from pydantic import Field, model_validator
from pal.execution.tool_facade import EmptyToolInput, StrictToolModel
from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.v2.architecture_yaml import (
    ArchitectureDraftFileError,
    load_architecture_draft,
)
from pal.minion.v2.review_findings import (
    ADD_FINDING_CAPABILITY,
    empty_review_draft,
    partition_findings,
    structured_findings,
)
from pal.minion.v2.submission_preflight import (
    bound_reference_payload,
    raise_submission_errors,
)
from pal.minion.v2.submission_drafts import (
    SubmissionDraftContext,
    SubmissionDraftStore,
    assert_authoring_schema_budget,
)
from pal.minion.v2.work_checklist import (
    normalize_work_checklist,
    render_work_checklist,
    unfinished_work_checklist_steps,
)
from pal.minion.v2.skeleton import (
    ArchitectureValidationError,
    ArchitectureValidationResult,
    SemanticReferenceError,
    analyze_architecture_submission,
    architecture_revision_changed_paths_since,
)
from pal.minion.workspace_tools import _append_unique_artifact, _write_minion_artifact
from pal.shared import RuntimeStatus


ARCHITECTURE_CHECKLIST_STEPS = (
    "read requirements and settle the module design",
    "write the declaration skeleton without product behavior",
    "project the settled design into architecture.yaml and reconcile it",
)

_ARCHITECTURE_ACTION_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "phase": "design",
        "steps": [
            "Read the ordered task ledger and perform one bounded consistency pass.",
            "Settle module responsibilities, ownership, lifecycle, and public contract edges.",
        ],
    },
    {
        "phase": "declare",
        "steps": [
            "Write only the module declaration skeleton and normative contract comments.",
            "Defer product behavior, build details, and implementation experiments.",
        ],
    },
    {
        "phase": "reconcile",
        "steps": [
            "Read the preseeded architecture.yaml and encode the settled design into its schema.",
            "Reconcile YAML with declarations, complete the fixed checklist, and submit.",
        ],
    },
    {
        "phase": "submit",
        "steps": [
            "All three design phases are complete.",
            "Call architecture_submit; the Architecture Reviewer owns semantic acceptance.",
        ],
    },
)


def _validate_architecture_phase_order(
    statuses: Sequence[str],
) -> None:
    phase = "completed"
    for status in statuses:
        if phase == "completed":
            if status == "in_progress":
                phase = "in_progress"
            elif status == "pending":
                phase = "pending"
        elif phase == "in_progress":
            if status != "pending":
                raise ValueError(
                    "Architect checklist phases must progress in order"
                )
            phase = "pending"
        elif status != "pending":
            raise ValueError(
                "Architect checklist phases must progress in order"
            )


def _validate_architecture_checklist_shape(
    checklist: Mapping[str, Any],
) -> None:
    plan = [dict(item or {}) for item in list(checklist.get("plan") or [])]
    steps = [str(item.get("step") or "") for item in plan]
    if tuple(steps[: len(ARCHITECTURE_CHECKLIST_STEPS)]) != ARCHITECTURE_CHECKLIST_STEPS:
        raise ValueError("Architect checklist must preserve the three fixed ordered phase steps")
    if any(not step.startswith("resolve finding: ") for step in steps[len(ARCHITECTURE_CHECKLIST_STEPS) :]):
        raise ValueError("Architect checklist may append only `resolve finding: <finding_key>` steps")
    _validate_architecture_phase_order(
        [str(item["status"]) for item in plan]
    )


class MinionV2ArchitectureChecklistItem(StrictToolModel):
    """One fixed Architect phase and its current state."""

    step: Annotated[str, Field(min_length=1, max_length=240)]
    status: Literal["pending", "in_progress", "completed"]


class MinionV2ArchitectureUpdateChecklistInput(StrictToolModel):
    """Complete replacement for the Architect's phase and finding checklist."""

    plan: list[MinionV2ArchitectureChecklistItem] = Field(
        min_length=3,
        max_length=67,
    )

    @model_validator(mode="after")
    def validate_plan(
        self,
    ) -> "MinionV2ArchitectureUpdateChecklistInput":
        checklist = normalize_work_checklist(
            self.model_dump(mode="python"),
            require_nonempty=True,
            owner="Architect",
        )
        _validate_architecture_checklist_shape(checklist)
        return self


_ARCHITECTURE_CHECKLIST_EXAMPLE = {
    "plan": [
        {
            "step": ARCHITECTURE_CHECKLIST_STEPS[0],
            "status": "completed",
        },
        {
            "step": ARCHITECTURE_CHECKLIST_STEPS[1],
            "status": "in_progress",
        },
        {
            "step": ARCHITECTURE_CHECKLIST_STEPS[2],
            "status": "pending",
        },
    ]
}


ARCHITECTURE_SKELETON_CAPABILITIES = (
    "op_minion_ask_question",
    "op_minion_architecture_update_checklist",
    "op_minion_architecture_submit",
)
SKELETON_REVIEW_CAPABILITIES = (
    ADD_FINDING_CAPABILITY,
    "op_minion_architecture_review_pass",
    "op_minion_architecture_review_fail",
)
SKELETON_BUILDER_CAPABILITIES = (*ARCHITECTURE_SKELETON_CAPABILITIES, *SKELETON_REVIEW_CAPABILITIES)

SKELETON_BUILDER_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_minion_ask_question": {
        "alias": "ask_question",
        "description": "Use this tool proactively before architecture design or revision when task.yaml contains or may contain a contradiction, material ambiguity, incorrect or infeasible requirement, or missing decision; when the result depends on user preference; or when a choice would materially change product behavior, compatibility, architecture, modification scope, or implementation scope. Do not silently choose precedence, repair or reinterpret a requirement, or widen or narrow scope. Ask one decisive question without ending the Architect invocation: identify the exact issue and the decision needed, then supply a short title, precise question, and three option strings. Each option contains a readable choice and its impact or tradeoff. The channel also permits a custom free-text answer. Calling suspends the current tool call until the user answers or the logical invocation is paused, cancelled, restarted, or superseded; wall-clock time never expires a pending question. Before the answer is returned to you, the Manager appends the exact question and answer to the immutable task.yaml ledger; no follow-up task-edit or submit action is allowed or required. Continue architecture work using that answer as the newest and therefore highest-priority task revision.",
        "InputModel": MinionV2SkeletonBuilderOpMinionAskQuestionInput,
    },
    "op_minion_architecture_update_checklist": {
        "alias": "update_checklist",
        "description": (
            "Replace the complete Architect checklist. Preserve the three exact "
            "ordered phase steps from the example, then append one exact "
            "`resolve finding: <finding_key>` step for every bound revision finding. Set each to "
            "pending, in_progress, or completed; at most one may be "
            "in_progress. Work only on the current phase: first settle the "
            "requirements and module design without editing product files or "
            "reading architecture.yaml; then write declaration skeletons "
            "without product behavior; finally read the preseeded template, "
            "project the settled design into architecture.yaml, reconcile it "
            "with declarations, and submit. This checklist is an observable "
            "work cursor, not architecture truth or review evidence. The result includes the next phase action "
            "template; follow it without working ahead or adding implementation process."
        ),
        "InputModel": MinionV2ArchitectureUpdateChecklistInput,
        "examples": (_ARCHITECTURE_CHECKLIST_EXAMPLE,),
        "idempotency": "idempotent",
        "retry_policy": "automatic",
    },
    "op_minion_architecture_submit": {
        "alias": "architecture_submit",
        "description": "Validate and submit the complete Manager-preseeded architecture.yaml together with the declaration skeleton after all three fixed phases and every bound `resolve finding: <finding_key>` checklist item are completed. Takes no arguments. Its requirements, modules, and scenarios are dynamic snake_case maps. Define each module's responsibility, behavior kind, semantic dependencies and consumed provider outputs, input/output/error/invariant contract, ownership, lifecycle, optional state machine, and physical paths. Map each binding requirement to one owner and public contract path. Define end-to-end contract flows and success/failure observations. Submit mechanically checks checklist closure, strict YAML/schema validity, structural graph closure, path safety, Git state, fencing, snapshot stability, and that a revision made an actual source or semantic change. It does not decide whether a claimed finding repair is correct; Architecture Reviewer owns semantic regression and acceptance.",
        "InputModel": MinionV2SkeletonBuilderOpMinionArchitectureSubmitInput,
    },
    "op_minion_architecture_review_pass": {
        "alias": "architecture_review_pass",
        "description": "Submit PASS with no arguments only after a complete breadth-first semantic review finds no blocking defect or unresolved public semantic ambiguity. Do not stop auditing after the first counterexample. Priority and disposition are independent: a real p2 defect is blocking and requires FAIL; only a genuinely optional p2 advisory whose omission still satisfies every requirement and contract may accompany PASS. Confirm the ordered task ledger maps to the architecture, every module protocol is complete, ownership and lifecycle close, provider outputs satisfy consumer dependencies, scenario contract graphs can produce the required success and failure observations, and declarations/comments agree with architecture.yaml. Every observable input edge, partial-success failure path, and operation on public stateful values must have one declared result or an explicit set of outcomes safe for every consumer. Compilation is only supporting evidence that contracts compose; API presence, a successful probe, review_guarded implementation freedom, or hypothetical implementation behavior is not semantic proof.",
        "InputModel": EmptyToolInput,
    },
    "op_minion_architecture_review_fail": {
        "alias": "architecture_review_fail",
        "description": "Submit FAIL after every material defect has been recorded with add_finding. Takes no arguments.",
        "InputModel": MinionV2SkeletonBuilderOpMinionArchitectureReviewFailInput,
    },
}

for _tool_name, _tool_spec in SKELETON_BUILDER_TOOL_SPECS.items():
    assert_authoring_schema_budget(
        _tool_spec["InputModel"].model_json_schema(mode="validation", union_format="primitive_type_array"),
        owner=_tool_name,
    )
    for _example in tuple(_tool_spec.get("examples") or ()):
        _tool_spec["InputModel"].model_validate(_example, strict=True)


def compile_architecture_review_invocation_tool_contract(
    *,
    task_ledger: Mapping[str, Any],
    architecture: Mapping[str, Any],
) -> dict[str, Any]:
    del task_ledger
    modules = dict(architecture.get("modules") or {})
    scenarios = dict(architecture.get("scenarios") or {})
    requirements = dict(architecture.get("requirements") or {})
    module_names = sorted(str(name) for name in modules)
    requirement_names = sorted(str(name) for name in requirements)
    overrides = {
        ADD_FINDING_CAPABILITY: {"use_when": (
            "Record one actionable architecture-review defect. Use requirements_defect for contradictory or "
            "unimplementable task-ledger semantics, contract_defect for an invalid public shape, and "
            "architecture_defect for topology, ownership, lifecycle, or scenario defects. Complete the "
            "complete breadth-first audit first; do not fail fast after the first defect. Continue through every "
            "required module, edge, scenario, and changed risk surface, then issue all independent add_finding calls "
            "in one tool batch when possible. Priority and disposition are independent: every real defect, including "
            "priority p2, is blocking; advisory is only for a genuinely optional improvement whose omission still "
            "satisfies all binding requirements and contracts. "
            "Available modules="
            f"{json.dumps(module_names, ensure_ascii=False)}; scenarios="
            f"{json.dumps(sorted(str(name) for name in scenarios), ensure_ascii=False)}; requirements="
            f"{json.dumps(requirement_names, ensure_ascii=False)}."
        )},
        "op_minion_architecture_review_fail": {"use_when": (
            "Submit FAIL with no arguments only after all material defects are present in the structured finding Draft."
        )},
        "op_minion_architecture_review_pass": {"use_when": (
            "Submit PASS only after independently reading task.yaml, reconciling each exact Manager-recorded question and answer in order, and reviewing every module and end-to-end scenario. "
            "Use the bound original-to-skeleton Git diff to detect removed, relocated, or narrowed public APIs and reject compatibility drift. "
            "For every module, inspect responsibility, contract, ownership, lifecycle, optional state machine, dependencies, and declaration comments; "
            "walk each end-to-end contract graph for both success and failure and confirm every requirement has a legal observable path. "
            "Syntax and compilation are supporting checks, not semantic proof, and a missing dependency, ownership seam, failure endpoint, or requirement mapping is a material defect. "
            "PASS is forbidden while any public input, state, or call-sequence ambiguity remains. Explicitly audit absent/null/empty/zero-length and size-coupled inputs; "
            "partial output followed by failure, including commitment, consumption, ordering, post-error state, and retry; and copy/move/clone/share/reset/reuse semantics "
            "for public stateful values in initial, partial, and failed states. If two conforming implementations could make observably different choices that a consumer must know, "
            "record a finding instead of selecting one. A compile probe cannot establish that its test value is contractually legal, and review_guarded private implementation freedom "
            "cannot supply missing public behavior. "
            "Do not submit positive audit rows or partial findings. Never stop the audit merely because one defect "
            "already proves FAIL. A genuine p2 defect is blocking and cannot accompany PASS; only a truly optional "
            "p2 advisory may remain. PASS is forbidden while any blocking finding remains. PASS takes no arguments."
        )},
    }
    return {
        "contract_version": "6",
        "module_names": module_names,
        "requirement_names": requirement_names,
        "requirements": requirements,
        "guidance_overrides": overrides,
    }


def is_skeleton_builder_capability(name: str) -> bool:
    return str(name or "") in SKELETON_BUILDER_TOOL_SPECS


async def ask_question_tool_result(
    call: CanonicalToolCall,
    workspace: dict[str, Any],
    produced_artifacts: list[dict[str, Any]],
    *,
    request_user: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None,
) -> CanonicalToolResult:
    del workspace, produced_artifacts
    if request_user is None:
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text="Architect user interaction is unavailable in this runtime",
            llm_text="Architect user interaction is unavailable. Do not guess a material Requirement or preference.",
            structured={"reason": "user_interaction_unavailable"},
            call_id=call.call_id,
            status=RuntimeStatus.ERROR,
        )
    try:
        args = dict(call.args or {})
        title = str(args.get("title") or "").strip()
        question = str(args.get("question") or "").strip()
        if not title or not question:
            raise ValueError("ask_question requires title and question")
        normalized_options: list[dict[str, str]] = []
        for index in range(1, 4):
            option = str(args.get(f"option_{index}") or "").strip()
            if not option:
                raise ValueError(f"ask_question requires option_{index}")
            normalized_options.append({"label": option, "description": option})
        response = await request_user(
            {
                "title": title,
                "questions": [
                    {
                        "id": "architecture-question",
                        "title": title,
                        "question": question,
                        "options": normalized_options,
                    }
                ],
            },
        )
        answers = [dict(item or {}) for item in list(response.get("answers") or [])]
        answer = str(answers[0].get("answer") or "") if answers else ""
        if not answer.strip():
            return CanonicalToolResult(
                name=call.name,
                ok=False,
                text="Architect user question timed out or was cancelled",
                llm_text="The user did not answer. Keep the ambiguity explicit; do not submit an architecture that guesses the answer.",
                structured={"status": "timed_out_or_cancelled"},
                call_id=call.call_id,
                status=RuntimeStatus.ERROR,
            )
        revision = dict(response.get("task_revision") or {})
        if not bool(revision.get("appended")):
            raise RuntimeError(
                "Manager returned an Architect answer without appending the task revision"
            )
        return CanonicalToolResult(
            name=call.name,
            ok=True,
            text=f"User answered: {answer}",
            llm_text=(
                f"User answered: {answer}\n"
                f"The Manager already appended this exact communication as task revision "
                f"{int(revision.get('sequence') or 0)}. Continue architecture work directly. "
                "Do not edit or restate task.yaml; this newest revision has precedence over "
                "conflicting older revisions and original text."
            ),
            structured={
                "status": "answered_revision_recorded",
                "answer": answer,
                "task_revision": revision,
            },
            call_id=call.call_id,
            status=RuntimeStatus.OK,
        )
    except Exception as exc:
        message = f"{exc.__class__.__name__}: {exc}"
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=message,
            llm_text=message,
            structured={"error": str(exc), "error_type": exc.__class__.__name__},
            call_id=call.call_id,
            status=RuntimeStatus.INVALID,
        )


def skeleton_builder_tool_result(
    call: CanonicalToolCall,
    workspace: dict[str, Any],
    produced_artifacts: list[dict[str, Any]],
) -> CanonicalToolResult:
    try:
        name = str(call.name or "")
        if name == "op_minion_architecture_update_checklist":
            return _replace_architecture_checklist(call, workspace)
        if name == "op_minion_architecture_submit":
            output, version = _compile_architecture_submission(
                call,
                workspace,
            )
            filename, title, draft_kind = "architecture_submission.json", "V2 architecture skeleton submission", "architecture"
        elif name in {
            "op_minion_architecture_review_pass",
            "op_minion_architecture_review_fail",
        }:
            output, version = _compile_architecture_review(call, workspace)
            filename, title, draft_kind = "architecture_review.json", "V2 architecture skeleton review", "architecture_review"
        else:
            raise ValueError(f"unknown skeleton builder capability: {name}")
        context = SubmissionDraftContext.from_workspace(workspace, draft_kind=draft_kind)
        store = SubmissionDraftStore(Path(str(workspace["runtime_root"])))
        if store.uses_role_gateway:
            store.mark_submitted(
                context,
                expected_version=version,
                submission_payload=output,
            )
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
        if not store.uses_role_gateway:
            store.mark_submitted(
                context,
                expected_version=version,
                submission_payload=output,
            )
        return CanonicalToolResult(
            name=name,
            ok=True,
            text=f"{title} submitted",
            llm_text=f"{title} submitted",
            structured={"submitted": True},
            call_id=call.call_id,
            status=RuntimeStatus.OK,
        )
    except Exception as exc:
        message = f"{exc.__class__.__name__}: {exc}"
        structured = (
            exc.to_dict()
            if isinstance(
                exc,
                (
                    ArchitectureDraftFileError,
                    ArchitectureValidationError,
                    SemanticReferenceError,
                ),
            )
            else {"error": str(exc), "error_type": exc.__class__.__name__}
        )
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=message,
            llm_text=message + " Correct the rejected semantic definition and retry in the same invocation.",
            structured=structured,
            call_id=call.call_id,
            status=RuntimeStatus.INVALID,
        )


def architecture_checklist_context(
    workspace: Mapping[str, Any],
) -> str:
    """Render the latest fenced Architect phase cursor for prompt assembly."""

    metadata = dict(workspace.get("minion_v2") or {})
    if str(metadata.get("role") or "") != "architect":
        return ""
    try:
        context = SubmissionDraftContext.from_workspace(
            workspace,
            draft_kind="architecture",
        )
        snapshot = SubmissionDraftStore(
            Path(str(workspace["runtime_root"]))
        ).read(
            context,
            seed=_architecture_seed(workspace),
        )
        checklist = _normalize_architecture_checklist(
            snapshot.payload.get("checklist"),
        )
    except (OSError, RuntimeError, ValueError):
        return ""
    return _render_architecture_checklist(
        checklist,
    )


def _replace_architecture_checklist(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
) -> CanonicalToolResult:
    args = dict(call.args or {})
    checklist = _normalize_architecture_checklist(args)
    _validate_architecture_finding_checklist(checklist, workspace)
    context = SubmissionDraftContext.from_workspace(
        workspace,
        draft_kind="architecture",
    )
    store = SubmissionDraftStore(Path(str(workspace["runtime_root"])))

    def reducer(
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], Mapping[str, Any]]:
        payload["checklist"] = checklist
        return payload, {
            "updated": True,
            "checklist": checklist,
            "unfinished_count": len(
                unfinished_work_checklist_steps(checklist)
            ),
        }

    result = store.mutate(
        context,
        operation_key=str(
            call.call_id
            or "replace-architecture-checklist:"
            + hashlib.sha256(
                json.dumps(
                    args,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        ),
        request=args,
        reducer=reducer,
        seed=_architecture_seed(workspace),
    )
    next_action = _next_architecture_action(checklist)
    result = dict(result)
    result["next_action"] = next_action
    text = _render_architecture_checklist(checklist, next_action=next_action)
    return CanonicalToolResult(
        name=call.name,
        ok=True,
        text=text,
        llm_text=text,
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
    checklist = _normalize_architecture_checklist(
        snapshot.payload.get("checklist"),
    )
    _validate_architecture_finding_checklist(checklist, workspace)
    unfinished = unfinished_work_checklist_steps(checklist)
    if unfinished:
        raise ValueError(
            "architecture_submit requires every fixed Architect checklist "
            "phase to be completed; unfinished: "
            + ", ".join(unfinished)
        )
    output = load_architecture_draft(workspace)
    errors: list[Any] = []
    report: ArchitectureValidationResult | None = None
    try:
        report = _preflight_submission(output, workspace)
    except (SemanticReferenceError, ValueError) as exc:
        errors.append(exc)
    if workspace.get("architecture_revision_base_submission"):
        base = dict(workspace.get("architecture_revision_base_submission") or {})
        changed_paths = _architecture_revision_changed_paths(workspace)
        if output == base and not changed_paths:
            errors.append("architecture revision makes no source or semantic change")
    raise_submission_errors(errors, owner="architecture_submit")
    compiled = dict(report.normalized_submission) if report is not None else output
    return compiled, snapshot.version


def _compile_architecture_review(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
) -> tuple[dict[str, Any], int]:
    name = str(call.name or "")
    args = dict(call.args or {})
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind="architecture_review")
    snapshot = SubmissionDraftStore(Path(str(workspace["runtime_root"]))).read(
        context,
        seed=_architecture_review_seed(),
    )
    findings, advisories = partition_findings(structured_findings(snapshot.payload))
    verdict = "FAIL" if name == "op_minion_architecture_review_fail" else "PASS"
    if verdict == "FAIL" and not findings:
        raise ValueError("architecture_review_fail requires at least one add_finding call")
    if verdict == "FAIL" and args:
        raise ValueError("architecture_review_fail takes no arguments")
    if verdict == "PASS" and findings:
        raise ValueError("architecture_review_pass requires an empty finding Draft")
    if verdict == "PASS" and args:
        raise ValueError("architecture_review_pass takes no arguments")
    output = {
        "schema_version": "4",
        "verdict": verdict,
        "findings": findings,
        "advisories": advisories,
        "review_scope": _compiled_review_scope(workspace),
    }
    _validate_review_shape(output)
    _preflight_review_submission(output, workspace)
    return output, snapshot.version


def _architecture_review_seed() -> dict[str, Any]:
    return empty_review_draft()


def _compiled_review_scope(workspace: Mapping[str, Any]) -> dict[str, Any]:
    architecture = bound_reference_payload(workspace, "architecture_index", required=False)
    return {
        "module_names": sorted(str(name) for name in dict(architecture.get("modules") or {})),
        "scenario_names": sorted(str(name) for name in dict(architecture.get("scenarios") or {})),
        "requirement_names": sorted(str(name) for name in dict(architecture.get("requirements") or {})),
    }


def _architecture_seed(workspace: Mapping[str, Any]) -> dict[str, Any]:
    finding_steps = [
        {
            "step": step,
            "status": "pending",
        }
        for step in _required_architecture_finding_steps(workspace)
    ]
    return {
        "checklist": {
            "plan": [
                {
                    "step": step,
                    "status": (
                        "in_progress"
                        if index == 0
                        else "pending"
                    ),
                }
                for index, step in enumerate(
                    ARCHITECTURE_CHECKLIST_STEPS
                )
            ] + finding_steps
        },
        "definitions": {"submission": {}},
        "evidence": {},
        "findings": [],
        "summary": {},
    }


def _normalize_architecture_checklist(
    value: Any,
) -> dict[str, Any]:
    checklist = normalize_work_checklist(
        value,
        require_nonempty=True,
        owner="Architect",
    )
    _validate_architecture_checklist_shape(checklist)
    return checklist


def _required_architecture_finding_steps(
    workspace: Mapping[str, Any],
) -> tuple[str, ...]:
    payload = bound_reference_payload(workspace, "revision_finding", required=False)
    keys = sorted(
        {
            str(dict(item or {}).get("finding_key") or "").strip()
            for item in list(payload.get("findings") or [])
            if str(dict(item or {}).get("finding_key") or "").strip()
        }
    )
    return tuple(f"resolve finding: {key}" for key in keys)


def _validate_architecture_finding_checklist(
    checklist: Mapping[str, Any],
    workspace: Mapping[str, Any],
) -> None:
    actual = tuple(
        str(dict(item or {}).get("step") or "")
        for item in list(checklist.get("plan") or [])[len(ARCHITECTURE_CHECKLIST_STEPS) :]
    )
    expected = _required_architecture_finding_steps(workspace)
    if actual != expected:
        raise ValueError(
            "Architect checklist must contain exactly the bound revision findings: "
            + (", ".join(expected) if expected else "none")
        )


def _next_architecture_action(checklist: Mapping[str, Any]) -> dict[str, Any]:
    plan = [dict(item or {}) for item in list(checklist.get("plan") or [])]
    unfinished = [item for item in plan if item.get("status") != "completed"]
    if not unfinished:
        return dict(_ARCHITECTURE_ACTION_TEMPLATES[-1])
    index = next(
        (
            position
            for position, item in enumerate(plan)
            if item.get("status") == "in_progress"
        ),
        next(
            (position for position, item in enumerate(plan) if item.get("status") != "completed"),
            0,
        ),
    )
    return {
        **dict(_ARCHITECTURE_ACTION_TEMPLATES[min(index, len(_ARCHITECTURE_ACTION_TEMPLATES) - 2)]),
        "current_step": str(plan[index].get("step") or ""),
    }


def _render_architecture_checklist(
    checklist: Mapping[str, Any],
    *,
    next_action: Mapping[str, Any] | None = None,
) -> str:
    rendered = render_work_checklist(
        checklist,
        owner="Architect",
        purpose="durable phase cursor; not architecture truth or review evidence",
    )
    action = dict(next_action or _next_architecture_action(checklist))
    return "\\n".join(
        [
            rendered,
            "",
            f"Next action ({action.get('phase') or 'design'}):",
            *[f"- {step}" for step in list(action.get("steps") or [])],
        ]
    )


def architecture_work_checklist_artifact(
    value: Any,
) -> dict[str, Any]:
    """Compile the durable Architect Draft cursor into its review projection."""

    return {
        "schema_version": "1",
        "kind": "architect_work_checklist",
        "authority": "work_cursor_only",
        "checklist": _normalize_architecture_checklist(value),
    }


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


def _preflight_submission(
    payload: Mapping[str, Any], workspace: Mapping[str, Any]
) -> ArchitectureValidationResult:
    references = {
        str(item.get("name") or ""): Path(str(item.get("path") or ""))
        for item in list(workspace.get("reference_paths") or [])
        if str(item.get("name") or "")
        and str(item.get("path") or "")
        and not bool(item.get("bound_input"))
    }
    evidence = bound_reference_payload(workspace, "evidence_catalog", required=False) or None
    repo_path = Path(str(workspace.get("repo_path") or "")).expanduser()
    if not repo_path.is_dir():
        raise ValueError("architecture worktree is unavailable for architecture preflight")
    result = analyze_architecture_submission(
        payload,
        requirements_payload={},
        workspace_root=repo_path,
        reference_roots=references,
        evidence_catalog=evidence,
    )
    result.raise_for_errors()
    normalized = dict(result.normalized_submission)
    base_sha = str(workspace.get("architecture_base_sha") or "").strip()
    if base_sha:
        head = _git_output(repo_path, "rev-parse", "HEAD")
        if head != base_sha:
            raise ValueError(
                "Architect changed Git HEAD; commits, merges, rebases, checkouts, and resets are manager-owned operations"
            )
    return result


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
    findings = structured_findings(payload)
    _blocking_advisories, advisories = partition_findings(
        structured_findings({"findings": list(payload.get("advisories") or [])})
    )
    if verdict not in {"PASS", "FAIL"}:
        raise ValueError("verdict must be PASS or FAIL")
    if verdict == "PASS" and findings:
        raise ValueError("PASS cannot contain findings")
    if verdict == "FAIL" and not findings:
        raise ValueError("FAIL requires findings")
    if len(advisories) != len(list(payload.get("advisories") or [])):
        raise ValueError("architecture review advisories must use disposition=advisory")


def _preflight_review_submission(
    payload: Mapping[str, Any],
    workspace: Mapping[str, Any],
) -> None:
    del payload, workspace
