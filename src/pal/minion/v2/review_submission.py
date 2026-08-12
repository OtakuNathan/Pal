from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR

from pathlib import Path
from typing import Any, Mapping

from pal.execution.tool_facade import EmptyToolInput, rejection
from pal.minion.v2.review_findings import partition_findings
from pal.minion.v2.submission_drafts import (
    SubmissionDraftContext,
    SubmissionDraftStore,
)
from pal.minion.v2.work_items import (
    assert_work_items_complete,
    findings_from_work_items,
    submission_work_items,
)
from pal.shared import RuntimeStatus, ToolExecutionResult


REVIEW_SUBMIT_CAPABILITY = "op_minion_review_submit"

REVIEW_SUBMIT_TOOL_SPEC: dict[str, Any] = {
    "alias": "review_submit",
    "guidance": {
        "purpose": "Submit the completed semantic review and let the Manager derive its verdict.",
        "use_when": (
            "Use with no arguments after the complete audit is finished, every finding is "
            "recorded with add_finding, and the checklist is complete."
        ),
        "do_not_use_when": (
            "Do not submit before breadth and composition review finish, emit a separate "
            "Markdown verdict, or treat a blocking p2 finding as PASS."
        ),
        "failure_next_steps": (
            "Complete or correct the checklist and structured findings reported by the "
            "rejection, then submit again without inventing a separate verdict."
        ),
    },
    "InputModel": EmptyToolInput,
    "examples": (),
    "idempotency": "idempotent",
    "retry_policy": "automatic",
}


def review_submit_tool_result(
    call: ToolCallIR,
    workspace: Mapping[str, Any],
) -> ToolExecutionResult:
    try:
        if dict(call.args or {}):
            raise ValueError("review_submit takes no arguments")
        ledger = assert_work_items_complete(workspace)
        findings = findings_from_work_items(workspace)
        blocking, advisories = partition_findings(findings)
        binding = dict(workspace.get("minion_v2") or {})
        role = str(binding.get("role") or "")
        mode = str(binding.get("mode") or "")
        if role != "reviewer":
            raise ValueError("review_submit is available only to reviewer roles")
        draft_kind = (
            "architecture_review"
            if mode == "architecture"
            else "standalone_review"
        )
        context = SubmissionDraftContext.from_workspace(
            workspace,
            draft_kind=draft_kind,
        )
        store = SubmissionDraftStore(_runtime_root(workspace))
        snapshot = store.read(context, seed={})
        payload = {
            "schema_version": "1",
            "verdict": "FAIL" if blocking else "PASS",
            "findings": blocking,
            "advisories": advisories,
            "work_items": submission_work_items(ledger["items"]),
        }
        result = store.mark_submitted(
            context,
            expected_version=snapshot.version,
            submission_payload=payload,
        )
        text = (
            f"Review {payload['verdict']} submitted with "
            f"{len(blocking)} blocking finding(s) and "
            f"{len(advisories)} advisory finding(s)."
        )
        return ToolExecutionResult(
            name=call.name,
            ok=True,
            text=text,
            llm_text=text,
            structured=dict(result),
            call_id=call.call_id,
            status=RuntimeStatus.OK,
        )
    except Exception as exc:
        text = f"{exc.__class__.__name__}: {exc}"
        llm_text = f"{text} Complete the audit/checklist and retry."
        return ToolExecutionResult(
            name=call.name,
            ok=False,
            text=text,
            llm_text=llm_text,
            structured={"error": str(exc), "error_type": exc.__class__.__name__},
            call_id=call.call_id,
            status=RuntimeStatus.INVALID,
            invocation_result=rejection(
                "invalid_review_submission",
                llm_text,
                details={
                    "error": str(exc),
                    "error_type": exc.__class__.__name__,
                },
            ),
        )


def _runtime_root(workspace: Mapping[str, Any]) -> Path:
    value = str(workspace.get("runtime_root") or "").strip()
    if not value:
        raise ValueError("review submission requires runtime_root")
    return Path(value)
