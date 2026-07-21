from __future__ import annotations

from pal.execution.generated_tool_models import (
    MinionV2SkeletonBuilderOpMinionArchitectureAskUserInput,
    MinionV2SkeletonBuilderOpMinionArchitectureModuleRemoveInput,
    MinionV2SkeletonBuilderOpMinionArchitectureModuleUpsertInput,
    MinionV2SkeletonBuilderOpMinionArchitectureReviewFailInput,
    MinionV2SkeletonBuilderOpMinionArchitectureReviewPassInput,
    MinionV2SkeletonBuilderOpMinionArchitectureScenarioRemoveInput,
    MinionV2SkeletonBuilderOpMinionArchitectureScenarioUpsertInput,
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
    ArchitectureValidationResult,
    MODULE_KINDS,
    MODULE_NAME_PATTERN,
    SemanticReferenceError,
    analyze_architecture_submission,
    architecture_revision_changed_paths_since,
    validate_architecture_revision_scope,
    validate_architecture_changed_paths,
)
from pal.minion.workspace_tools import _append_unique_artifact, _write_minion_artifact
from pal.shared import RuntimeStatus


ARCHITECTURE_SKELETON_CAPABILITIES = (
    "op_minion_architecture_module_upsert",
    "op_minion_architecture_module_remove",
    "op_minion_architecture_scenario_upsert",
    "op_minion_architecture_scenario_remove",
    "op_minion_architecture_ask_user",
    "op_minion_architecture_submit",
)
SKELETON_REVIEW_CAPABILITIES = (
    ADD_FINDING_CAPABILITY,
    "op_minion_architecture_review_pass",
    "op_minion_architecture_review_fail",
)
SKELETON_BUILDER_CAPABILITIES = (*ARCHITECTURE_SKELETON_CAPABILITIES, *SKELETON_REVIEW_CAPABILITIES)

_MODULE_UPSERT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "module_kind": {"type": "string", "enum": ["implementation", "contract_only"]},
        "contract_mode": {"type": "string", "enum": ["file_frozen", "review_guarded"]},
        "contract_dependencies": {"type": "array", "items": {"type": "string"}},
        "contract_paths": {"type": "array", "items": {"type": "string"}},
        "implementation_files": {"type": "array", "items": {"type": "string"}},
        "implementation_directories": {"type": "array", "items": {"type": "string"}},
        "test_files": {"type": "array", "items": {"type": "string"}},
        "test_directories": {"type": "array", "items": {"type": "string"}},
        "reference_only": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "module_kind", "contract_dependencies", "contract_paths"],
    "additionalProperties": False,
}
_NAME_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string", "minLength": 1}},
    "required": ["name"],
    "additionalProperties": False,
}
_SCENARIO_UPSERT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "modules": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "uniqueItems": True,
        },
        "entrypoint": {"type": "string", "minLength": 1},
        "observable_behavior": {"type": "string", "minLength": 1},
        "environment": {"type": "string", "minLength": 1},
    },
    "required": ["name", "modules", "entrypoint", "observable_behavior", "environment"],
    "additionalProperties": False,
}
_ARCHITECT_QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1},
        "question": {"type": "string", "minLength": 1},
        "option_1": {"type": "string", "minLength": 1},
        "option_2": {"type": "string", "minLength": 1},
        "option_3": {"type": "string", "minLength": 1},
        "timeout_seconds": {"type": ["number", "null"], "minimum": 1},
    },
    "required": ["title", "question", "option_1", "option_2", "option_3"],
    "additionalProperties": False,
}
_NO_ARGS_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}

SKELETON_BUILDER_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_minion_architecture_module_upsert": {
        "name": "op_architecture_module_upsert",
        "description": "Create or fully replace one semantic module. contract_dependencies describe the acyclic production/protocol consumption graph; they do not delay Coder startup. implementation modules default to review_guarded when contract_mode is omitted. Use file_frozen only for a physically separate protocol/interface/schema file. Contract paths may never be owned by test scopes. contract_only modules are already complete in the Skeleton, are automatically file_frozen, and have no writable scopes. Product semantics live in the Skeleton source and are reviewed independently.",
        "InputModel": MinionV2SkeletonBuilderOpMinionArchitectureModuleUpsertInput,
    },
    "op_minion_architecture_module_remove": {
        "name": "op_architecture_module_remove",
        "description": "Remove one semantic module during a scoped revision. Dependencies must be corrected before submit.",
        "InputModel": MinionV2SkeletonBuilderOpMinionArchitectureModuleRemoveInput,
    },
    "op_minion_architecture_scenario_upsert": {
        "name": "op_architecture_scenario_upsert",
        "description": "Create or fully replace one required end-to-end scenario. Name the exact implementation modules used by a real product entrypoint, the externally observable behavior it proves, and the execution environment. This is a semantic verification scenario, not a synthetic all-module join and not a test-case implementation.",
        "InputModel": MinionV2SkeletonBuilderOpMinionArchitectureScenarioUpsertInput,
    },
    "op_minion_architecture_scenario_remove": {
        "name": "op_architecture_scenario_remove",
        "description": "Remove one end-to-end scenario by semantic name during architecture revision.",
        "InputModel": MinionV2SkeletonBuilderOpMinionArchitectureScenarioRemoveInput,
    },
    "op_minion_architecture_ask_user": {
        "name": "op_architecture_ask_user",
        "description": "Ask one material Requirement, preference, or high-impact architecture question without ending the Architect invocation. Supply a short title, a precise question, and three option strings; each option must contain a readable choice and its impact/tradeoff. The channel also permits a custom free-text answer. Omit timeout_seconds to wait until answer, pause, or cancel. The answer becomes an immutable task-source amendment shared with Reviewer, Coder, and Verifier.",
        "InputModel": MinionV2SkeletonBuilderOpMinionArchitectureAskUserInput,
    },
    "op_minion_architecture_submit": {
        "name": "op_architecture_submit",
        "description": "Preflight and submit the current code skeleton, semantic Contract Dependency Graph, and end-to-end scenarios. Takes no arguments. Manager checks only deterministic structure, path safety, Git state, and snapshot stability; Architecture Reviewer owns all semantic review.",
        "InputModel": MinionV2SkeletonBuilderOpMinionArchitectureSubmitInput,
    },
    "op_minion_architecture_review_pass": {
        "name": "op_architecture_review_pass",
        "description": "Submit PASS only after one breadth-first review of every immutable task source, module contract, semantic dependency, and end-to-end scenario finds no material defect.",
        "InputModel": MinionV2SkeletonBuilderOpMinionArchitectureReviewPassInput,
    },
    "op_minion_architecture_review_fail": {
        "name": "op_architecture_review_fail",
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
        ADD_FINDING_CAPABILITY: (
            "Record one actionable architecture-review defect. Use requirements_defect for contradictory or "
            "unimplementable task-source semantics, contract_defect for an invalid public shape, and "
            "architecture_defect for topology, ownership, lifecycle, or scenario defects. Complete the "
            "breadth-first audit first, then issue all independent add_finding calls in one tool batch when possible. "
            "Available modules="
            f"{json.dumps(module_names, ensure_ascii=False)}; scenarios="
            f"{json.dumps(sorted(str(name) for name in scenarios), ensure_ascii=False)}."
        ),
        "op_minion_architecture_review_fail": (
            "Submit FAIL with no arguments only after all material defects are present in the structured finding Draft."
        ),
        "op_minion_architecture_review_pass": (
            "Submit PASS only after independently reading every bound task source and reviewing every module and end-to-end scenario. "
            "Use the bound original-to-skeleton Git diff to detect removed, relocated, or narrowed public APIs and reject compatibility drift. "
            "For every stateful contract, rehearse where private state/resources live and how lifecycle/error paths close using only declared writable scopes; "
            "syntax and compilation are not semantic proof, and a missing legal storage seam is a material defect. "
            "Do not submit positive audit rows or partial findings."
        ),
    }
    return {
        "contract_version": "1",
        "module_names": module_names,
        "description_overrides": overrides,
    }


def is_skeleton_builder_capability(name: str) -> bool:
    return str(name or "") in SKELETON_BUILDER_TOOL_SPECS


async def architecture_question_tool_result(
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
            raise ValueError("architecture_ask_user requires title and question")
        normalized_options: list[dict[str, str]] = []
        for index in range(1, 4):
            option = str(args.get(f"option_{index}") or "").strip()
            if not option:
                raise ValueError(f"architecture_ask_user requires option_{index}")
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
            return _mutate_architecture_draft(call, workspace)
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
            if isinstance(exc, SemanticReferenceError)
            else {"error": str(exc), "error_type": exc.__class__.__name__}
        )
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=message,
            llm_text=message + " Correct only this local semantic field and retry in the same invocation.",
            structured=structured,
            call_id=call.call_id,
            status=RuntimeStatus.INVALID,
        )


def _mutate_architecture_draft(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
) -> CanonicalToolResult:
    name = str(call.name or "")
    args = dict(call.args or {})
    if name not in set(ARCHITECTURE_SKELETON_CAPABILITIES) - {"op_minion_architecture_submit"}:
        raise ValueError(f"unknown architecture authoring capability: {name}")
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind="architecture")
    store = SubmissionDraftStore(Path(str(workspace["runtime_root"])))

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        definitions = dict(payload.get("definitions") or {})
        submission = dict(definitions.get("submission") or {"modules": {}, "scenarios": {}})
        modules = dict(submission.get("modules") or {})
        scenarios = dict(submission.get("scenarios") or {})
        if name == "op_minion_architecture_module_upsert":
            module_name = _semantic_name(args, "name")
            modules[module_name] = {
                "module_kind": str(args.get("module_kind") or ""),
                "contract_dependencies": _string_array(
                    args.get("contract_dependencies") or [],
                    owner="contract_dependencies",
                ),
                "paths": {
                    "contract_mode": str(args.get("contract_mode") or ""),
                    "contract_paths": _string_array(args.get("contract_paths") or [], owner="contract_paths"),
                    "implementation_scopes": _path_scopes(args, "implementation"),
                    "test_scopes": _path_scopes(args, "test"),
                    "reference_only": _string_array(args.get("reference_only") or [], owner="reference_only"),
                },
            }
        elif name == "op_minion_architecture_module_remove":
            module_name = _semantic_name(args, "name")
            if module_name not in modules:
                raise ValueError(f"unknown module: {module_name}")
            del modules[module_name]
        elif name == "op_minion_architecture_scenario_upsert":
            scenario_name = _semantic_name(args, "name")
            scenarios[scenario_name] = {
                "modules": _unique_strings(args.get("modules") or [], owner="modules"),
                "entrypoint": str(args.get("entrypoint") or "").strip(),
                "observable_behavior": str(args.get("observable_behavior") or "").strip(),
                "environment": str(args.get("environment") or "").strip(),
            }
            for field in ("entrypoint", "observable_behavior", "environment"):
                if not scenarios[scenario_name][field]:
                    raise ValueError(f"scenario {scenario_name} requires {field}")
        elif name == "op_minion_architecture_scenario_remove":
            scenario_name = _semantic_name(args, "name")
            if scenario_name not in scenarios:
                raise ValueError(f"unknown scenario: {scenario_name}")
            del scenarios[scenario_name]
        elif name == "op_minion_architecture_ask_user":
            raise ValueError("architecture_ask_user requires the async user-interaction binding")
        submission["modules"] = modules
        submission["scenarios"] = scenarios
        definitions["submission"] = submission
        payload["definitions"] = definitions
        return payload, {
            "updated": True,
            "module_count": len(modules),
            "scenario_count": len(scenarios),
        }

    result = store.mutate(
        context,
        operation_key=str(call.call_id or f"{name}:{json.dumps(args, sort_keys=True)}"),
        request=args,
        reducer=reducer,
        seed=_architecture_seed(workspace),
    )
    return CanonicalToolResult(
        name=call.name,
        ok=True,
        text="architecture Draft updated",
        llm_text="Architecture Draft updated. Continue with the next semantic unit; submit only after the skeleton and topology are complete.",
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
    output = dict(dict(snapshot.payload.get("definitions") or {}).get("submission") or {})
    errors: list[Any] = []
    report: ArchitectureValidationResult | None = None
    try:
        _validate_submission_shape(output)
    except (SemanticReferenceError, ValueError) as exc:
        errors.append(exc)
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
    clarification_refs = [
        dict(item)
        for item in list(output.get("clarification_refs") or [])
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
    base = workspace.get("architecture_revision_base_submission")
    submission = (
        json.loads(json.dumps(dict(base)))
        if isinstance(base, Mapping)
        else {"modules": {}, "scenarios": {}}
    )
    return {"definitions": {"submission": submission}, "evidence": {}, "findings": [], "summary": {}}


def _path_scopes(args: Mapping[str, Any], prefix: str) -> list[dict[str, str]]:
    result = [
        {"kind": "file", "path": value}
        for value in _string_array(args.get(f"{prefix}_files") or [], owner=f"{prefix}_files")
    ]
    result.extend(
        {"kind": "directory", "path": value}
        for value in _string_array(args.get(f"{prefix}_directories") or [], owner=f"{prefix}_directories")
    )
    return result


def _semantic_name(args: Mapping[str, Any], field: str) -> str:
    value = str(args.get(field) or "").strip()
    if MODULE_NAME_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be a stable snake_case semantic name")
    return value


def _string_array(value: Any, *, owner: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{owner} must be a string array")
    return [str(item) for item in value]


def _unique_strings(value: Any, *, owner: str) -> list[str]:
    values = [item.strip() for item in _string_array(value, owner=owner)]
    if any(not item for item in values):
        raise ValueError(f"{owner} must not contain empty values")
    return list(dict.fromkeys(values))


def _validate_submission_shape(payload: Mapping[str, Any]) -> None:
    errors: list[str] = []
    modules = payload.get("modules")
    if not isinstance(modules, Mapping) or not modules:
        raise ValueError("modules must be a non-empty map")
    names = {str(name) for name in modules}
    for name, raw_module in modules.items():
        if MODULE_NAME_PATTERN.fullmatch(str(name)) is None:
            errors.append(f"invalid semantic module name: {name}")
        if not isinstance(raw_module, Mapping):
            errors.append(f"module {name} must be an object")
            continue
        module = dict(raw_module or {})
        missing = {"module_kind", "contract_dependencies", "paths"} - set(module)
        if missing:
            errors.append(f"module {name} is missing: {', '.join(sorted(missing))}")
        unknown = set(
            str(item) for item in list(module.get("contract_dependencies") or [])
        ) - names
        if unknown:
            errors.append(f"module {name} references unknown dependencies: {', '.join(sorted(unknown))}")
        paths = dict(module.get("paths") or {})
        default_mode = "file_frozen" if str(module.get("module_kind") or "") == "contract_only" else "review_guarded"
        contract_mode = str(paths.get("contract_mode") or default_mode)
        if contract_mode not in {"file_frozen", "review_guarded"}:
            errors.append(f"module {name} has invalid paths.contract_mode: {contract_mode or '<empty>'}")
        if not list(paths.get("contract_paths") or []):
            errors.append(f"module {name} requires paths.contract_paths")
        module_kind = str(module.get("module_kind") or "")
        if module_kind not in MODULE_KINDS:
            errors.append(f"module {name} has invalid module_kind: {module_kind or '<empty>'}")
        writable = [
            *list(paths.get("implementation_scopes") or []),
            *list(paths.get("test_scopes") or []),
        ]
        if module_kind == "implementation":
            for field in ("implementation_scopes", "test_scopes"):
                if not list(paths.get(field) or []):
                    errors.append(f"implementation module {name} requires paths.{field}")
        else:
            if contract_mode != "file_frozen":
                errors.append(f"contract_only module {name} must use file_frozen contract mode")
            if writable:
                errors.append(
                    f"contract_only module {name} cannot declare implementation_scopes or test_scopes"
                )
    try:
        _assert_acyclic(
            {
                str(name): [
                    str(item)
                    for item in list(dict(module).get("contract_dependencies") or [])
                ]
                for name, module in modules.items()
                if isinstance(module, Mapping)
            }
        )
    except ValueError as exc:
        errors.append(str(exc))
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, Mapping) or not scenarios:
        errors.append("architecture submission requires at least one end-to-end scenario")
    else:
        implementation_names = {
            str(name)
            for name, raw_module in modules.items()
            if isinstance(raw_module, Mapping)
            and str(dict(raw_module).get("module_kind") or "") == "implementation"
        }
        for name, raw_scenario in scenarios.items():
            if MODULE_NAME_PATTERN.fullmatch(str(name)) is None:
                errors.append(f"invalid semantic scenario name: {name}")
                continue
            scenario = dict(raw_scenario or {}) if isinstance(raw_scenario, Mapping) else {}
            used = _unique_strings(scenario.get("modules") or [], owner=f"scenario {name} modules")
            if not used:
                errors.append(f"scenario {name} requires at least one implementation module")
            unknown_modules = sorted(set(used) - implementation_names)
            if unknown_modules:
                errors.append(
                    f"scenario {name} references unknown or non-implementation modules: "
                    + ", ".join(unknown_modules)
                )
            for field in ("entrypoint", "observable_behavior", "environment"):
                if not str(scenario.get(field) or "").strip():
                    errors.append(f"scenario {name} requires {field}")
    raise_submission_errors(errors, owner="architecture submission shape")


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


def _assert_acyclic(depends_on: Mapping[str, list[str]]) -> None:
    pending = {name: set(values) for name, values in depends_on.items()}
    while pending:
        ready = {name for name, values in pending.items() if not values}
        if not ready:
            raise ValueError("module dependency graph contains a cycle: " + ", ".join(sorted(pending)))
        pending = {name: values - ready for name, values in pending.items() if name not in ready}
