from __future__ import annotations

from pal.execution.generated_tool_models import (
    MinionV2SkeletonBuilderOpMinionAskQuestionInput,
    MinionV2SkeletonBuilderOpMinionArchitectureReviewFailInput,
    MinionV2SkeletonBuilderOpMinionArchitectureSubmitInput,
)

import json
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from pal.execution.tool_facade import EmptyToolInput
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
    "op_minion_architecture_submit": {
        "alias": "architecture_submit",
        "description": "Validate and submit the complete Manager-preseeded architecture.yaml together with the declaration skeleton. Takes no arguments. Its requirements, modules, and scenarios are dynamic snake_case maps. Define each module's responsibility, behavior kind, semantic dependencies and consumed provider outputs, input/output/error/invariant contract, ownership, lifecycle, optional state machine, and physical paths. Map each binding requirement to one owner and public contract path. Define end-to-end contract flows and success/failure observations. Submit performs strict YAML/schema validation, requirement closure, dependency output and scenario composition checks, path safety, Git-state checks, revision-scope checks, fencing, and snapshot stability. A rejected submit does not advance the workflow; correct the exact structured error path in architecture.yaml and retry. Architecture Reviewer owns semantic review.",
        "InputModel": MinionV2SkeletonBuilderOpMinionArchitectureSubmitInput,
    },
    "op_minion_architecture_review_pass": {
        "alias": "architecture_review_pass",
        "description": "Submit PASS with no arguments only after a breadth-first semantic review finds no blocking defect or unresolved public semantic ambiguity. Advisory p2 findings may remain. Confirm the ordered task ledger maps to the architecture, every module protocol is complete, ownership and lifecycle close, provider outputs satisfy consumer dependencies, scenario contract graphs can produce the required success and failure observations, and declarations/comments agree with architecture.yaml. Every observable input edge, partial-success failure path, and operation on public stateful values must have one declared result or an explicit set of outcomes safe for every consumer. Compilation is only supporting evidence that contracts compose; API presence, a successful probe, review_guarded implementation freedom, or hypothetical implementation behavior is not semantic proof.",
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
            "breadth-first audit first, then issue all independent add_finding calls in one tool batch when possible. "
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
            "Do not submit positive audit rows or partial findings. Advisory p2 findings may remain; "
            "PASS is forbidden only while a blocking finding remains. PASS takes no arguments."
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
