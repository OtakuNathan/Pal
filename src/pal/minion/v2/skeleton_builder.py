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

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.v2.artifacts import ContentAddressedArtifactStore
from pal.minion.v2.architecture_yaml import (
    ArchitectureDraftFileError,
    load_architecture_draft,
)
from pal.minion.v2.repository import MinionV2Repository
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
        "description": "Use this tool proactively before architecture design or revision when task sources contain or may contain a contradiction, material ambiguity, incorrect or infeasible requirement, or missing decision; when the result depends on user preference; or when a choice would materially change product behavior, compatibility, architecture, modification scope, or implementation scope. Do not silently choose precedence, repair or reinterpret a Requirement, or widen or narrow scope. Ask one decisive question without ending the Architect invocation: identify the exact issue and the decision needed, then supply a short title, a precise question, and three option strings. Each option must contain a readable choice and its impact or tradeoff. The channel also permits a custom free-text answer. Calling this tool suspends the current tool call until the user answers, the long timeout expires, or the invocation is paused, cancelled, or restarted. The answer becomes an immutable task-source amendment shared with Reviewer, Coder, and Verifier.",
        "InputModel": MinionV2SkeletonBuilderOpMinionAskQuestionInput,
    },
    "op_minion_architecture_submit": {
        "alias": "architecture_submit",
        "description": "Validate and submit the complete Manager-preseeded architecture.yaml together with the declaration skeleton. Takes no arguments. The YAML uses dynamic snake_case maps for modules and scenarios, so add, edit, or delete entries with ordinary file tools. Submit performs strict YAML/schema validation, dependency and scenario checks, path safety, Git-state checks, revision-scope checks, fencing, and snapshot stability. A rejected submit does not advance the workflow; correct the exact structured error path in architecture.yaml and retry. Architecture Reviewer owns semantic review.",
        "InputModel": MinionV2SkeletonBuilderOpMinionArchitectureSubmitInput,
    },
    "op_minion_architecture_review_pass": {
        "alias": "architecture_review_pass",
        "description": "Submit PASS only after one breadth-first review of every immutable task source, module contract, semantic dependency, and end-to-end scenario finds no material defect. For every Requirement and observable scenario claim, manually trace the declared interface semantics from a concrete entrypoint through data, ownership, state, error, cleanup, and output boundaries to the required legal terminal. API presence, compatible signatures, successful compilation, or hypothetical future implementation support are not semantic proof. Do not assume unspecified behavior, and do not require private algorithms or function bodies when the declared semantics already compose.",
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
    task_sources: Mapping[str, Any],
    architecture: Mapping[str, Any],
) -> dict[str, Any]:
    del task_sources
    modules = dict(architecture.get("modules") or {})
    scenarios = dict(architecture.get("scenarios") or {})
    module_names = sorted(str(name) for name in modules)
    overrides = {
        ADD_FINDING_CAPABILITY: {"use_when": (
            "Record one actionable architecture-review defect. Use requirements_defect for contradictory or "
            "unimplementable task-source semantics, contract_defect for an invalid public shape, and "
            "architecture_defect for topology, ownership, lifecycle, or scenario defects. Complete the "
            "breadth-first audit first, then issue all independent add_finding calls in one tool batch when possible. "
            "Available modules="
            f"{json.dumps(module_names, ensure_ascii=False)}; scenarios="
            f"{json.dumps(sorted(str(name) for name in scenarios), ensure_ascii=False)}."
        )},
        "op_minion_architecture_review_fail": {"use_when": (
            "Submit FAIL with no arguments only after all material defects are present in the structured finding Draft."
        )},
        "op_minion_architecture_review_pass": {"use_when": (
            "Submit PASS only after independently reading every bound task source and reviewing every module and end-to-end scenario. "
            "Use the bound original-to-skeleton Git diff to detect removed, relocated, or narrowed public APIs and reject compatibility drift. "
            "For every stateful contract, rehearse where private state/resources live and how lifecycle/error paths close using only declared writable scopes; "
            "syntax and compilation are not semantic proof, and a missing legal storage seam is a material defect. "
            "Do not submit positive audit rows or partial findings."
        )},
    }
    return {
        "contract_version": "1",
        "module_names": module_names,
        "guidance_overrides": overrides,
    }


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
        answer = str(answers[0].get("answer") or "").strip() if answers else ""
        if not answer:
            return CanonicalToolResult(
                name=call.name,
                ok=False,
                text="Architect user question timed out or was cancelled",
                llm_text="The user did not answer. Keep the ambiguity explicit; do not submit an architecture that guesses the answer.",
                structured={"status": "timed_out_or_cancelled"},
                call_id=call.call_id,
                status=RuntimeStatus.ERROR,
            )
        observed_at = datetime.now(UTC).isoformat()
        content = "\n".join(
            (
                f"# {title}",
                "",
                f"Question: {question}",
                "",
                f"User answer: {answer}",
                "",
                f"Observed at: {observed_at}",
                "Origin: architect_user_clarification",
                "",
            )
        )
        ref = await _publish_architecture_clarification(
            workspace,
            content.encode("utf-8"),
        )
        _append_architecture_clarification_to_draft(
            workspace,
            call=call,
            clarification_ref=ref,
        )
        artifact = _write_minion_artifact(
            workspace,
            {
                "relative_path": f"clarifications/{ref['sha256'][:16]}.md",
                "title": title,
                "role": "supporting",
                "mime_type": "text/markdown",
                "overwrite": True,
                "content": content,
            },
        )
        _append_unique_artifact(produced_artifacts, artifact)
        return CanonicalToolResult(
            name=call.name,
            ok=True,
            text=f"User answered: {answer}",
            llm_text=(
                f"User answered: {answer}\n"
                "This answer is now an immutable task-source amendment for this revision. Continue in the same invocation."
            ),
            structured={"status": "answered", "answer": answer},
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


async def _publish_architecture_clarification(
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
                "artifact_type": "TaskSourceAmendmentArtifact",
                "schema_version": "1",
                "media_type": "text/markdown",
            },
        )
        return dict(response.get("artifact_ref") or {})
    artifacts = ContentAddressedArtifactStore(runtime_root, MinionV2Repository(runtime_root))
    return artifacts.put_bytes(
        data,
        artifact_type="TaskSourceAmendmentArtifact",
        media_type="text/markdown",
        provenance={"origin": "architect_user_clarification"},
    ).to_dict()


def _append_architecture_clarification_to_draft(
    workspace: Mapping[str, Any],
    *,
    call: CanonicalToolCall,
    clarification_ref: Mapping[str, Any],
) -> None:
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind="architecture")
    store = SubmissionDraftStore(Path(str(workspace["runtime_root"])))

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        definitions = dict(payload.get("definitions") or {})
        submission = dict(definitions.get("submission") or {"modules": {}, "scenarios": {}})
        refs = [
            dict(item)
            for item in list(submission.get("clarification_refs") or [])
            if isinstance(item, Mapping) and item.get("sha256")
        ]
        if str(clarification_ref.get("sha256") or "") not in {
            str(item.get("sha256") or "") for item in refs
        }:
            refs.append(dict(clarification_ref))
        submission["clarification_refs"] = refs
        definitions["submission"] = submission
        payload["definitions"] = definitions
        return payload, {"recorded": True, "clarification_count": len(refs)}

    store.mutate(
        context,
        operation_key=str(call.call_id or f"architect-question:{clarification_ref.get('sha256', '')}"),
        request=dict(call.args or {}),
        reducer=reducer,
        seed=_architecture_seed(workspace),
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
    internal_submission = dict(
        dict(snapshot.payload.get("definitions") or {}).get("submission") or {}
    )
    clarification_refs = [
        dict(item)
        for item in list(internal_submission.get("clarification_refs") or [])
        if isinstance(item, Mapping) and item.get("sha256")
    ]
    if clarification_refs:
        compiled["clarification_refs"] = clarification_refs
    return compiled, snapshot.version


def _compile_architecture_review(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
) -> tuple[dict[str, Any], int]:
    name = str(call.name or "")
    args = dict(call.args or {})
    if args:
        raise ValueError(f"{name} takes no arguments")
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind="architecture_review")
    snapshot = SubmissionDraftStore(Path(str(workspace["runtime_root"]))).read(
        context,
        seed=_architecture_review_seed(),
    )
    findings = structured_findings(snapshot.payload)
    verdict = "FAIL" if name == "op_minion_architecture_review_fail" else "PASS"
    if verdict == "FAIL" and not findings:
        raise ValueError("architecture_review_fail requires at least one add_finding call")
    if verdict == "PASS" and findings:
        raise ValueError("architecture_review_pass requires an empty finding Draft")
    output = {
        "schema_version": "2",
        "verdict": verdict,
        "findings": findings,
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
    }


def _architecture_seed(workspace: Mapping[str, Any]) -> dict[str, Any]:
    del workspace
    return {
        "definitions": {"submission": {"clarification_refs": []}},
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
