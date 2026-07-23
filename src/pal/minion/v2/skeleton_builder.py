from __future__ import annotations

from pal.execution.generated_tool_models import (
    MinionV2SkeletonBuilderOpMinionAskQuestionInput,
    MinionV2SkeletonBuilderOpMinionArchitectureReviewFailInput,
    MinionV2SkeletonBuilderOpMinionArchitectureReviewPassInput,
    MinionV2SkeletonBuilderOpMinionArchitectureSubmitInput,
)

import base64
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from pal.execution.tool_facade import EmptyToolInput
from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.v2.artifacts import ContentAddressedArtifactStore
from pal.minion.v2.architecture_yaml import (
    ArchitectureDraftFileError,
    load_architecture_draft,
)
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.task_ledger import (
    TaskRevisionAuthority,
    load_task_revision_yaml,
    task_revision_template_yaml,
)
from pal.minion.v2.review_findings import (
    ADD_FINDING_CAPABILITY,
    empty_review_draft,
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
from pal.minion.v2.skeleton import (
    ArchitectureValidationError,
    ArchitectureValidationResult,
    SemanticReferenceError,
    analyze_architecture_submission,
    architecture_revision_changed_paths_since,
    validate_architecture_revision_scope,
    validate_architecture_changed_paths,
)
from pal.minion.workspace_tools import _append_unique_artifact, _write_minion_artifact
from pal.shared import RuntimeStatus


ARCHITECTURE_SKELETON_CAPABILITIES = (
    "op_minion_ask_question",
    "op_minion_task_revision_submit",
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
        "description": "Use this tool proactively before architecture design or revision when task.yaml contains or may contain a contradiction, material ambiguity, incorrect or infeasible requirement, or missing decision; when the result depends on user preference; or when a choice would materially change product behavior, compatibility, architecture, modification scope, or implementation scope. Do not silently choose precedence, repair or reinterpret a requirement, or widen or narrow scope. Ask one decisive question without ending the Architect invocation: identify the exact issue and the decision needed, then supply a short title, precise question, and three option strings. Each option contains a readable choice and its impact or tradeoff. The channel also permits a custom free-text answer. Calling suspends the current tool call until the user answers, the long timeout expires, or the invocation is paused, cancelled, or restarted. After an answer, immediately encode only its semantic consequence in the preseeded task_revision.yaml and call task_revision_submit before continuing architecture work.",
        "InputModel": MinionV2SkeletonBuilderOpMinionAskQuestionInput,
    },
    "op_minion_task_revision_submit": {
        "alias": "task_revision_submit",
        "description": "Validate and append the Manager-preseeded task_revision.yaml after a user-authorized clarification or requirements edit. Takes no arguments. Use artifact_edit with relative_path=task_revision.yaml, content=<complete YAML>, operation=replace, and create_if_missing=false to fill the fixed schema: schema_version '1', a concise summary, and one or more changes with op add|replace|remove, a JSON Pointer path into the effective task document, and value for add/replace only. The JSON Pointer root is the content under task.yaml.original, not the task.yaml envelope: never prefix a path with /original, /revisions, or /schema_version; for example use /cli/decode/example/stdin_lines. Record the smallest semantic delta authorized by the exact user answer; never restate, rewrite, or dump the whole task. The Manager validates the YAML against the current task ledger, appends it atomically, preserves the exact question and answer as authority, and updates the running Architecture Revision. A rejected submit leaves the revision pending; correct the named YAML path and retry before architecture_submit or contract_submit.",
        "InputModel": EmptyToolInput,
        "idempotency": "keyed_idempotent",
        "retry_policy": "reconcile_first",
    },
    "op_minion_architecture_submit": {
        "alias": "architecture_submit",
        "description": "Validate and submit the complete Manager-preseeded architecture.yaml together with the declaration skeleton. Takes no arguments. The YAML uses dynamic snake_case maps for requirement obligations, modules, and scenarios, so add, edit, or delete entries with ordinary file tools. Every binding requirement must have an obligation that declares its claim, boundary partitions, observable outcome, public contract path, and semantic state map. Each state declares its exact entry_condition, consumer decision_point, stable outcome key, and required_outcome. States examined at the same consumer decision site must use the same decision_point so the Manager can derive cross-outcome distinguishability checks. Every scenario names the obligations it consumes. contract_path is an ordered semantic interface/signal chain such as module::API -> observation, not a filesystem allowlist or a list of bare source filenames. Submit performs strict YAML/schema validation, obligation-reference closure, dependency and scenario checks, path safety, Git-state checks, revision-scope checks, fencing, and snapshot stability. A rejected submit does not advance the workflow; correct the exact structured error path in architecture.yaml and retry. Architecture Reviewer owns semantic review.",
        "InputModel": MinionV2SkeletonBuilderOpMinionArchitectureSubmitInput,
    },
    "op_minion_architecture_review_pass": {
        "alias": "architecture_review_pass",
        "description": "Submit PASS only after one breadth-first review of the effective task.yaml ledger, every task revision against its embedded exact user answer, every requirement obligation, module contract, semantic dependency, and end-to-end scenario finds no material defect. Supply one compact obligation_trace per bound obligation and every Manager-bound decision_trace exactly once. An obligation_trace cites the contract and summarizes all declared boundaries without repeating a positive row per state or partition. A decision_trace compares two states reached at the same consumer decision_point but requiring different outcomes: for each side instantiate the smallest witness satisfying its bound entry_condition with no optional later progress, calculate only the public observation available then, and cite exact declaration/comment lines. distinguishing_signal must name the declared public signal whose value actually differs at those two entry instants. Evidence may cite only Manager-bound declaration contract files; the Manager reads and records the cited source lines and rejects missing files, invalid lines, implementation files, or missing/extra/duplicate coverage. Never replace bytes currently buffered, items currently owned, or work already performed with bytes/items/work still missing. If no declared signal distinguishes a bound pair, record an architecture_defect and call architecture_review_fail. API presence, compatible signatures, successful compilation, or hypothetical future implementation support are not semantic proof. Do not assume unspecified behavior, and do not require private algorithms or function bodies when declared semantics already compose.",
        "InputModel": MinionV2SkeletonBuilderOpMinionArchitectureReviewPassInput,
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


def compile_architecture_review_invocation_tool_contract(
    *,
    task_ledger: Mapping[str, Any],
    architecture: Mapping[str, Any],
) -> dict[str, Any]:
    del task_ledger
    modules = dict(architecture.get("modules") or {})
    scenarios = dict(architecture.get("scenarios") or {})
    obligations = dict(architecture.get("obligations") or {})
    module_names = sorted(str(name) for name in modules)
    obligation_names = sorted(str(name) for name in obligations)
    pass_example = _architecture_review_pass_example(architecture)
    overrides = {
        ADD_FINDING_CAPABILITY: {"use_when": (
            "Record one actionable architecture-review defect. Use requirements_defect for contradictory or "
            "unimplementable task-ledger semantics, contract_defect for an invalid public shape, and "
            "architecture_defect for topology, ownership, lifecycle, or scenario defects. Complete the "
            "breadth-first audit first, then issue all independent add_finding calls in one tool batch when possible. "
            "Available modules="
            f"{json.dumps(module_names, ensure_ascii=False)}; scenarios="
            f"{json.dumps(sorted(str(name) for name in scenarios), ensure_ascii=False)}; obligations="
            f"{json.dumps(obligation_names, ensure_ascii=False)}."
        )},
        "op_minion_architecture_review_fail": {"use_when": (
            "Submit FAIL with no arguments only after all material defects are present in the structured finding Draft."
        )},
        "op_minion_architecture_review_pass": {"use_when": (
            "Submit PASS only after independently reading task.yaml, auditing each revision against its embedded exact user answer, and reviewing every module and end-to-end scenario. "
            "Use the bound original-to-skeleton Git diff to detect removed, relocated, or narrowed public APIs and reject compatibility drift. "
            "For every stateful contract, rehearse where private state/resources live and how lifecycle/error paths close using only declared writable scopes; "
            "For every bound obligation, submit one compact obligation trace. Submit every Manager-bound same-decision-point, cross-outcome state pair exactly once; each side must use the bound entry_condition before optional progress, and reject a contract that has no declared public distinguishing signal. "
            "syntax and compilation are not semantic proof, and a missing legal storage seam is a material defect. "
            "Do not submit positive audit rows or partial findings."
        )},
    }
    return {
        "contract_version": "5",
        "module_names": module_names,
        "obligation_names": obligation_names,
        "obligations": obligations,
        "decision_pairs": _architecture_review_decision_pairs(architecture),
        "pass_example": pass_example,
        "guidance_overrides": overrides,
    }


def _architecture_review_pass_example(
    architecture: Mapping[str, Any],
) -> dict[str, Any]:
    obligations = dict(architecture.get("obligations") or {})
    modules = dict(architecture.get("modules") or {})
    scenarios = dict(architecture.get("scenarios") or {})
    changed_paths = [
        str(value).strip()
        for value in list(architecture.get("changed_paths") or [])
        if str(value).strip()
    ]

    def evidence_file(obligation: Mapping[str, Any]) -> str:
        owner = str(obligation.get("owner") or "")
        module_names = [owner] if owner in modules else [
            str(value)
            for value in list(dict(scenarios.get(owner) or {}).get("modules") or [])
        ]
        for module_name in module_names:
            paths = dict(dict(modules.get(module_name) or {}).get("paths") or {})
            contract_paths = [
                str(value).strip()
                for value in list(paths.get("contract_paths") or [])
                if str(value).strip()
            ]
            if contract_paths:
                return contract_paths[0]
        return changed_paths[0] if changed_paths else "replace/with/declared_contract.file"

    traces: list[dict[str, Any]] = []
    for name, raw_obligation in sorted(obligations.items()):
        obligation = dict(raw_obligation or {})
        source_file = evidence_file(obligation)
        expected_outcome = str(obligation.get("observable_outcome") or "").strip()
        contract_path = [
            (
                str(value).strip()
                if any(marker in str(value) for marker in ("#", "::", "->"))
                else str(value).strip() + "#public_contract -> observable_terminal"
            )
            for value in list(obligation.get("contract_path") or [])
            if str(value).strip()
        ]
        traces.append({
            "obligation": str(name),
            "contract_trace": contract_path or [
                "owner::public_interface -> observable_terminal"
            ],
            "boundary_summary": (
                "Replace with a compact conclusion covering every declared boundary partition."
            ),
            "evidence": [{"file": source_file, "line": 1}],
            "conclusion": (
                "Replace with why the declared public semantics compose for this obligation and its required outcome: "
                + expected_outcome
            ),
        })
    decision_traces = []
    for pair in _architecture_review_decision_pairs(architecture):
        left = dict(pair["left"])
        right = dict(pair["right"])
        decision_traces.append({
            "scenario": pair["scenario"],
            "decision_point": pair["decision_point"],
            "left": {
                "state_ref": left["state_ref"],
                "entry_witness": (
                    "Replace with the smallest concrete witness satisfying: "
                    + left["entry_condition"]
                ),
                "public_observation": "Replace with the exact public observation at entry.",
                "evidence": [{"file": evidence_file(obligations[left["obligation"]]), "line": 1}],
            },
            "right": {
                "state_ref": right["state_ref"],
                "entry_witness": (
                    "Replace with the smallest concrete witness satisfying: "
                    + right["entry_condition"]
                ),
                "public_observation": "Replace with the exact public observation at entry.",
                "evidence": [{"file": evidence_file(obligations[right["obligation"]]), "line": 1}],
            },
            "distinguishing_signal": (
                "Replace with the declared public signal that differs at these entry instants."
            ),
            "conclusion": (
                f"Replace with why {left['required_outcome']} remains distinguishable from "
                f"{right['required_outcome']}."
            ),
        })
    return {"obligation_traces": traces, "decision_traces": decision_traces}


def _architecture_review_decision_pairs(
    architecture: Mapping[str, Any],
) -> list[dict[str, Any]]:
    obligations = {
        str(name): dict(value or {})
        for name, value in dict(architecture.get("obligations") or {}).items()
    }
    pairs: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str, str]] = set()
    for scenario_name, raw_scenario in sorted(
        dict(architecture.get("scenarios") or {}).items()
    ):
        groups: dict[str, list[dict[str, str]]] = {}
        scenario = dict(raw_scenario or {})
        for obligation_name in list(scenario.get("obligations") or []):
            obligation_name = str(obligation_name)
            obligation = obligations.get(obligation_name, {})
            for state_name, raw_state in dict(obligation.get("states") or {}).items():
                state = dict(raw_state or {})
                decision_point = str(state.get("decision_point") or "").strip()
                groups.setdefault(decision_point, []).append({
                    "state_ref": f"{obligation_name}.{state_name}",
                    "obligation": obligation_name,
                    "state": str(state_name),
                    "entry_condition": str(state.get("entry_condition") or ""),
                    "outcome": str(state.get("outcome") or ""),
                    "required_outcome": str(state.get("required_outcome") or ""),
                })
        for decision_point, states in sorted(groups.items()):
            ordered = sorted(states, key=lambda item: item["state_ref"])
            for left_index, left in enumerate(ordered):
                for right in ordered[left_index + 1:]:
                    if left["outcome"] == right["outcome"]:
                        continue
                    pair_key = (
                        decision_point,
                        left["state_ref"],
                        right["state_ref"],
                    )
                    if pair_key in seen_pairs:
                        continue
                    seen_pairs.add(pair_key)
                    pairs.append({
                        "scenario": str(scenario_name),
                        "decision_point": decision_point,
                        "left": left,
                        "right": right,
                    })
    return pairs


def is_skeleton_builder_capability(name: str) -> bool:
    return str(name or "") in SKELETON_BUILDER_TOOL_SPECS


async def ask_question_tool_result(
    call: CanonicalToolCall,
    workspace: dict[str, Any],
    produced_artifacts: list[dict[str, Any]],
    *,
    request_user: Callable[[dict[str, Any], float | None], Awaitable[dict[str, Any]]] | None,
) -> CanonicalToolResult:
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
        timeout_raw = args.get("timeout_seconds")
        timeout = float(timeout_raw) if timeout_raw is not None else None
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
            timeout,
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
        authority = TaskRevisionAuthority(
            title=title,
            question=question,
            answer=answer,
            observed_at=datetime.now(UTC).isoformat(),
            origin="architect_user_clarification",
        )
        ref = await _publish_task_revision_authority(
            workspace,
            authority.model_dump_json().encode("utf-8"),
        )
        await _record_task_revision_authority(workspace, ref)
        draft_path = _seed_task_revision_draft(workspace)
        return CanonicalToolResult(
            name=call.name,
            ok=True,
            text=f"User answered: {answer}",
            llm_text=(
                f"User answered: {answer}\n"
                "The answer is authoritative but has not yet changed task semantics. "
                "Immediately replace task_revision.yaml with the smallest authorized semantic delta using "
                "artifact_edit(relative_path='task_revision.yaml', content=<complete YAML>, "
                "operation='replace', create_if_missing=false). JSON Pointer paths start inside "
                "task.yaml.original; never prefix them with /original. "
                "Then call task_revision_submit before continuing architecture work. "
                f"Draft path: {draft_path}"
            ),
            structured={
                "status": "answered_revision_required",
                "answer": answer,
                "task_revision_path": str(draft_path),
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


async def _publish_task_revision_authority(
    workspace: Mapping[str, Any],
    data: bytes,
) -> dict[str, Any]:
    from pal.minion.v2.role_gateway import role_gateway_client_from_env

    runtime_root = Path(str(workspace["runtime_root"]))
    gateway = role_gateway_client_from_env(runtime_root)
    if gateway is not None:
        response = await gateway.request(
            "artifact_put",
            {
                "data_base64": base64.b64encode(data).decode("ascii"),
                "artifact_type": "TaskRevisionAuthorityArtifact",
                "schema_version": "1",
                "media_type": "application/json",
            },
        )
        return dict(response.get("artifact_ref") or {})
    artifacts = ContentAddressedArtifactStore(runtime_root, MinionV2Repository(runtime_root))
    return artifacts.put_bytes(
        data,
        artifact_type="TaskRevisionAuthorityArtifact",
        media_type="application/json",
        provenance={"origin": "architect_user_clarification"},
    ).to_dict()


async def _record_task_revision_authority(
    workspace: Mapping[str, Any],
    authority_ref: Mapping[str, Any],
) -> None:
    from pal.minion.v2.role_gateway import role_gateway_client_from_env

    gateway = role_gateway_client_from_env(Path(str(workspace["runtime_root"])))
    if gateway is None:
        if isinstance(workspace, dict):
            workspace["_pending_task_revision_authority_ref"] = dict(authority_ref)
        return
    await gateway.request(
        "task_revision_authority_record",
        {"task_revision_authority_ref": dict(authority_ref)},
    )


def _task_revision_draft_path(workspace: Mapping[str, Any]) -> Path:
    root_value = str(
        workspace.get("artifact_stage_dir") or workspace.get("artifact_dir") or ""
    ).strip()
    if not root_value:
        raise ValueError("task revision requires workspace.artifact_dir")
    root = Path(root_value).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root / "task_revision.yaml"


def _seed_task_revision_draft(workspace: Mapping[str, Any]) -> Path:
    path = _task_revision_draft_path(workspace)
    path.write_text(task_revision_template_yaml(), encoding="utf-8")
    return path


def _submit_task_revision(
    call: CanonicalToolCall,
    workspace: dict[str, Any],
) -> CanonicalToolResult:
    if dict(call.args or {}):
        raise ValueError("task_revision_submit takes no arguments")
    revision = load_task_revision_yaml(_task_revision_draft_path(workspace))
    from pal.minion.v2.role_gateway import role_gateway_client_from_env

    gateway = role_gateway_client_from_env(Path(str(workspace["runtime_root"])))
    if gateway is None:
        if not workspace.get("_pending_task_revision_authority_ref"):
            raise ValueError("there is no pending user-authorized task revision")
        workspace.pop("_pending_task_revision_authority_ref", None)
        result = {"appended": True, "generation": 1, "local_test_fallback": True}
    else:
        result = gateway.request_sync("task_revision_append", {"revision": revision})
    public_result = {
        "appended": bool(result.get("appended")),
        "generation": int(result.get("generation") or 0),
        "duplicate": bool(result.get("duplicate")),
        "revision": revision,
    }
    return CanonicalToolResult(
        name=call.name,
        ok=True,
        text="Task revision appended",
        llm_text=(
            f"Task revision generation {public_result['generation']} is durably current. "
            "Continue the same Architect invocation by applying the exact delta you just submitted to the immutable task.yaml projection; "
            "later roles receive the refreshed single task.yaml ledger. Do not restate the whole task."
        ),
        structured=public_result,
        call_id=call.call_id,
        status=RuntimeStatus.OK,
    )


def skeleton_builder_tool_result(
    call: CanonicalToolCall,
    workspace: dict[str, Any],
    produced_artifacts: list[dict[str, Any]],
) -> CanonicalToolResult:
    try:
        name = str(call.name or "")
        if name == "op_minion_task_revision_submit":
            return _submit_task_revision(call, workspace)
        if name == "op_minion_architecture_submit":
            output, version = _compile_architecture_submission(call, workspace)
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
        try:
            _validate_revision_scope(output, workspace, changed_paths=changed_paths)
        except ValueError as exc:
            errors.append(exc)
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
    findings = structured_findings(snapshot.payload)
    verdict = "FAIL" if name == "op_minion_architecture_review_fail" else "PASS"
    obligation_traces: list[dict[str, Any]] = []
    decision_traces: list[dict[str, Any]] = []
    if verdict == "FAIL" and not findings:
        raise ValueError("architecture_review_fail requires at least one add_finding call")
    if verdict == "FAIL" and args:
        raise ValueError("architecture_review_fail takes no arguments")
    if verdict == "PASS" and findings:
        raise ValueError("architecture_review_pass requires an empty finding Draft")
    if verdict == "PASS":
        architecture = bound_reference_payload(
            workspace,
            "architecture_index",
            required=True,
        )
        obligation_traces, decision_traces = validate_architecture_review_obligation_traces(
            architecture,
            args.get("obligation_traces"),
            args.get("decision_traces"),
            workspace=workspace,
        )
    output = {
        "schema_version": "3",
        "verdict": verdict,
        "findings": findings,
        "review_scope": _compiled_review_scope(workspace),
        **({"obligation_traces": obligation_traces} if obligation_traces else {}),
        **({"decision_traces": decision_traces} if verdict == "PASS" else {}),
    }
    _validate_review_shape(output)
    _preflight_review_submission(output, workspace)
    return output, snapshot.version


def validate_architecture_review_obligation_traces(
    architecture: Mapping[str, Any],
    values: Any,
    decision_values: Any,
    *,
    workspace: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    obligations = {
        str(name): dict(value or {})
        for name, value in dict(architecture.get("obligations") or {}).items()
    }
    traces: dict[str, dict[str, Any]] = {}
    for raw_trace in list(values or []):
        trace = dict(raw_trace or {})
        name = str(trace.get("obligation") or "").strip()
        if not name:
            raise ValueError("architecture_review_pass obligation trace requires obligation")
        if name in traces:
            raise ValueError(
                f"architecture_review_pass obligation_traces contains duplicate: {name}"
            )
        traces[name] = trace
    if set(traces) != set(obligations):
        missing = sorted(set(obligations) - set(traces))
        extra = sorted(set(traces) - set(obligations))
        details = []
        if missing:
            details.append("missing=" + ", ".join(missing))
        if extra:
            details.append("unbound=" + ", ".join(extra))
        raise ValueError(
            "architecture_review_pass obligation_traces must exactly cover bound obligations"
            + (": " + "; ".join(details) if details else "")
        )

    allowed_contract_paths = {
        str(path).strip()
        for raw_module in dict(architecture.get("modules") or {}).values()
        for path in list(
            dict(dict(raw_module or {}).get("paths") or {}).get(
                "contract_paths"
            )
            or []
        )
        if str(path).strip()
    }
    repository_root = Path(str(workspace.get("repo_path") or "")).resolve()
    if not repository_root.is_dir():
        raise ValueError("architecture review evidence requires the bound review repository")

    def normalize_evidence(
        raw_locations: Any,
        *,
        label: str,
    ) -> list[dict[str, Any]]:
        normalized_locations: list[dict[str, Any]] = []
        for raw_location in list(raw_locations or []):
            location = dict(raw_location or {})
            file_name = str(location.get("file") or "").strip()
            relative = Path(file_name)
            if (
                not file_name
                or relative.is_absolute()
                or ".." in relative.parts
                or file_name not in allowed_contract_paths
            ):
                raise ValueError(
                    f"{label} evidence must cite a declared contract path: {file_name or '<empty>'}"
                )
            resolved = (repository_root / relative).resolve()
            try:
                resolved.relative_to(repository_root)
            except ValueError as exc:
                raise ValueError(f"{label} evidence escapes the review repository") from exc
            if not resolved.is_file():
                raise ValueError(f"{label} evidence file does not exist: {file_name}")
            try:
                source_lines = resolved.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError) as exc:
                raise ValueError(f"{label} evidence file is unreadable: {file_name}") from exc
            line = int(location.get("line") or 0)
            if line < 1 or line > len(source_lines):
                raise ValueError(
                    f"{label} evidence line is outside {file_name}: {line}"
                )
            source_text = source_lines[line - 1].strip()
            if not source_text:
                raise ValueError(
                    f"{label} evidence line is blank: {file_name}:{line}"
                )
            symbol = str(location.get("symbol") or "").strip()
            normalized_locations.append(
                {
                    "file": file_name,
                    "line": line,
                    **({"symbol": symbol} if symbol else {}),
                    "source_text": source_text,
                }
            )
        if not normalized_locations:
            raise ValueError(f"{label} requires contract evidence")
        return normalized_locations

    normalized: list[dict[str, Any]] = []
    for name in obligations:
        trace = traces[name]
        contract_trace = [
            str(value).strip()
            for value in list(trace.get("contract_trace") or [])
            if str(value).strip()
        ]
        if not contract_trace:
            raise ValueError(f"obligation {name} requires a public semantic contract_trace")
        bare_files = [
            value
            for value in contract_trace
            if ("/" in value or value.endswith((".h", ".hpp", ".py")))
            and not any(marker in value for marker in ("#", "::", "->"))
        ]
        if bare_files:
            raise ValueError(
                f"obligation {name} contract_trace must name interfaces/signals, not bare files: "
                + ", ".join(bare_files)
            )
        boundary_summary = str(trace.get("boundary_summary") or "").strip()
        if not boundary_summary:
            raise ValueError(f"obligation {name} requires a boundary_summary")
        conclusion = str(trace.get("conclusion") or "").strip()
        if not conclusion:
            raise ValueError(f"obligation {name} requires a semantic conclusion")
        normalized.append({
            "obligation": name,
            "contract_trace": contract_trace,
            "boundary_summary": boundary_summary,
            "evidence": normalize_evidence(
                trace.get("evidence"),
                label=f"obligation {name}",
            ),
            "conclusion": conclusion,
        })

    expected_pairs = {
        (
            str(item["scenario"]),
            str(item["decision_point"]),
            str(dict(item["left"])["state_ref"]),
            str(dict(item["right"])["state_ref"]),
        ): item
        for item in _architecture_review_decision_pairs(architecture)
    }
    submitted_pairs: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for raw_trace in list(decision_values or []):
        trace = dict(raw_trace or {})
        left = dict(trace.get("left") or {})
        right = dict(trace.get("right") or {})
        key = (
            str(trace.get("scenario") or "").strip(),
            str(trace.get("decision_point") or "").strip(),
            str(left.get("state_ref") or "").strip(),
            str(right.get("state_ref") or "").strip(),
        )
        if key in submitted_pairs:
            raise ValueError(
                "architecture_review_pass decision_traces contains duplicate: "
                + " / ".join(key)
            )
        submitted_pairs[key] = trace
    if set(submitted_pairs) != set(expected_pairs):
        missing = sorted(set(expected_pairs) - set(submitted_pairs))
        extra = sorted(set(submitted_pairs) - set(expected_pairs))
        details = []
        if missing:
            details.append(
                "missing=" + ", ".join(" / ".join(item) for item in missing)
            )
        if extra:
            details.append(
                "unbound=" + ", ".join(" / ".join(item) for item in extra)
            )
        raise ValueError(
            "architecture_review_pass decision_traces must exactly cover Manager-bound pairs"
            + (": " + "; ".join(details) if details else "")
        )

    normalized_decisions: list[dict[str, Any]] = []
    for key in expected_pairs:
        trace = submitted_pairs[key]

        def normalize_side(raw_side: Any, *, label: str) -> dict[str, Any]:
            side = dict(raw_side or {})
            entry_witness = str(side.get("entry_witness") or "").strip()
            public_observation = str(side.get("public_observation") or "").strip()
            if not entry_witness or not public_observation:
                raise ValueError(f"{label} requires entry_witness and public_observation")
            return {
                "state_ref": str(side.get("state_ref") or "").strip(),
                "entry_witness": entry_witness,
                "public_observation": public_observation,
                "evidence": normalize_evidence(
                    side.get("evidence"),
                    label=label,
                ),
            }

        distinguishing_signal = str(
            trace.get("distinguishing_signal") or ""
        ).strip()
        conclusion = str(trace.get("conclusion") or "").strip()
        if not distinguishing_signal or not conclusion:
            raise ValueError(
                "decision trace requires distinguishing_signal and conclusion: "
                + " / ".join(key)
            )
        normalized_decisions.append({
            "scenario": key[0],
            "decision_point": key[1],
            "left": normalize_side(
                trace.get("left"), label=f"decision {' / '.join(key)} left"
            ),
            "right": normalize_side(
                trace.get("right"), label=f"decision {' / '.join(key)} right"
            ),
            "distinguishing_signal": distinguishing_signal,
            "conclusion": conclusion,
        })
    return normalized, normalized_decisions


def _architecture_review_seed() -> dict[str, Any]:
    return empty_review_draft()


def _compiled_review_scope(workspace: Mapping[str, Any]) -> dict[str, Any]:
    architecture = bound_reference_payload(workspace, "architecture_index", required=False)
    return {
        "module_names": sorted(str(name) for name in dict(architecture.get("modules") or {})),
        "scenario_names": sorted(str(name) for name in dict(architecture.get("scenarios") or {})),
        "obligation_names": sorted(str(name) for name in dict(architecture.get("obligations") or {})),
    }


def _architecture_seed(workspace: Mapping[str, Any]) -> dict[str, Any]:
    del workspace
    return {
        "definitions": {"submission": {}},
        "evidence": {},
        "findings": [],
        "summary": {},
    }


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
        validate_architecture_changed_paths(
            normalized,
            _revision_changed_paths(repo_path, base_sha),
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
    if verdict not in {"PASS", "FAIL"}:
        raise ValueError("verdict must be PASS or FAIL")
    if verdict == "PASS" and findings:
        raise ValueError("PASS cannot contain findings")
    if verdict == "FAIL" and not findings:
        raise ValueError("FAIL requires findings")


def _preflight_review_submission(
    payload: Mapping[str, Any],
    workspace: Mapping[str, Any],
) -> None:
    del payload, workspace
