from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping

from pydantic import Field

from pal.execution.tool_facade import StrictToolModel
from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.v2.submission_drafts import (
    SubmissionDraftContext,
    SubmissionDraftStore,
    assert_authoring_schema_budget,
)
from pal.shared import RuntimeStatus


ADD_FINDING_CAPABILITY = "op_minion_add_finding"
FINDING_KINDS = frozenset(
    {
        "requirements_defect",
        "module_defect",
        "dependency_defect",
        "contract_defect",
        "architecture_defect",
        "integration_defect",
        "verification_defect",
    }
)
FINDING_PRIORITIES = ("p0", "p1", "p2")
FINDING_DISPOSITIONS = ("blocking", "advisory")
PRIORITY_TO_SEVERITY = {"p0": "blocker", "p1": "major", "p2": "minor"}


class MinionV2ReviewFindingLocation(StrictToolModel):
    """Minion-owned citation contract so plugin reloads do not depend on core model caches."""

    scope: Literal["task_ledger", "workspace"]
    file: str = Field(min_length=1)
    line: int = Field(ge=1)
    symbol: str | None = Field(default=None, min_length=1)


class MinionV2ReviewAddFindingInput(StrictToolModel):
    """Structured finding authored by reviewer and verifier roles."""

    finding_key: str = Field(
        min_length=3,
        max_length=96,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    finding_kind: Literal[
        "requirements_defect",
        "module_defect",
        "dependency_defect",
        "contract_defect",
        "architecture_defect",
        "integration_defect",
        "verification_defect",
    ]
    priority: Literal["p0", "p1", "p2"]
    disposition: Literal["blocking", "advisory"] = "blocking"
    summary: str = Field(min_length=1, max_length=4000)
    locations: list[MinionV2ReviewFindingLocation] | None = Field(
        default=None,
        max_length=8,
    )


ADD_FINDING_EXAMPLES = (
    {
        "finding_key": "decode_example_header_mismatch",
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
        "finding_key": "failed_decoder_accepts_later_input",
        "finding_kind": "module_defect",
        "priority": "p1",
        "summary": (
            "Decoder consumes later input after entering the failed state, violating "
            "the failed-until-reset contract."
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
    {
        "finding_key": "decoder_state_could_be_static",
        "finding_kind": "module_defect",
        "priority": "p2",
        "disposition": "advisory",
        "summary": (
            "The target language can express the decoder's legal states as a closed "
            "type-level contract instead of relying only on a runtime convention."
        ),
        "locations": [
            {
                "scope": "workspace",
                "file": "include/framepipe/decoder.hpp",
                "line": 24,
                "symbol": "framepipe::Decoder",
            }
        ],
    },
)

ADD_FINDING_TOOL_SPEC: dict[str, Any] = {
    "alias": "add_finding",
    "description": (
        "Record or replace one actionable review finding in the current fenced Draft. "
        "finding_key is a readable stable snake_case identity; reusing it corrects that finding. "
        "priority is p0, p1, or p2. disposition is blocking or advisory; blocking findings prevent "
        "PASS, while advisory is reserved for a worthwhile non-blocking p2 improvement. Add all "
        "independent findings in one parallel tool batch when possible, then use the role's "
        "no-argument terminal tool."
    ),
    "InputModel": MinionV2ReviewAddFindingInput,
    "examples": ADD_FINDING_EXAMPLES,
    "idempotency": "keyed_idempotent",
    "retry_policy": "reconcile_first",
}

assert_authoring_schema_budget(
    MinionV2ReviewAddFindingInput.model_json_schema(
        mode="validation",
        union_format="primitive_type_array",
    ),
    owner=ADD_FINDING_CAPABILITY,
)
for _example in ADD_FINDING_EXAMPLES:
    MinionV2ReviewAddFindingInput.model_validate(_example, strict=True)


def is_review_finding_capability(name: str) -> bool:
    return str(name or "") == ADD_FINDING_CAPABILITY


def review_finding_draft_kind(workspace: Mapping[str, Any]) -> str:
    metadata = dict(workspace.get("minion_v2") or {})
    role = str(metadata.get("role") or "")
    mode = str(metadata.get("mode") or "")
    if role == "reviewer" and mode == "architecture":
        return "architecture_review"
    if role == "reviewer":
        return "standalone_review"
    return "verification"


def add_finding_tool_result(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
) -> CanonicalToolResult:
    try:
        args = dict(call.args or {})
        finding = normalize_finding(args)
        metadata = dict(workspace.get("minion_v2") or {})
        role = str(metadata.get("role") or "")
        mode = str(metadata.get("mode") or "")
        if role == "reviewer" and mode == "architecture" and finding["finding_kind"] not in {
            "requirements_defect",
            "contract_defect",
            "architecture_defect",
        }:
            raise ValueError(
                "architecture reviewer finding_kind must be requirements_defect, "
                "contract_defect, or architecture_defect"
            )
        draft_kind = review_finding_draft_kind(workspace)
        context = SubmissionDraftContext.from_workspace(workspace, draft_kind=draft_kind)
        store = SubmissionDraftStore(Path(str(workspace["runtime_root"])))

        def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
            findings = [dict(item) for item in list(payload.get("findings") or [])]
            key = finding["finding_key"]
            replaced = False
            for index, existing in enumerate(findings):
                if str(existing.get("finding_key") or "") == key:
                    findings[index] = finding
                    replaced = True
                    break
            if not replaced:
                findings.append(finding)
            payload["findings"] = findings
            return payload, {
                "recorded": True,
                "replaced": replaced,
                "finding_key": key,
                "finding_count": len(findings),
            }

        request_hash = hashlib.sha256(
            json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        result = store.mutate(
            context,
            operation_key=str(call.call_id or f"add-finding:{finding['finding_key']}:{request_hash}"),
            request=args,
            reducer=reducer,
            seed=empty_review_draft(),
        )
        text = (
            f"Finding {finding['finding_key']} "
            f"{'updated' if result.get('replaced') else 'recorded'}; "
            f"Draft now has {result.get('finding_count', 0)} finding(s)."
        )
        return CanonicalToolResult(
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
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=text,
            llm_text=text + " Correct this finding and retry with the same semantic finding_key.",
            structured={"error": str(exc), "error_type": exc.__class__.__name__},
            call_id=call.call_id,
            status=RuntimeStatus.INVALID,
        )


def normalize_finding(value: Mapping[str, Any]) -> dict[str, Any]:
    validated = MinionV2ReviewAddFindingInput.model_validate(value, strict=True)
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


def partition_findings(
    findings: list[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split normalized findings into repair-blocking defects and optional advisories."""

    blocking: list[dict[str, Any]] = []
    advisories: list[dict[str, Any]] = []
    for raw in findings:
        item = normalize_finding(raw)
        if item["disposition"] == "advisory":
            advisories.append(item)
        else:
            blocking.append(item)
    return blocking, advisories


def structured_advisories(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate the compiled non-blocking advisory projection."""

    raw = list(payload.get("advisories") or [])
    blocking, advisories = partition_findings(
        structured_findings({"findings": raw})
    )
    if blocking:
        raise ValueError("compiled advisories must use disposition=advisory")
    return advisories


def structured_findings(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in list(payload.get("findings") or []):
        finding = normalize_finding(dict(raw or {}))
        key = finding["finding_key"]
        if key in seen:
            raise ValueError(f"duplicate finding_key in review artifact: {key}")
        seen.add(key)
        values.append(finding)
    return sorted(
        values,
        key=lambda item: (
            FINDING_PRIORITIES.index(str(item["priority"])),
            str(item["finding_key"]),
        ),
    )


def empty_review_draft() -> dict[str, Any]:
    return {
        "definitions": {},
        "evidence": {"cases": {}},
        "findings": [],
        "summary": {},
    }


def finding_severity(finding: Mapping[str, Any]) -> str:
    return PRIORITY_TO_SEVERITY[str(finding.get("priority") or "p1")]
