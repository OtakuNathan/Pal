from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.contracts import AggregateType
from pal.bunshin.service import BunshinWorkflowService
from pal.bunshin.swe_verification import semantic_verification_submission_errors, verification_corpus_files, verification_scratch_paths, verification_workspace_changed_paths


def validate_verification_submission(
    service: BunshinWorkflowService,
    load_prompt_pack: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    authenticated: Mapping[str, Any],
    assignment: Mapping[str, Any],
    submission: Mapping[str, Any],
    *, case_revision: int = 0,
) -> None:
    """Reject correctable verifier output before freezing its Draft."""

    if str(assignment.get("aggregate_type") or "") != AggregateType.DAG_NODE_RUN.value:
        return
    node = service.repository.snapshots.read_snapshot(
        AggregateType.DAG_NODE_RUN,
        str(assignment.get("aggregate_id") or ""),
    )
    if node is None:
        return
    view_value = node.payload.get("unit_work_view_ref")
    if not isinstance(view_value, Mapping) or not view_value.get("sha256"):
        return
    try:
        work_view = service.artifacts.read_json(dict(view_value))
    except Exception:
        # Manager-owned identity/corpus corruption is not correctable by
        # the live role.  Preserve the durable post-submit invariant path
        # instead of feeding the model an impossible retry instruction.
        return
    if not isinstance(work_view, Mapping):
        return

    prompt_pack = load_prompt_pack(authenticated)
    workspace = dict(prompt_pack.get("workspace") or {})
    workspace["bunshin"] = {
        **dict(dict(prompt_pack.get("metadata") or {}).get("bunshin") or {}),
        **dict(workspace.get("bunshin") or {}),
    }
    from pal.bunshin.semantic_orchestration.verification_policy import _verification_repair_scope

    workspace["bunshin"]["swe_verification_tool_contract"] = {
        **dict(workspace["bunshin"].get("swe_verification_tool_contract") or {}),
        **_verification_repair_scope(service.repository, node),
    }
    workspace["verification_case_revision"] = case_revision
    review_workspace = Path(str(workspace.get("repo_path") or ""))
    review_scratch = Path(str(workspace.get("review_scratch_dir") or ""))
    scratch_only = bool(workspace.get("verification_scratch_only"))
    corpus_scope = dict(
        dict(node.payload.get("path_policy") or {}).get(
            "verification_corpus"
        )
        or {}
    )
    if scratch_only:
        changed_paths = verification_scratch_paths(review_scratch)
        current_case_paths = list(changed_paths)
    else:
        candidate_digest = str(node.payload.get("candidate_digest") or "").strip()
        if not candidate_digest:
            return
        if not review_workspace.is_dir():
            return
        try:
            changed_paths = verification_workspace_changed_paths(
                review_workspace,
                candidate_digest,
            )
        except Exception:
            return
        current_case_paths = verification_corpus_files(
            review_workspace,
            corpus_scope,
        )
    errors = semantic_verification_submission_errors(
        submission,
        work_view=dict(work_view),
        changed_paths=changed_paths,
        current_case_paths=current_case_paths,
        corpus_scope=corpus_scope,
        scratch_only=scratch_only,
        workspace=workspace,
    )
    if errors:
        raise ValueError(
            "verification submission rejected before durable receipt:\n- "
            + "\n- ".join(errors)
        )
