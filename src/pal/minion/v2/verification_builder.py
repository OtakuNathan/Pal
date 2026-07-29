from __future__ import annotations

from pal.execution.generated_tool_models import (
    MinionV2VerificationBuilderOpMinionReviewConclusionInput,
    MinionV2VerificationBuilderOpMinionReviewSurfaceInput,
    MinionV2VerificationBuilderOpMinionStandaloneReviewSubmitInput,
    MinionV2VerificationBuilderOpMinionVerificationCheckUnavailableInput,
    MinionV2VerificationBuilderOpMinionVerificationDraftStatusInput,
    MinionV2VerificationBuilderOpMinionVerificationRemoveCaseInput,
    MinionV2VerificationBuilderOpMinionVerificationRemoveFindingInput,
    MinionV2VerificationBuilderOpMinionVerificationRunLspCheckInput,
    MinionV2VerificationBuilderOpMinionVerificationScratchWriteInput,
    MinionV2VerificationBuilderOpMinionVerificationSetSummaryInput,
    MinionV2VerificationBuilderOpMinionVerificationSubmitInput,
    MinionV2VerificationBuilderVERIFICATIONBUILDERTOOLSPECSInput,
)

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.v2.artifacts import ContentAddressedArtifactStore
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.review_findings import (
    ADD_FINDING_CAPABILITY,
    finding_severity,
    partition_findings,
    structured_findings,
)
from pal.minion.v2.semantic_evidence import (
    record_unavailable_evidence,
    recorded_cases,
    run_lsp_evidence,
    run_shell_evidence,
    scratch_fingerprint,
)
from pal.minion.v2.submission_drafts import (
    SubmissionDraftContext,
    SubmissionDraftStore,
    assert_authoring_schema_budget,
)
from pal.minion.v2.submission_preflight import (
    bound_reference_payload,
    raise_submission_errors,
)
from pal.minion.v2.verification import (
    historical_repair_checklist_items,
    validate_verification_case_order,
)
from pal.minion.workspace_tools import _append_unique_artifact, _write_minion_artifact
from pal.shared import RuntimeStatus


_RUN_TO_KIND_TAG = {
    "op_minion_verification_run_historical_regression": ("historical_regression", "historical_regressions"),
    "op_minion_verification_run_diff_risk": ("diff_risk", "candidate_delta_review"),
    "op_minion_verification_run_adversarial_case": ("contract_adversarial", "focused_tests"),
    "op_minion_verification_run_focused_test": ("unit", "focused_tests"),
    "op_minion_verification_run_compile_check": ("compile", "compile"),
    "op_minion_verification_run_warning_check": ("compile", "warning_clean"),
    "op_minion_verification_run_consumer_probe": ("consumer_probe", "consumer_probe"),
    "op_minion_verification_run_dogfood": ("consumer_probe", "public_surface_dogfood"),
    "op_minion_verification_run_platform_probe": ("platform_assumption", "platform_probe"),
}

_COMMON_VERIFICATION_CAPABILITIES = frozenset(
    {
        "op_minion_verification_scratch_write",
        "op_minion_verification_run_historical_regression",
        "op_minion_verification_run_diff_risk",
        "op_minion_verification_run_adversarial_case",
        "op_minion_verification_run_focused_test",
        "op_minion_verification_run_compile_check",
        "op_minion_verification_run_warning_check",
        "op_minion_verification_run_lsp_check",
        "op_minion_verification_check_unavailable",
        ADD_FINDING_CAPABILITY,
        "op_minion_verification_set_summary",
        "op_minion_verification_draft_status",
        "op_minion_verification_remove_case",
        "op_minion_verification_remove_finding",
        "op_minion_verification_submit",
    }
)

_EXECUTION_CAPABILITIES = (
    "op_minion_verification_scratch_write",
    *_RUN_TO_KIND_TAG,
    "op_minion_verification_run_lsp_check",
    "op_minion_verification_check_unavailable",
)
_FINDING_CAPABILITIES = (
    ADD_FINDING_CAPABILITY,
    "op_minion_verification_set_summary",
)
VERIFICATION_BUILDER_CAPABILITIES = (
    *_EXECUTION_CAPABILITIES,
    *_FINDING_CAPABILITIES,
    "op_minion_verification_draft_status",
    "op_minion_verification_remove_case",
    "op_minion_verification_remove_finding",
    "op_minion_verification_submit",
)
VERIFICATION_EVIDENCE_CAPABILITIES = (
    *(
        capability
        for capability in _EXECUTION_CAPABILITIES
        if capability != "op_minion_verification_scratch_write"
    ),
    "op_minion_verification_draft_status",
    "op_minion_verification_remove_case",
)
STANDALONE_REVIEW_BUILDER_CAPABILITIES = (
    *(
        capability
        for capability in _EXECUTION_CAPABILITIES
        if capability != "op_minion_verification_run_diff_risk"
    ),
    *_FINDING_CAPABILITIES,
    "op_minion_verification_draft_status",
    "op_minion_verification_remove_case",
    "op_minion_verification_remove_finding",
    "op_minion_review_surface",
    "op_minion_review_conclusion",
    "op_minion_standalone_review_submit",
)
VERIFICATION_TOOL_CAPABILITIES = tuple(dict.fromkeys((*VERIFICATION_BUILDER_CAPABILITIES, *STANDALONE_REVIEW_BUILDER_CAPABILITIES)))

_DEFECT_PRECEDENCE = {
    "verification_defect": -1,
    "module_defect": 0,
    "integration_defect": 1,
    "dependency_defect": 2,
    "contract_defect": 3,
    "architecture_defect": 4,
    "requirements_defect": 5,
}

VERIFICATION_BUILDER_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_minion_verification_scratch_write": {
        "alias": "verification_scratch_write",
        "description": (
            "Create or replace one complete verifier-owned test/probe file under the bound scratch directory. "
            "The result returns the exact scratch_path to execute. Call this same tool again with the complete "
            "replacement content when revising a probe; do not switch to ordinary file editing tools. "
            "This cannot modify product source."
        ),
        "InputModel": MinionV2VerificationBuilderOpMinionVerificationScratchWriteInput,
    },
    **{
        name: {
            "alias": name.removeprefix("op_minion_"),
            "description": (
                "Run and durably register one semantic verification case. Use a readable case name and put any source citation in the description or path; "
                "the Manager owns stdout/stderr Artifacts and does not ask you to construct a report object."
            ),
            "InputModel": MinionV2VerificationBuilderVERIFICATIONBUILDERTOOLSPECSInput,
        }
        for name in _RUN_TO_KIND_TAG
    },
    "op_minion_verification_run_lsp_check": {
        "alias": "verification_run_lsp_check",
        "description": (
            "Run and durably register LSP diagnostics for one source file using the Manager-prepared context. "
            "Use this instead of invoking a language-server executable or creating compile_commands.json/compile_flags.txt through shell. "
            "If the prepared operation is unavailable, record the LSP obligation UNKNOWN once with verification_check_unavailable; do not repair the LSP environment yourself."
        ),
        "InputModel": MinionV2VerificationBuilderOpMinionVerificationRunLspCheckInput,
    },
    "op_minion_verification_check_unavailable": {
        "alias": "verification_check_unavailable",
        "description": "Record focused_tests, warning_clean, consumer_probe, public_surface_dogfood, lsp, historical_regressions, platform_probe, or candidate_delta_review as UNKNOWN with a concrete environmental reason. This never manufactures PASS evidence.",
        "InputModel": MinionV2VerificationBuilderOpMinionVerificationCheckUnavailableInput,
    },
    "op_minion_verification_set_summary": {
        "alias": "verification_set_summary",
        "description": "Set the concise verifier summary after running cases.",
        "InputModel": MinionV2VerificationBuilderOpMinionVerificationSetSummaryInput,
    },
    "op_minion_verification_draft_status": {
        "alias": "verification_draft_status",
        "description": "Read a compact status of the current verification Draft, including case names, statuses, active findings, and remaining policy obligations.",
        "InputModel": MinionV2VerificationBuilderOpMinionVerificationDraftStatusInput,
    },
    "op_minion_verification_remove_case": {
        "alias": "verification_remove_case",
        "description": "Explicitly withdraw one recorded case by semantic name and give an audit reason. All findings attached to it are withdrawn with it. Do not use this to hide a failing case; rerun the case after a real fix instead.",
        "InputModel": MinionV2VerificationBuilderOpMinionVerificationRemoveCaseInput,
    },
    "op_minion_verification_remove_finding": {
        "alias": "verification_remove_finding",
        "description": "Explicitly withdraw one finding by finding_key with an audit reason. Do not withdraw a finding merely to make submission pass.",
        "InputModel": MinionV2VerificationBuilderOpMinionVerificationRemoveFindingInput,
    },
    "op_minion_verification_submit": {
        "alias": "verification_submit",
        "description": "Submit recorded verification evidence and findings. Takes no arguments; Manager infers verdict and routing from immutable results.",
        "InputModel": MinionV2VerificationBuilderOpMinionVerificationSubmitInput,
    },
    "op_minion_review_surface": {
        "alias": "review_surface",
        "description": "Record one standalone-review surface. kind is reviewed, test_gap, unreviewed, or residual_risk.",
        "InputModel": MinionV2VerificationBuilderOpMinionReviewSurfaceInput,
    },
    "op_minion_review_conclusion": {
        "alias": "review_conclusion",
        "description": "Set the standalone review conclusion. verdict is approved, changes_requested, or blocked.",
        "InputModel": MinionV2VerificationBuilderOpMinionReviewConclusionInput,
    },
    "op_minion_standalone_review_submit": {
        "alias": "review_submit",
        "description": "Submit the standalone review. Takes no arguments; Manager compiles recorded cases, findings, surfaces, and conclusion.",
        "InputModel": MinionV2VerificationBuilderOpMinionStandaloneReviewSubmitInput,
    },
}


def effective_verification_policy(
    *,
    work_view: Mapping[str, Any],
    verification_policy: Mapping[str, Any],
    standalone: bool = False,
) -> dict[str, Any]:
    """Compile family defaults into obligations owned by this exact review node."""

    source = dict(verification_policy or {})
    system = str(work_view.get("kind") or "") == "system_and_delivery"
    mode = "standalone" if standalone else "system" if system else "module"
    entrypoints = [dict(item) for item in list(work_view.get("entrypoints") or []) if isinstance(item, Mapping)]
    entrypoint_kinds = {str(item.get("kind") or "").strip() for item in entrypoints}
    require_consumer_probe = False
    require_dogfood = system
    require_platform_probe = system and "platform_probe" in entrypoint_kinds
    if standalone:
        require_dogfood = bool(source.get("require_public_surface_dogfood", False))
        require_platform_probe = False

    historical_regressions = historical_repair_checklist_items(work_view)

    allowed_obligations = {
        "compile",
        "focused_tests",
        "lsp",
        "warning_clean",
    }
    if not standalone:
        allowed_obligations.add("candidate_delta_review")
    if historical_regressions:
        allowed_obligations.add("historical_regressions")
    if mode == "module" or require_consumer_probe:
        allowed_obligations.add("consumer_probe")
    if require_dogfood:
        allowed_obligations.add("public_surface_dogfood")
    if require_platform_probe:
        allowed_obligations.add("platform_probe")
    if standalone:
        allowed_obligations.update(
            {"consumer_probe", "public_surface_dogfood", "platform_probe"}
        )

    return {
        "mode": mode,
        "require_focused_tests": bool(source.get("require_focused_tests", False)),
        "require_warning_clean": bool(source.get("require_warning_clean", False)),
        "require_consumer_probe": require_consumer_probe,
        "require_public_surface_dogfood": require_dogfood,
        "require_platform_probe": require_platform_probe,
        "require_candidate_delta_review": not standalone,
        # Historical regression is node-local. An empty RepairBill ledger has
        # no case to replay and must not become an UNKNOWN obligation.
        "require_historical_regressions": bool(historical_regressions),
        "lsp_policy": str(source.get("lsp_policy") or ""),
        "unknown_policy": str(source.get("unknown_policy") or "strict"),
        "case_timeout_seconds": int(source.get("case_timeout_seconds") or 300),
        "allowed_obligations": sorted(allowed_obligations),
    }


def compile_verification_invocation_tool_contract(
    *,
    work_view: Mapping[str, Any],
    verification_policy: Mapping[str, Any],
    standalone: bool = False,
) -> dict[str, Any]:
    """Compile one stable, invocation-local description contract from bound inputs."""

    module_name = str(
        work_view.get("module_name")
        or work_view.get("verification_name")
        or ("standalone_review" if standalone else "")
    ).strip()
    accepted_modules = sorted(
        {
            str(dict(item or {}).get("module_name") or "").strip()
            for item in list(work_view.get("accepted_modules") or [])
            if str(dict(item or {}).get("module_name") or "").strip()
        }
    )
    dependencies = sorted(
        {
            str(item).strip()
            for item in list(
                work_view.get("construction_dependencies")
                or work_view.get("depends_on")
                or []
            )
            if str(item).strip()
        }
    )
    consumption = sorted(
        (
            {
                key: str(dict(item or {}).get(key) or "").strip()
                for key in ("module", "path", "symbol")
                if str(dict(item or {}).get(key) or "").strip()
            }
            for item in list(work_view.get("contract_consumption") or [])
            if isinstance(item, Mapping)
        ),
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
    )
    consumed_modules = sorted(
        {str(item.get("module") or "") for item in consumption if item.get("module")}
    )
    contract_paths = sorted(
        {
            str(item).strip()
            for item in list(work_view.get("contract_paths") or [])
            if str(item).strip()
        }
    )
    entrypoints = [
        dict(item) if isinstance(item, Mapping) else {"target": str(item).strip()}
        for item in list(work_view.get("entrypoints") or [])
        if isinstance(item, Mapping) or str(item).strip()
    ]
    implementation_targets = accepted_modules or ([module_name] if module_name else [])
    all_targets = sorted(set(implementation_targets + dependencies + consumed_modules))
    policy = effective_verification_policy(
        work_view=work_view,
        verification_policy=verification_policy,
        standalone=standalone,
    )
    historical_regressions = historical_repair_checklist_items(work_view)
    allowed_capabilities = set(
        STANDALONE_REVIEW_BUILDER_CAPABILITIES
        if standalone
        else _COMMON_VERIFICATION_CAPABILITIES
    )
    if standalone:
        allowed_capabilities.discard("op_minion_verification_run_diff_risk")
    if "consumer_probe" in set(policy["allowed_obligations"]):
        allowed_capabilities.add("op_minion_verification_run_consumer_probe")
    if "public_surface_dogfood" in set(policy["allowed_obligations"]):
        allowed_capabilities.add("op_minion_verification_run_dogfood")
    if "platform_probe" in set(policy["allowed_obligations"]):
        allowed_capabilities.add("op_minion_verification_run_platform_probe")
    if not historical_regressions:
        allowed_capabilities.discard("op_minion_verification_run_historical_regression")
    contract: dict[str, Any] = {
        "contract_version": "1",
        "module_name": module_name,
        "contract_paths": contract_paths,
        "contract_consumption": consumption,
        "entrypoints": entrypoints,
        "verification_policy": policy,
        "allowed_capabilities": sorted(allowed_capabilities),
        "allowed_obligations": list(policy["allowed_obligations"]),
        "required_historical_regressions": historical_regressions,
    }
    boundary_text = json.dumps(
        {
            "contract_paths": contract_paths,
            "contract_consumption": consumption,
            "entrypoints": entrypoints,
            "policy": policy,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    overrides: dict[str, dict[str, str]] = {}
    if policy["mode"] in {"system", "standalone"}:
        overrides["op_minion_verification_scratch_write"] = {
            "use_when": (
                "Create or replace a complete executable verifier probe in the bound durable review scratch. "
                "Use the returned scratch_path directly in a dedicated verification run command and set that run "
                "tool's probe_path to the same relative path. To correct the probe, call verification_scratch_write "
                "again with the same relative path and complete replacement content; do not use read_file, "
                "edit_file, or write_file on system-verification scratch."
            ),
            "do_not_use_when": (
                "Do not use this for product source, module developer/verification corpora, or any path outside "
                "the bound system-verification or standalone-review scratch."
            ),
        }
    for capability in (*_RUN_TO_KIND_TAG, "op_minion_verification_run_lsp_check", "op_minion_verification_check_unavailable"):
        overrides[capability] = {"use_when": (
            "Record one semantic verification case. Consult the immutable task.yaml ledger when accepted contracts are insufficient; do not copy it into structured fields. "
            "Reusing a case name updates that case. The description may cite a source filename in any language. Set probe_path whenever the command "
            "consumes a verifier scratch file so exact evidence reuse is invalidated only by that file. "
            f"Bound verification context: {boundary_text}."
        )}
    if not standalone:
        overrides["op_minion_verification_run_diff_risk"] = {
            "use_when": (
                "After replaying every bound historical/current RepairBill regression, inspect the current "
                "Candidate diff and its semantic neighborhood, then run one targeted check for newly introduced "
                "defects. Cite the changed path, symbol or contract section and relevant invariants. This evidence "
                "is Candidate-specific and must be rerun for every assignment; a prior Candidate result cannot "
                "settle the current one."
            ),
            "do_not_use_when": (
                "Do not use before every required historical RepairBill case has been recorded for this assignment, "
                "and do not use it as a substitute for focused corpus tests or historical regressions."
            ),
        }
    overrides["op_minion_verification_check_unavailable"] = {"use_when": (
        "Record UNKNOWN only for an allowed obligation that is genuinely required but unavailable in this environment. "
        "Do not create an UNKNOWN case for an absent or non-applicable obligation. In particular, an empty historical "
        "RepairBill checklist means there is no historical-regression obligation. Allowed obligations: "
        + json.dumps(policy["allowed_obligations"], ensure_ascii=False, sort_keys=True)
        + f". Bound verification context: {boundary_text}."
    )}
    if historical_regressions:
        overrides["op_minion_verification_run_historical_regression"] = {"use_when": (
            "Replay one Manager-bound historical RepairBill case before new adversarial or diff-risk exploration. "
            "Use an exact case name from the checklist and a command that executes its preserved reproducer or committed project regression. "
            "Every listed case must be recorded before new risk exploration and before verification_submit. A repeated "
            "FAIL blocks PASS but must not skip the current Candidate diff-risk audit; finish that audit, batch all "
            "findings, and then submit one outcome. Required historical regressions: "
            + json.dumps(historical_regressions, ensure_ascii=False, sort_keys=True)
        )}
    overrides[ADD_FINDING_CAPABILITY] = {"use_when": (
        "Record or replace one independently actionable, evidence-backed finding. Use a stable snake_case finding_key, "
        "p0/p1/p2 priority, a self-contained summary, and exact task_ledger or workspace locations when available. "
        "Complete the breadth-first audit first and batch independent add_finding calls in one tool round when possible. "
        "A performance finding is valid when a representative scaling probe or source-level complexity trace shows a "
        "material asymptotic, latency, memory, resource, copying, scanning, serialization, blocking, or I/O-amplification "
        "problem; do not record speculative micro-optimizations. Include the triggering workload, measured or derived "
        "impact, exact hot path, and a bounded contract-preserving optimization direction in the summary. "
        "Use verification_defect only for an incorrect Verifier-owned probe or corpus, module_defect for the current implementation, dependency_defect for upstream code, contract_defect for a "
        "frozen boundary, architecture_defect for ownership/topology, requirements_defect for a conflicting task ledger, "
        "and integration_defect for cross-module product behavior. Bound semantic modules: "
        f"{json.dumps(all_targets, ensure_ascii=False)}."
    )}
    contract["guidance_overrides"] = overrides
    contract["fingerprint"] = hashlib.sha256(
        json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return contract


def dominant_verification_defect_kind(findings: list[Mapping[str, Any]]) -> str:
    kinds = {
        str(item.get("finding_kind") or item.get("defect_kind") or "").strip()
        for item in findings
        if str(item.get("finding_kind") or item.get("defect_kind") or "").strip()
    }
    return max(kinds, key=lambda item: _DEFECT_PRECEDENCE.get(item, -1)) if kinds else ""

for _tool_name, _tool_spec in VERIFICATION_BUILDER_TOOL_SPECS.items():
    assert_authoring_schema_budget(
        _tool_spec["InputModel"].model_json_schema(mode="validation", union_format="primitive_type_array"),
        owner=_tool_name,
    )


def is_verification_builder_capability(name: str) -> bool:
    return str(name or "") in VERIFICATION_BUILDER_TOOL_SPECS


async def verification_builder_tool_result(
    call: CanonicalToolCall,
    workspace: dict[str, Any],
    produced_artifacts: list[dict[str, Any]],
    *,
    original_adapter: Any | None = None,
    turn_id: str | None = None,
) -> CanonicalToolResult:
    name = str(call.name or "")
    draft_kind = _draft_kind(workspace)
    try:
        _assert_tool_contract_allows(workspace, name=name, args=dict(call.args or {}))
    except Exception as exc:
        return _error(call, exc)
    if name in _RUN_TO_KIND_TAG:
        case_kind, obligation = _RUN_TO_KIND_TAG[name]
        try:
            _preflight_verification_case_execution(
                workspace,
                draft_kind=draft_kind,
                requested_case_kind=case_kind,
            )
        except Exception as exc:
            return _error(call, exc)
        return await run_shell_evidence(
            call,
            workspace=workspace,
            original_adapter=_require_adapter(original_adapter),
            draft_kind=draft_kind,
            case_kind=case_kind,
            obligation_tag=obligation,
            turn_id=turn_id,
        )
    if name == "op_minion_verification_run_lsp_check":
        return await run_lsp_evidence(
            call,
            workspace=workspace,
            original_adapter=_require_adapter(original_adapter),
            draft_kind=draft_kind,
            turn_id=turn_id,
        )
    if name == "op_minion_verification_check_unavailable":
        return record_unavailable_evidence(call, workspace=workspace, draft_kind=draft_kind)
    try:
        if name == "op_minion_verification_scratch_write":
            return _scratch_write(call, workspace, draft_kind=draft_kind)
        if name == "op_minion_verification_set_summary":
            return _set_summary(call, workspace, draft_kind=draft_kind)
        if name == "op_minion_verification_draft_status":
            return _draft_status(call, workspace, draft_kind=draft_kind)
        if name == "op_minion_verification_remove_case":
            return _remove_case(call, workspace, draft_kind=draft_kind)
        if name == "op_minion_verification_remove_finding":
            return _remove_finding(call, workspace, draft_kind=draft_kind)
        if name == "op_minion_review_surface":
            return _record_review_surface(call, workspace)
        if name == "op_minion_review_conclusion":
            return _set_review_conclusion(call, workspace)
        if name == "op_minion_verification_submit":
            return _submit(call, workspace, produced_artifacts, standalone=False)
        if name == "op_minion_standalone_review_submit":
            return _submit(call, workspace, produced_artifacts, standalone=True)
        raise ValueError(f"unknown verification authoring capability: {name}")
    except Exception as exc:
        return _error(call, exc)


def _scratch_write(call: CanonicalToolCall, workspace: Mapping[str, Any], *, draft_kind: str) -> CanonicalToolResult:
    args = dict(call.args or {})
    relative = PurePosixPath(str(args.get("path") or ""))
    if not str(relative) or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("scratch path must be a safe relative path")
    context, store = _store_context(workspace, draft_kind=draft_kind)
    store.read(context, seed=_empty_payload())
    root = Path(str(workspace.get("review_scratch_dir") or ""))
    if not root:
        raise ValueError("review scratch directory is not bound")
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(args.get("content") or ""), encoding="utf-8")

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        definitions = dict(payload.get("definitions") or {})
        files = list(definitions.get("scratch_files") or [])
        if str(relative) not in files:
            files.append(str(relative))
        definitions["scratch_files"] = files
        payload["definitions"] = definitions
        return payload, {"written": str(relative), "scratch_fingerprint": scratch_fingerprint(workspace)}

    result = store.mutate(
        context,
        operation_key=str(call.call_id or f"scratch:{relative}"),
        request=args,
        reducer=reducer,
        seed=_empty_payload(),
    )
    scratch_path = str(target.resolve())
    return _ok(
        call,
        f"scratch file created or replaced: {scratch_path}",
        {
            **result,
            "path": str(relative),
            "scratch_path": scratch_path,
        },
    )


def _draft_status(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
    *,
    draft_kind: str,
) -> CanonicalToolResult:
    if dict(call.args or {}):
        raise ValueError(f"{call.name} takes no arguments")
    context, store = _store_context(workspace, draft_kind=draft_kind)
    snapshot = store.read(context, seed=_empty_payload())
    cases = recorded_cases(snapshot.payload)
    findings, advisories = partition_findings(structured_findings(snapshot.payload))
    tags = {
        str(tag)
        for item in cases
        for tag in list(item.get("obligation_tags") or [])
    }
    policy = bound_reference_payload(workspace, "verification_policy", required=False)
    required_tags = {
        tag
        for key, tag in (
            ("require_focused_tests", "focused_tests"),
            ("require_warning_clean", "warning_clean"),
            ("require_consumer_probe", "consumer_probe"),
            ("require_public_surface_dogfood", "public_surface_dogfood"),
            ("require_platform_probe", "platform_probe"),
            ("require_historical_regressions", "historical_regressions"),
            ("require_candidate_delta_review", "candidate_delta_review"),
        )
        if bool(policy.get(key, False))
    }
    if str(policy.get("lsp_policy") or "") == "when_available":
        required_tags.add("lsp")
    result = {
        "draft_version": snapshot.version,
        "status": snapshot.status,
        "cases": [
            {"name": str(item.get("name") or ""), "status": str(item.get("status") or "")}
            for item in cases
        ],
        "findings": [
            {
                "finding_key": str(item.get("finding_key") or ""),
                "finding_kind": str(item.get("finding_kind") or ""),
                "priority": str(item.get("priority") or ""),
                "summary": str(item.get("summary") or ""),
            }
            for item in findings
        ],
        "advisories": [
            {
                "finding_key": str(item.get("finding_key") or ""),
                "finding_kind": str(item.get("finding_kind") or ""),
                "priority": str(item.get("priority") or ""),
                "summary": str(item.get("summary") or ""),
            }
            for item in advisories
        ],
        "remaining_policy_obligations": sorted(required_tags - tags),
    }
    return _ok(call, "verification Draft status", result)


def _remove_case(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
    *,
    draft_kind: str,
) -> CanonicalToolResult:
    args = dict(call.args or {})
    name = str(args.get("name") or "").strip()
    reason = str(args.get("reason") or "").strip()
    if not reason:
        raise ValueError("removing a verification case requires an audit reason")
    context, store = _store_context(workspace, draft_kind=draft_kind)

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        evidence = dict(payload.get("evidence") or {})
        cases = dict(evidence.get("cases") or {})
        removed = cases.pop(name, None) is not None
        evidence["cases"] = cases
        payload["evidence"] = evidence
        return payload, {"removed": removed, "case": name, "reason": reason}

    result = store.mutate(
        context,
        operation_key=str(call.call_id or f"remove-case:{name}"),
        request=args,
        reducer=reducer,
        seed=_empty_payload(),
    )
    return _ok(call, f"verification case removed: {name}", result)


def _remove_finding(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
    *,
    draft_kind: str,
) -> CanonicalToolResult:
    args = dict(call.args or {})
    finding_key = str(args.get("finding_key") or "").strip()
    reason = str(args.get("reason") or "").strip()
    if not finding_key:
        raise ValueError("removing a finding requires finding_key")
    if not reason:
        raise ValueError("removing a verification finding requires an audit reason")
    context, store = _store_context(workspace, draft_kind=draft_kind)

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        findings = [dict(item) for item in list(payload.get("findings") or [])]
        matches = [item for item in findings if str(item.get("finding_key") or "") == finding_key]
        if not matches:
            candidates = [str(item.get("finding_key") or "") for item in findings]
            raise ValueError(
                "no finding matches finding_key; candidate keys: "
                + json.dumps(candidates, ensure_ascii=False)
            )
        retained = [item for item in findings if item is not matches[0]]
        payload["findings"] = retained
        return payload, {
            "removed": True,
            "finding_key": finding_key,
            "reason": reason,
        }

    result = store.mutate(
        context,
        operation_key=str(call.call_id or f"remove-finding:{finding_key}"),
        request=args,
        reducer=reducer,
        seed=_empty_payload(),
    )
    return _ok(call, f"verification finding removed: {finding_key}", result)


def _set_summary(call: CanonicalToolCall, workspace: Mapping[str, Any], *, draft_kind: str) -> CanonicalToolResult:
    args = dict(call.args or {})
    summary_text = str(args.get("summary") or "").strip()
    if not summary_text:
        raise ValueError("summary is required")
    context, store = _store_context(workspace, draft_kind=draft_kind)

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        summary = dict(payload.get("summary") or {})
        summary["reviewer_summary"] = summary_text
        payload["summary"] = summary
        return payload, {"recorded": True}

    result = store.mutate(context, operation_key=str(call.call_id or "summary"), request=args, reducer=reducer, seed=_empty_payload())
    return _ok(call, "verification summary recorded", result)


def _record_review_surface(call: CanonicalToolCall, workspace: Mapping[str, Any]) -> CanonicalToolResult:
    args = dict(call.args or {})
    kind = str(args.get("kind") or "")
    text = str(args.get("text") or "").strip()
    context, store = _store_context(workspace, draft_kind="standalone_review")

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        summary = dict(payload.get("summary") or {})
        values = list(summary.get(kind) or [])
        if text not in values:
            values.append(text)
        summary[kind] = values
        payload["summary"] = summary
        return payload, {"recorded": True, "kind": kind}

    result = store.mutate(context, operation_key=str(call.call_id or f"surface:{kind}:{text}"), request=args, reducer=reducer, seed=_empty_payload())
    return _ok(call, "review surface recorded", result)


def _set_review_conclusion(call: CanonicalToolCall, workspace: Mapping[str, Any]) -> CanonicalToolResult:
    args = dict(call.args or {})
    context, store = _store_context(workspace, draft_kind="standalone_review")

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        summary = dict(payload.get("summary") or {})
        summary["conclusion"] = {
            "verdict": str(args.get("verdict") or ""),
            "summary": str(args.get("summary") or "").strip(),
            "scope": str(args.get("scope") or "").strip(),
        }
        payload["summary"] = summary
        return payload, {"recorded": True}

    result = store.mutate(context, operation_key=str(call.call_id or "review-conclusion"), request=args, reducer=reducer, seed=_empty_payload())
    return _ok(call, "review conclusion recorded", result)


def _submit(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
    produced_artifacts: list[dict[str, Any]],
    *,
    standalone: bool,
) -> CanonicalToolResult:
    if dict(call.args or {}):
        raise ValueError(f"{call.name} takes no arguments")
    draft_kind = "standalone_review" if standalone else "verification"
    context, store = _store_context(workspace, draft_kind=draft_kind)
    snapshot = store.read(context, seed=_empty_payload())
    cases = recorded_cases(snapshot.payload)
    if not cases:
        raise ValueError("submit requires at least one recorded verification case")
    findings, advisories = partition_findings(
        structured_findings(snapshot.payload)
    )
    summary = dict(snapshot.payload.get("summary") or {})
    _validate_case_references(cases, workspace=workspace, standalone=standalone)
    if standalone:
        conclusion = dict(summary.get("conclusion") or {})
        if not conclusion:
            raise ValueError("standalone review requires review_conclusion before submit")
        if str(conclusion.get("verdict") or "") == "approved" and findings:
            raise ValueError("approved standalone review requires an empty finding Draft")
        output = {
            "verdict": str(conclusion.get("verdict") or ""),
            "scope": {"description": str(conclusion.get("scope") or "")},
            "reviewed_surfaces": list(summary.get("reviewed") or []),
            "cases": [_case_declaration(item) for item in cases],
            "findings": [_public_finding(item) for item in findings],
            "advisories": [_public_finding(item) for item in advisories],
            "commands_or_lsp_evidence": [str(item.get("name")) for item in cases],
            "test_gaps": list(summary.get("test_gap") or []),
            "unreviewed_surfaces": list(summary.get("unreviewed") or []),
            "residual_risk": list(summary.get("residual_risk") or []),
            "reviewer_summary": str(conclusion.get("summary") or ""),
            "recorded_results": cases,
            "internal_context": _internal_context(context, workspace),
        }
        filename = "standalone_review.json"
        title = "Standalone review submission"
    else:
        defect_kind = dominant_verification_defect_kind(findings)
        output = {
            "cases": [_case_declaration(item) for item in cases],
            "findings": [_public_finding(item) for item in findings],
            "advisories": [_public_finding(item) for item in advisories],
            "reviewer_summary": _default_summary(cases, findings),
            **(
                {"reviewer_notes": str(summary.get("reviewer_summary") or "")}
                if str(summary.get("reviewer_summary") or "").strip()
                else {}
            ),
            "recorded_results": cases,
            "internal_context": _internal_context(context, workspace),
        }
        if defect_kind:
            output["defect_kind"] = defect_kind
            dominant_findings = [
                item for item in findings if str(item.get("finding_kind") or "") == defect_kind
            ]
            first = dominant_findings[0]
            output["severity"] = finding_severity(first)
        output["policy_exceptions"] = _policy_exceptions(cases)
        filename = "verification_plan.json"
        title = "V2 semantic verification plan"
    validate_semantic_verification_plan_shape(output, standalone=standalone, require_complete=True)
    if not standalone:
        reference_warnings = _preflight_verification_submission(output, workspace)
        if reference_warnings:
            output["reference_warnings"] = list(reference_warnings)
    submission_ref: dict[str, Any] = {}
    if store.uses_role_gateway:
        receipt = store.mark_submitted(
            context,
            expected_version=snapshot.version,
            submission_payload=output,
        )
        submission_ref = dict(receipt.get("submission_artifact_ref") or {})
    else:
        runtime_root = Path(str(workspace["runtime_root"]))
        submission_store = ContentAddressedArtifactStore(
            runtime_root,
            MinionV2Repository(runtime_root),
        )
        local_submission_ref = submission_store.put_json(
            output,
            artifact_type=(
                "StandaloneReviewRoleSubmissionArtifact"
                if standalone
                else "VerifierRoleSubmissionArtifact"
            ),
            provenance={
                "workflow_id": context.workflow_id,
                "invocation_id": context.invocation_id,
                "role": context.role,
                "draft_key": context.draft_key,
            },
        )
        submission_payload_hash = hashlib.sha256(
            json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        store.mark_submitted(
            context,
            expected_version=snapshot.version,
            submission_artifact_ref=local_submission_ref.to_dict(),
            submission_payload_hash=submission_payload_hash,
            submission_payload=output,
        )
        submission_ref = local_submission_ref.to_dict()
    if not submission_ref:
        raise RuntimeError("Manager accepted verification submission without a durable receipt")
    artifact = _write_minion_artifact(
        dict(workspace),
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
    return _ok(
        call,
        f"{title} submitted. Stop now.",
        {
            "submitted": True,
        },
    )


def validate_semantic_verification_plan_shape(value: Mapping[str, Any], *, standalone: bool, require_complete: bool = True) -> None:
    required = (
        {"verdict", "scope", "reviewed_surfaces", "cases", "findings", "commands_or_lsp_evidence", "test_gaps", "unreviewed_surfaces", "residual_risk", "reviewer_summary", "recorded_results", "internal_context"}
        if standalone
        else {"cases", "findings", "reviewer_summary", "recorded_results", "internal_context"}
    )
    missing = required - set(value) if require_complete else set()
    if missing:
        raise ValueError("compiled review artifact is missing Manager fields: " + ", ".join(sorted(missing)))
    if not isinstance(value.get("cases"), list) or not value.get("cases"):
        raise ValueError("compiled review artifact requires cases")
    if not isinstance(value.get("findings"), list):
        raise ValueError("compiled review findings must be an array")
    if not isinstance(value.get("advisories", []), list):
        raise ValueError("compiled review advisories must be an array")
    _unexpected_blocking, advisories = partition_findings(
        structured_findings({"findings": list(value.get("advisories") or [])})
    )
    if len(advisories) != len(list(value.get("advisories") or [])):
        raise ValueError("compiled review advisories must use disposition=advisory")
    if not isinstance(value.get("recorded_results"), list) or len(value["recorded_results"]) != len(value["cases"]):
        raise ValueError("every case requires one Manager-recorded result")
    if standalone and str(value.get("verdict") or "") not in {"approved", "changes_requested", "blocked"}:
        raise ValueError("standalone verdict is invalid")


def _verification_submission_errors(
    value: Mapping[str, Any], workspace: Mapping[str, Any]
) -> tuple[list[str], tuple[str, ...]]:
    errors: list[str] = []
    reference_warnings: tuple[str, ...] = ()
    work_view = bound_reference_payload(workspace, "module_work_view", required=False)
    if work_view:
        reference_warnings = ()
    historical = list(work_view.get("historical_repair_bills") or []) or list(
        work_view.get("historical_repair_bill_refs") or []
    )
    required_historical = historical_repair_checklist_items(work_view)
    recorded_results = [dict(item) for item in list(value.get("recorded_results") or [])]
    try:
        validate_verification_case_order(
            [str(item.get("case_kind") or "") for item in recorded_results],
            historical_required=bool(historical),
        )
    except ValueError as exc:
        errors.append(str(exc))
    if required_historical:
        historical_status = {
            str(item.get("name") or ""): str(item.get("status") or "")
            for item in recorded_results
            if str(item.get("case_kind") or "") == "historical_regression"
        }
        missing = [
            str(item["case"])
            for item in required_historical
            if str(item["case"]) not in historical_status
        ]
        if missing:
            errors.append(
                "verification must replay every historical RepairBill case before submit: "
                + ", ".join(missing)
            )
    policy = bound_reference_payload(workspace, "verification_policy", required=False)
    if not policy:
        return errors, reference_warnings
    tags = {str(tag) for item in list(value.get("recorded_results") or []) for tag in list(dict(item).get("obligation_tags") or [])}
    exceptions = dict(value.get("policy_exceptions") or {})
    obligations = (
        ("require_focused_tests", "focused_tests"),
        ("require_warning_clean", "warning_clean"),
        ("require_consumer_probe", "consumer_probe"),
        ("require_public_surface_dogfood", "public_surface_dogfood"),
        ("require_platform_probe", "platform_probe"),
        ("require_candidate_delta_review", "candidate_delta_review"),
    )
    for policy_key, tag in obligations:
        if bool(policy.get(policy_key, False)) and tag not in tags and not str(exceptions.get(tag) or "").strip():
            errors.append(f"VerificationPolicy requires {tag} evidence or an explicit UNKNOWN reason")
    if bool(policy.get("require_historical_regressions", False)) and historical and "historical_regressions" not in tags:
        errors.append("VerificationPolicy requires historical RepairBill regression evidence")
    if str(policy.get("lsp_policy") or "") == "when_available" and "lsp" not in tags and not str(exceptions.get("lsp") or "").strip():
        errors.append("VerificationPolicy requires LSP evidence or an explicit UNKNOWN reason")
    allowed_obligations = {
        str(item) for item in list(policy.get("allowed_obligations") or []) if str(item)
    }
    unexpected = tags - allowed_obligations if allowed_obligations else set()
    if unexpected:
        errors.append(
            "verification submission contains obligations outside this node's scope: "
            + ", ".join(sorted(unexpected))
        )
    failed_cases = [
        str(item.get("name") or "")
        for item in list(value.get("recorded_results") or [])
        if str(item.get("status") or "") == "FAIL"
    ]
    if failed_cases and not list(value.get("findings") or []):
        errors.append(
            "FAIL evidence requires at least one add_finding call: "
            + ", ".join(sorted(failed_cases))
        )
    return errors, reference_warnings


def semantic_verification_draft_errors(
    payload: Mapping[str, Any],
    workspace: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return policy errors for the current assignment-local verifier Draft."""

    cases = recorded_cases(payload)
    findings, _advisories = partition_findings(structured_findings(payload))
    errors, _warnings = _verification_submission_errors(
        {
            "recorded_results": cases,
            "findings": findings,
            "policy_exceptions": _policy_exceptions(cases),
        },
        workspace,
    )
    return tuple(dict.fromkeys(errors))


def _preflight_verification_submission(
    value: Mapping[str, Any], workspace: Mapping[str, Any]
) -> tuple[str, ...]:
    errors, reference_warnings = _verification_submission_errors(value, workspace)
    raise_submission_errors(errors, owner="verification_submit")
    return reference_warnings


def _preflight_verification_case_execution(
    workspace: Mapping[str, Any],
    *,
    draft_kind: str,
    requested_case_kind: str,
) -> None:
    if draft_kind != "verification" or requested_case_kind not in {
        "contract_adversarial",
        "diff_risk",
    }:
        return
    work_view = bound_reference_payload(workspace, "module_work_view", required=False)
    historical = list(work_view.get("historical_repair_bills") or []) or list(
        work_view.get("historical_repair_bill_refs") or []
    )
    if not historical:
        return
    context, store = _store_context(workspace, draft_kind=draft_kind)
    cases = recorded_cases(store.read(context, seed=_empty_payload()).payload)
    required_historical = historical_repair_checklist_items(work_view)
    if required_historical:
        historical_status = {
            str(item.get("name") or ""): str(item.get("status") or "")
            for item in cases
            if str(item.get("case_kind") or "") == "historical_regression"
        }
        incomplete = [
            str(item["case"])
            for item in required_historical
            if str(item["case"]) not in historical_status
        ]
        if not incomplete:
            return
        raise ValueError(
            "record every historical RepairBill regression before adversarial or diff-risk cases: "
            + ", ".join(incomplete)
        )
    if any(
        str(item.get("case_kind") or "") == "historical_regression"
        for item in cases
    ):
        return
    raise ValueError(
        "run the historical RepairBill regression before adversarial or diff-risk cases"
    )


def _validate_case_references(cases: list[Mapping[str, Any]], *, workspace: Mapping[str, Any], standalone: bool) -> None:
    del workspace, standalone
    for item in cases:
        if not str(item.get("description") or "").strip() and not list(item.get("locations") or []) and not list(item.get("invariants") or []):
            raise ValueError(f"case {item.get('name')!r} requires a semantic description, source location, or invariant")


def _case_declaration(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": str(item.get("name") or ""),
        "case_kind": str(item.get("case_kind") or ""),
        "command": list(item.get("command") or []),
        "expected_exit_codes": list(item.get("expected_exit_codes") or [0]),
        "requirements": [dict(value) for value in list(item.get("requirements") or [])],
        "locations": [dict(value) for value in list(item.get("locations") or [])],
        "invariants": [str(value) for value in list(item.get("invariants") or [])],
        "description": str(item.get("description") or ""),
    }


def _public_finding(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in dict(item).items()
        if not str(key).startswith("_")
    }


def _internal_context(context: SubmissionDraftContext, workspace: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "draft_key": context.draft_key,
        "invocation_id": context.invocation_id,
        "fencing_token": context.fencing_token,
        "input_fingerprint": context.input_fingerprint,
        "scratch_fingerprint": scratch_fingerprint(workspace),
    }


def _policy_exceptions(cases: list[Mapping[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in cases:
        if str(item.get("status") or "") != "UNKNOWN":
            continue
        for tag in list(item.get("obligation_tags") or []):
            result[str(tag)] = str(item.get("summary") or "UNKNOWN")
    return result


def _default_summary(cases: list[Mapping[str, Any]], findings: list[Mapping[str, Any]]) -> str:
    counts = {status: sum(1 for item in cases if str(item.get("status")) == status) for status in ("PASS", "FAIL", "UNKNOWN")}
    finding_counts: dict[str, int] = {}
    for item in findings:
        kind = str(item.get("finding_kind") or "unclassified")
        finding_counts[kind] = finding_counts.get(kind, 0) + 1
    suffix = ", ".join(f"{kind}={count}" for kind, count in sorted(finding_counts.items()))
    return (
        f"Recorded {len(cases)} cases: {counts['PASS']} PASS, {counts['FAIL']} FAIL, "
        f"{counts['UNKNOWN']} UNKNOWN; {len(findings)} findings"
        + (f" ({suffix})." if suffix else ".")
    )


def _draft_kind(workspace: Mapping[str, Any]) -> str:
    role = str(dict(workspace.get("minion_v2") or {}).get("role") or "")
    return "standalone_review" if role == "reviewer" else "verification"


def _store_context(workspace: Mapping[str, Any], *, draft_kind: str) -> tuple[SubmissionDraftContext, SubmissionDraftStore]:
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind=draft_kind)
    return context, SubmissionDraftStore(Path(str(workspace["runtime_root"])))


def _empty_payload() -> dict[str, Any]:
    return {"definitions": {"scratch_files": []}, "evidence": {"cases": {}}, "findings": [], "summary": {}}


def _assert_tool_contract_allows(
    workspace: Mapping[str, Any],
    *,
    name: str,
    args: Mapping[str, Any],
) -> None:
    binding = dict(workspace.get("minion_v2") or {})
    contract = dict(binding.get("verification_tool_contract") or {})
    if not contract:
        return
    allowed = {str(item) for item in list(contract.get("allowed_capabilities") or [])}
    if allowed and name not in allowed:
        raise ValueError(
            f"verification capability {name!r} is outside the bound node contract"
        )
    obligation = ""
    if name in _RUN_TO_KIND_TAG:
        obligation = _RUN_TO_KIND_TAG[name][1]
    elif name == "op_minion_verification_run_lsp_check":
        obligation = "lsp"
    elif name == "op_minion_verification_check_unavailable":
        obligation = str(args.get("obligation") or "").strip()
    allowed_obligations = {
        str(item) for item in list(contract.get("allowed_obligations") or [])
    }
    if obligation and allowed_obligations and obligation not in allowed_obligations:
        raise ValueError(
            f"verification obligation {obligation!r} is outside the bound node contract; "
            "use only the declared module or system-delivery entrypoints"
        )


def _require_adapter(adapter: Any | None) -> Any:
    if adapter is None:
        raise ValueError("verification run tool requires scoped execution")
    return adapter


def _ok(call: CanonicalToolCall, text: str, structured: Mapping[str, Any]) -> CanonicalToolResult:
    return CanonicalToolResult(name=call.name, ok=True, text=text, llm_text=text, structured=dict(structured), call_id=call.call_id, status=RuntimeStatus.OK)


def _error(call: CanonicalToolCall, exc: Exception) -> CanonicalToolResult:
    text = f"{exc.__class__.__name__}: {exc}"
    return CanonicalToolResult(name=call.name, ok=False, text=text, llm_text=text + " Correct only this local issue and retry.", structured={"error": str(exc), "error_type": exc.__class__.__name__}, call_id=call.call_id, status=RuntimeStatus.INVALID)
