from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from pal.minion.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.minion.v2.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, DispatchResult
from pal.minion.v2.repository import MinionV2Repository


class VerificationStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class DefectKind(StrEnum):
    MODULE = "module_defect"
    DEPENDENCY = "dependency_defect"
    CONTRACT = "contract_defect"
    ARCHITECTURE = "architecture_defect"
    INTEGRATION = "integration_defect"
    REQUIREMENTS = "requirements_defect"


class VerificationCaseKind(StrEnum):
    HISTORICAL_REGRESSION = "historical_regression"
    CONTRACT_ADVERSARIAL = "contract_adversarial"
    DIFF_RISK = "diff_risk"
    COMPILE = "compile"
    LSP = "lsp"
    UNIT = "unit"
    CONSUMER_PROBE = "consumer_probe"
    PLATFORM_ASSUMPTION = "platform_assumption"


@dataclass(frozen=True)
class VerificationCaseSpec:
    case_id: str
    case_kind: VerificationCaseKind
    command: tuple[str, ...]
    expected_exit_codes: tuple[int, ...] = (0,)
    description: str = ""
    case_name: str = ""
    requirements: tuple[Mapping[str, str], ...] = ()
    locations: tuple[Mapping[str, str], ...] = ()
    invariants: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationCaseResult:
    case_id: str
    case_kind: VerificationCaseKind
    status: VerificationStatus
    command: tuple[str, ...]
    exit_code: int | None
    stdout_ref: Mapping[str, Any]
    stderr_ref: Mapping[str, Any]
    environment: Mapping[str, Any]
    summary: str
    case_name: str = ""
    requirements: tuple[Mapping[str, str], ...] = ()
    locations: tuple[Mapping[str, str], ...] = ()
    invariants: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.case_name or self.case_id,
            "case_kind": self.case_kind.value,
            "status": self.status.value,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "stdout_ref": dict(self.stdout_ref),
            "stderr_ref": dict(self.stderr_ref),
            "environment": dict(self.environment),
            "requirements": [dict(item) for item in self.requirements],
            "locations": [dict(item) for item in self.locations],
            "invariants": list(self.invariants),
            "summary": self.summary,
        }


@dataclass(frozen=True)
class UnknownPolicy:
    architecture_allows_platform_unknown: bool
    assumption_ref: Mapping[str, Any] | None
    hard_or_core_semantics: bool
    human_waiver_ref: Mapping[str, Any] | None = None

    def allows(self) -> bool:
        if not self.architecture_allows_platform_unknown or not self.assumption_ref:
            return False
        if self.hard_or_core_semantics and not self.human_waiver_ref:
            return False
        return True


@dataclass
class VerificationCaseRunner:
    artifacts: ContentAddressedArtifactStore

    def run(
        self,
        case: VerificationCaseSpec,
        *,
        cwd: Path,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: float = 120.0,
    ) -> VerificationCaseResult:
        env = dict(os.environ)
        env.update({str(key): str(value) for key, value in dict(environment or {}).items()})
        try:
            completed = subprocess.run(
                list(case.command),
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=max(0.1, timeout_seconds),
                check=False,
            )
            exit_code: int | None = int(completed.returncode)
            status = VerificationStatus.PASS if exit_code in case.expected_exit_codes else VerificationStatus.FAIL
            stdout = completed.stdout
            stderr = completed.stderr
            summary = f"exit {exit_code}; expected {list(case.expected_exit_codes)}"
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            exit_code = None
            status = VerificationStatus.UNKNOWN
            stdout = bytes(getattr(exc, "stdout", b"") or b"")
            stderr = bytes(getattr(exc, "stderr", b"") or str(exc).encode("utf-8"))
            summary = str(exc)
        stdout_ref = self.artifacts.put_bytes(
            stdout,
            artifact_type="VerificationStdoutArtifact",
            media_type="text/plain",
        )
        stderr_ref = self.artifacts.put_bytes(
            stderr,
            artifact_type="VerificationStderrArtifact",
            media_type="text/plain",
        )
        environment_record = {
            "cwd": str(cwd.resolve()),
            "selected_environment": dict(environment or {}),
        }
        return VerificationCaseResult(
            case_id=case.case_id,
            case_kind=case.case_kind,
            status=status,
            command=case.command,
            exit_code=exit_code,
            stdout_ref=stdout_ref.to_dict(),
            stderr_ref=stderr_ref.to_dict(),
            environment=environment_record,
            summary=summary,
            case_name=case.case_name,
            requirements=case.requirements,
            locations=case.locations,
            invariants=case.invariants,
        )


@dataclass
class VerificationService:
    repository: MinionV2Repository
    artifacts: ContentAddressedArtifactStore

    def publish_report(
        self,
        *,
        node: AggregateSnapshot,
        candidate_ref: Mapping[str, Any],
        case_results: Sequence[VerificationCaseResult],
        reviewer_summary: str,
        findings: Sequence[Mapping[str, Any]] = (),
        test_workspace_ref: Mapping[str, Any] | None = None,
    ) -> tuple[ArtifactRef, VerificationStatus]:
        if not case_results:
            raise ValueError("verification report requires at least one test case")
        validate_verification_case_order(
            [item.case_kind for item in case_results],
            historical_required=bool(node.payload.get("historical_repair_bill_refs")),
        )
        status = aggregate_verification_status(item.status for item in case_results)
        payload = {
            "schema_version": "1",
            "workflow_id": node.workflow_id,
            "node_run_id": node.aggregate_id,
            "candidate_ref": dict(candidate_ref),
            "status": status.value,
            "cases": [item.to_dict() for item in case_results],
            "findings": [semantic_finding_payload(item) for item in findings],
            "reviewer_summary": reviewer_summary,
            "test_workspace_ref": dict(test_workspace_ref or {}),
            **(
                {"scenario_fingerprint": str(node.payload.get("scenario_fingerprint") or "")}
                if str(node.payload.get("node_kind") or "") == "verification"
                else {}
            ),
        }
        child_refs: list[tuple[str, str]] = []
        if candidate_ref.get("sha256"):
            child_refs.append((str(candidate_ref["sha256"]), "candidate"))
        if test_workspace_ref and test_workspace_ref.get("sha256"):
            child_refs.append((str(test_workspace_ref["sha256"]), "test_workspace"))
        for item in case_results:
            for relation, ref in (("stdout", item.stdout_ref), ("stderr", item.stderr_ref)):
                if ref.get("sha256"):
                    child_refs.append((str(ref["sha256"]), relation))
        report_ref = self.artifacts.put_json(
            payload,
            artifact_type="VerificationArtifact",
            child_refs=tuple(child_refs),
        )
        return report_ref, status

    def publish_repair_bill(
        self,
        *,
        node: AggregateSnapshot,
        candidate_digest: str,
        verification_ref: ArtifactRef,
        defect_kind: DefectKind,
        severity: str,
        minimal_reproducer_ref: Mapping[str, Any],
        test_artifact_ref: Mapping[str, Any],
        expected: Any,
        actual: Any,
        suggested_repair_boundary: Sequence[str],
        finding_section: str = "implementation",
        finding_summary: str = "",
        failure_reason: str = "",
        case_name: str = "",
        requirements: Sequence[Mapping[str, str]] = (),
        locations: Sequence[Mapping[str, str]] = (),
        invariants: Sequence[str] = (),
        findings: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[ArtifactRef, str]:
        finding_values = [dict(item) for item in findings]
        all_requirements = [
            dict(reference)
            for item in finding_values
            for reference in list(item.get("requirements") or [])
        ] or [dict(item) for item in requirements]
        all_locations = [
            dict(reference)
            for item in finding_values
            for reference in list(item.get("locations") or [])
        ] or [dict(item) for item in locations]
        all_invariants = [
            str(reference)
            for item in finding_values
            for reference in list(item.get("invariants") or [])
        ] or [str(item) for item in invariants]
        semantic_contract_refs = _semantic_reference_keys(
            requirements=all_requirements,
            locations=all_locations,
            invariants=all_invariants,
        )
        semantic_case_names = sorted(
            {
                str(item).strip()
                for item in (
                    case_name,
                    *(
                        str(item.get("case_name") or item.get("case") or "")
                        for item in finding_values
                    ),
                )
                if str(item).strip()
            }
        )
        reproducer_identity = _normalized_hash(
            {"semantic_case_names": semantic_case_names}
        )
        fingerprint = finding_fingerprint(
            defect_kind=defect_kind,
            contract_refs=semantic_contract_refs,
            reproducer_hash=reproducer_identity,
            expected=expected,
            actual=actual,
        )
        payload = {
            "schema_version": "1",
            "workflow_id": node.workflow_id,
            "node_run_id": node.aggregate_id,
            "candidate_digest": candidate_digest,
            "verification_artifact_ref": verification_ref.to_dict(),
            "defect_kind": defect_kind.value,
            "module_name": str(node.payload.get("module_name") or node.payload.get("unit_id") or ""),
            "severity": severity,
            "finding_section": finding_section,
            "finding_summary": finding_summary,
            "failure_reason": failure_reason,
            "case_name": case_name,
            "requirements": [dict(item) for item in requirements],
            "locations": [dict(item) for item in locations],
            "invariants": [str(item) for item in invariants],
            "findings": finding_values,
            "finding_fingerprint": fingerprint,
            "minimal_reproducer_ref": dict(minimal_reproducer_ref),
            "test_artifact_ref": dict(test_artifact_ref),
            "expected": expected,
            "actual": actual,
            "suggested_repair_boundary": list(suggested_repair_boundary),
            "regression_test_obligation": {
                "source_test_artifact_ref": dict(test_artifact_ref),
                "instruction": "Add the relevant reviewer case to the project regression suite before repair acceptance.",
            },
        }
        child_refs = [(verification_ref.sha256, "verification")]
        for relation, ref in (("reproducer", minimal_reproducer_ref), ("test", test_artifact_ref)):
            if ref.get("sha256"):
                child_refs.append((str(ref["sha256"]), relation))
        repair_ref = self.artifacts.put_json(
            payload,
            artifact_type="RepairBillArtifact",
            child_refs=tuple(child_refs),
        )
        return repair_ref, fingerprint

    def submit_verdict(
        self,
        *,
        node: AggregateSnapshot,
        verification_ref: ArtifactRef,
        status: VerificationStatus,
        actor: str,
        unknown_policy: UnknownPolicy | None = None,
        repair_bill_ref: ArtifactRef | None = None,
        finding_fingerprint_value: str = "",
        candidate_tree_hash: str = "",
        defect_kind: DefectKind = DefectKind.MODULE,
        dependency_node_id: str = "",
        module_node_id: str = "",
        dependency_node_ids: Sequence[str] = (),
        module_node_ids: Sequence[str] = (),
        scenario_fingerprint: str = "",
        requirement_patch_ref: ArtifactRef | None = None,
        revised_requirements_ref: ArtifactRef | None = None,
        worker_assignment_id: str = "",
        worker_submission_payload_hash: str = "",
        accepted_candidate_ref: ArtifactRef | None = None,
        accepted_candidate_digest: str = "",
    ) -> DispatchResult:
        if (requirement_patch_ref is None) != (revised_requirements_ref is None):
            raise ValueError(
                "RequirementPatch verdict routing requires both patch and revised Requirements artifacts"
            )
        if requirement_patch_ref is not None and defect_kind not in {
            DefectKind.CONTRACT,
            DefectKind.ARCHITECTURE,
        }:
            raise ValueError("RequirementPatch can only accompany a contract or architecture defect")
        if bool(accepted_candidate_ref) != bool(accepted_candidate_digest):
            raise ValueError(
                "accepted verifier candidate requires both artifact ref and digest"
            )
        if accepted_candidate_ref is not None and status != VerificationStatus.PASS:
            raise ValueError("only a PASS verdict may promote verifier-authored tests")
        common = {
            "verification_artifact_ref": verification_ref.to_dict(),
            **(
                {
                    "candidate_ref": accepted_candidate_ref.to_dict(),
                    "candidate_digest": accepted_candidate_digest,
                }
                if accepted_candidate_ref is not None
                else {}
            ),
        }
        dependency_targets = tuple(
            dict.fromkeys(
                str(item)
                for item in (dependency_node_id, *dependency_node_ids)
                if str(item)
            )
        )
        module_targets = tuple(
            dict.fromkeys(
                str(item)
                for item in (module_node_id, *module_node_ids)
                if str(item)
            )
        )
        scenario = str(node.payload.get("node_kind") or "") == "verification"
        if scenario:
            if not scenario_fingerprint or scenario_fingerprint != str(node.payload.get("scenario_fingerprint") or ""):
                raise ValueError("verification verdict does not match the prepared scenario fingerprint")
            common["scenario_fingerprint"] = scenario_fingerprint
        if status == VerificationStatus.PASS:
            action_type = "VERIFICATION_PASSED" if scenario else "REVIEW_PASSED"
            payload = common
        elif status == VerificationStatus.NOT_APPLICABLE:
            action_type = "VERIFICATION_PASSED" if scenario else "REVIEW_PASSED"
            payload = {**common, "not_applicable": True}
        elif status == VerificationStatus.UNKNOWN:
            policy = unknown_policy or UnknownPolicy(False, None, True)
            if policy.allows():
                action_type = "VERIFICATION_UNKNOWN_ALLOWED" if scenario else "REVIEW_UNKNOWN_ALLOWED"
                payload = {
                    **common,
                    "policy_allows_unknown": True,
                    "assumption_ref": dict(policy.assumption_ref or {}),
                    "hard_or_core_semantics": policy.hard_or_core_semantics,
                    "human_waiver_ref": dict(policy.human_waiver_ref or {}),
                }
            else:
                action_type = "ENTER_TRIAGE"
                payload = {
                    **common,
                    "unknown_blocking": True,
                    "blocker": {"kind": "blocking_unknown"},
                }
        else:
            if repair_bill_ref is None or not finding_fingerprint_value:
                raise ValueError("FAIL requires repair_bill_ref and finding_fingerprint")
            history = list(node.payload.get("failure_history") or [])
            history.append(
                {
                    "finding_fingerprint": finding_fingerprint_value,
                    "candidate_tree_hash": candidate_tree_hash,
                }
            )
            if no_progress_detected(history):
                action_type = "ENTER_TRIAGE"
                payload = {
                    **common,
                    "repair_bill_ref": repair_bill_ref.to_dict(),
                    "finding_fingerprint": finding_fingerprint_value,
                    "candidate_tree_hash": candidate_tree_hash,
                    "failure_history": history,
                    "blocker": {"kind": "no_progress", "rounds": 3},
                }
            elif defect_kind == DefectKind.DEPENDENCY:
                if not dependency_targets:
                    raise ValueError("dependency defect requires dependency_node_id")
                action_type = "DEPENDENCY_DEFECT"
                payload = {
                    **common,
                    "repair_bill_ref": repair_bill_ref.to_dict(),
                    "dependency_node_id": dependency_targets[0],
                    "dependency_node_ids": list(dependency_targets),
                    "finding_fingerprint": finding_fingerprint_value,
                    "failure_history": history,
                }
            elif defect_kind == DefectKind.CONTRACT:
                action_type = "CONTRACT_DEFECT"
                payload = {
                    **common,
                    "repair_bill_ref": repair_bill_ref.to_dict(),
                    "failure_history": history,
                    **(
                        {
                            "requirement_patch_ref": requirement_patch_ref.to_dict(),
                            "revised_requirements_ref": revised_requirements_ref.to_dict(),
                        }
                        if requirement_patch_ref is not None and revised_requirements_ref is not None
                        else {}
                    ),
                }
            elif defect_kind == DefectKind.ARCHITECTURE:
                action_type = "ARCHITECTURE_DEFECT"
                payload = {
                    **common,
                    "repair_bill_ref": repair_bill_ref.to_dict(),
                    "failure_history": history,
                    **(
                        {
                            "requirement_patch_ref": requirement_patch_ref.to_dict(),
                            "revised_requirements_ref": revised_requirements_ref.to_dict(),
                        }
                        if requirement_patch_ref is not None and revised_requirements_ref is not None
                        else {}
                    ),
                }
            elif scenario:
                if not module_targets:
                    raise ValueError("scenario module defect requires module_node_id")
                action_type = "MODULE_DEFECT"
                payload = {
                    **common,
                    "repair_bill_ref": repair_bill_ref.to_dict(),
                    "module_node_id": module_targets[0],
                    "module_node_ids": list(module_targets),
                    "finding_fingerprint": finding_fingerprint_value,
                    "failure_history": history,
                }
            else:
                action_type = "REVIEW_FAILED"
                payload = {
                    **common,
                    "repair_bill_ref": repair_bill_ref.to_dict(),
                    "finding_fingerprint": finding_fingerprint_value,
                    "candidate_tree_hash": candidate_tree_hash,
                    "failure_history": history,
                    "defect_kind": defect_kind.value,
                }
        return self.repository.dispatch(
            ActionEnvelope(
                action_type=action_type,
                workflow_id=node.workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node.aggregate_id,
                actor=actor,
                expected_version=node.version,
                idempotency_key=f"verdict:{node.aggregate_id}:{node.version}:{verification_ref.sha256}",
                payload=payload,
            ),
            worker_assignment_id=worker_assignment_id,
            worker_submission_payload_hash=worker_submission_payload_hash,
        )


@dataclass
class DefectPropagationService:
    repository: MinionV2Repository

    def propagate_dependency_defect(
        self,
        *,
        workflow_id: str,
        epoch_id: str,
        dependency_node_id: str,
        repair_bill_ref: ArtifactRef,
        actor: str = "minion-manager",
    ) -> tuple[str, ...]:
        snapshots = self.repository.list_workflow_snapshots(workflow_id)
        nodes = {
            item.aggregate_id: item
            for item in snapshots
            if item.aggregate_type == AggregateType.DAG_NODE_RUN
            and str(item.payload.get("epoch_id") or "") == epoch_id
        }
        dependency = nodes.get(dependency_node_id)
        if dependency is None:
            raise ValueError("dependency node does not exist in epoch")
        if dependency.state == "ACCEPTED":
            self.repository.dispatch(
                _node_action(
                    dependency,
                    "REOPEN_DEPENDENCY",
                    actor,
                    {"repair_bill_ref": repair_bill_ref.to_dict()},
                )
            )
        affected = _transitive_dependents(dependency_node_id, nodes)
        for node_id in sorted(affected):
            node = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
            if node is None or node.state in {"STALE", "CANCELLED"}:
                continue
            payload = {"stale_reason_ref": repair_bill_ref.to_dict(), "stale_dependency_node_id": dependency_node_id}
            action_type = (
                "MARK_STALE"
                if node.state in {"BLOCKED_BY_DEPS", "QUEUED", "REVIEW_QUEUED", "REPAIR_QUEUED", "ACCEPTED", "CANCELLED"}
                else "REQUEST_STALE"
            )
            self.repository.dispatch(_node_action(node, action_type, actor, payload))
        return tuple(sorted(affected))


def repair_bill_semantic_view(
    artifacts: ContentAddressedArtifactStore,
    repair_bill_ref: ArtifactRef | Mapping[str, Any],
) -> dict[str, Any]:
    """Compile a RepairBill for a worker without exposing manager identities."""

    bill = dict(artifacts.read_json(repair_bill_ref))
    if str(bill.get("artifact_kind") or "") == "semantic_repair_packet":
        return {
            "artifact_kind": "semantic_repair_packet",
            "module_name": str(bill.get("module_name") or ""),
            "route": str(bill.get("route") or "module_repair"),
            "target_modules": [
                str(item) for item in list(bill.get("target_modules") or [])
            ],
            "findings": [dict(item) for item in list(bill.get("findings") or [])],
            "regression_commands": [
                str(item)
                for item in list(bill.get("regression_commands") or [])
                if str(item).strip()
            ],
            "verifier_test_paths": [
                str(item) for item in list(bill.get("changed_test_paths") or [])
            ],
        }
    canonical_findings = [
        semantic_finding_payload(dict(item))
        for item in list(bill.get("findings") or [])
        if isinstance(item, Mapping) and dict(item).get("finding_key")
    ]
    if canonical_findings:
        return {
            "artifact_kind": str(bill.get("artifact_kind") or "structured_repair_bill"),
            "module_name": str(bill.get("module_name") or ""),
            "route": str(bill.get("route") or "module_repair"),
            "findings": canonical_findings,
            "regression_test_obligation": str(
                dict(bill.get("regression_test_obligation") or {}).get("instruction") or ""
            ),
        }
    reproducer: dict[str, Any] = {}
    reproducer_ref = bill.get("minimal_reproducer_ref")
    if isinstance(reproducer_ref, Mapping) and reproducer_ref.get("sha256"):
        try:
            raw = dict(artifacts.read_json(reproducer_ref))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            raw = {}
        reproducer = {
            key: raw[key]
            for key in (
                "name",
                "case_kind",
                "status",
                "command",
                "exit_code",
                "requirements",
                "locations",
                "invariants",
                "summary",
            )
            if key in raw
        }
    test_files: list[dict[str, Any]] = []
    test_ref = bill.get("test_artifact_ref")
    if isinstance(test_ref, Mapping) and test_ref.get("sha256"):
        try:
            test_workspace = dict(artifacts.read_json(test_ref))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            test_workspace = {}
        for item in list(test_workspace.get("files") or []):
            entry = dict(item or {})
            encoded = str(entry.get("content_base64") or "")
            content = ""
            binary = False
            if encoded:
                try:
                    raw = base64.b64decode(encoded, validate=True)
                    content = raw.decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    binary = True
            test_files.append(
                {
                    "path": str(entry.get("path") or ""),
                    **({"content": content} if not binary else {"binary": True}),
                }
            )
    return {
        "module_name": str(bill.get("module_name") or ""),
        "defect_kind": str(bill.get("defect_kind") or ""),
        "severity": str(bill.get("severity") or ""),
        "finding_section": str(bill.get("finding_section") or "implementation"),
        "summary": str(bill.get("finding_summary") or ""),
        "failure_reason": str(bill.get("failure_reason") or ""),
        "case_name": str(bill.get("case_name") or reproducer.get("name") or ""),
        "requirements": [dict(item) for item in list(bill.get("requirements") or [])],
        "locations": [dict(item) for item in list(bill.get("locations") or [])],
        "invariants": [str(item) for item in list(bill.get("invariants") or [])],
        "findings": [
            semantic_finding_payload(dict(item))
            for item in list(bill.get("findings") or [])
            if isinstance(item, Mapping)
        ],
        "reproducer": reproducer,
        "expected": bill.get("expected"),
        "actual": bill.get("actual"),
        "suggested_repair_boundary": [
            str(item) for item in list(bill.get("suggested_repair_boundary") or [])
        ],
        "test_files": test_files,
        "regression_test_obligation": str(
            dict(bill.get("regression_test_obligation") or {}).get("instruction") or ""
        ),
    }


def semantic_finding_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(value)
    if item.get("finding_key"):
        return {
            "finding_key": str(item.get("finding_key") or ""),
            "finding_kind": str(item.get("finding_kind") or ""),
            "priority": str(item.get("priority") or ""),
            "summary": str(item.get("summary") or ""),
            "locations": [dict(entry) for entry in list(item.get("locations") or [])],
        }
    return {
        "case": str(item.get("case_name") or ""),
        "finding_section": str(item.get("finding_section") or "implementation"),
        "summary": str(item.get("summary") or ""),
        "failure_reason": str(item.get("failure_reason") or ""),
        "requirements": [dict(entry) for entry in list(item.get("requirements") or [])],
        "locations": [dict(entry) for entry in list(item.get("locations") or [])],
        "invariants": [str(entry) for entry in list(item.get("invariants") or [])],
        "evidence": list(item.get("evidence") or []),
        "severity": str(item.get("severity") or "major"),
        "suggested_repair_boundary": [
            str(entry) for entry in list(item.get("suggested_repair_boundary") or [])
        ],
        **(
            {"defect_kind": str(item.get("defect_kind") or "")}
            if str(item.get("defect_kind") or "").strip()
            else {}
        ),
        **(
            {"target_module": str(item.get("target_module") or "")}
            if str(item.get("target_module") or "").strip()
            else {}
        ),
        **(
            {"routing_disposition": str(item.get("routing_disposition") or "")}
            if str(item.get("routing_disposition") or "").strip()
            else {}
        ),
    }


def repair_checklist_items(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Compile semantic, model-facing repair work without exposing internal IDs."""

    bill = dict(value or {})
    raw_findings = [
        dict(item)
        for item in list(bill.get("findings") or [])
        if isinstance(item, Mapping)
    ]
    if not raw_findings:
        raw_findings = [bill]
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for finding in raw_findings:
        case_name = str(
            finding.get("finding_key")
            or finding.get("case")
            or finding.get("case_name")
            or bill.get("case_name")
            or ""
        ).strip()
        if not case_name or case_name in seen:
            continue
        seen.add(case_name)
        items.append(
            {
                "case": case_name,
                "summary": str(
                    finding.get("summary")
                    or finding.get("finding_summary")
                    or bill.get("summary")
                    or bill.get("finding_summary")
                    or ""
                ).strip(),
                "failure_reason": str(
                    finding.get("failure_reason")
                    or bill.get("failure_reason")
                    or finding.get("summary")
                    or ""
                ).strip(),
                "severity": str(
                    finding.get("priority") or finding.get("severity") or bill.get("severity") or "p1"
                ).strip(),
                "requirements": [
                    dict(item)
                    for item in list(
                        finding.get("requirements") or bill.get("requirements") or []
                    )
                    if isinstance(item, Mapping)
                ],
                "locations": [
                    dict(item)
                    for item in list(
                        finding.get("locations") or bill.get("locations") or []
                    )
                    if isinstance(item, Mapping)
                ],
                "invariants": [
                    str(item)
                    for item in list(
                        finding.get("invariants") or bill.get("invariants") or []
                    )
                    if str(item).strip()
                ],
                "suggested_repair_boundary": [
                    str(item)
                    for item in list(
                        finding.get("suggested_repair_boundary")
                        or bill.get("suggested_repair_boundary")
                        or []
                    )
                    if str(item).strip()
                ],
            }
        )
    return items


def historical_repair_checklist_items(work_view: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return every named historical regression obligation in stable order."""

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_bill in list(work_view.get("historical_repair_bills") or []):
        if not isinstance(raw_bill, Mapping):
            continue
        for item in repair_checklist_items(raw_bill):
            case_name = str(item.get("case") or "").strip()
            if not case_name or case_name in seen:
                continue
            seen.add(case_name)
            items.append(item)
    return items


def aggregate_verification_status(statuses: Sequence[VerificationStatus] | Any) -> VerificationStatus:
    values = tuple(statuses)
    if any(item == VerificationStatus.FAIL for item in values):
        return VerificationStatus.FAIL
    if any(item == VerificationStatus.UNKNOWN for item in values):
        return VerificationStatus.UNKNOWN
    if values and all(item == VerificationStatus.NOT_APPLICABLE for item in values):
        return VerificationStatus.NOT_APPLICABLE
    return VerificationStatus.PASS


def finding_fingerprint(
    *,
    defect_kind: DefectKind,
    contract_refs: Sequence[str],
    reproducer_hash: str,
    expected: Any,
    actual: Any,
) -> str:
    normalized = {
        "defect_kind": defect_kind.value,
        "contract_refs": sorted(str(item) for item in contract_refs),
        "reproducer_hash": str(reproducer_hash),
        "expected_hash": _normalized_hash(expected),
        "actual_hash": _normalized_hash(actual),
    }
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _semantic_reference_keys(
    *,
    requirements: Sequence[Mapping[str, str]],
    locations: Sequence[Mapping[str, str]],
    invariants: Sequence[str],
) -> list[str]:
    values = [
        *(f"requirement:{str(item.get('section') or '')}:{str(item.get('requirement') or '')}" for item in requirements),
        *(
            "location:"
            + ":".join(
                (
                    str(item.get("path") or ""),
                    str(item.get("symbol") or ""),
                    str(item.get("section") or ""),
                )
            )
            for item in locations
        ),
        *(f"invariant:{str(item)}" for item in invariants),
    ]
    return sorted(value for value in values if value)


def no_progress_detected(history: Sequence[Mapping[str, Any]]) -> bool:
    if len(history) < 3:
        return False
    latest = list(history)[-3:]
    fingerprints = {str(item.get("finding_fingerprint") or "") for item in latest}
    tree_hashes = {str(item.get("candidate_tree_hash") or "") for item in latest}
    return len(fingerprints) == 1 and "" not in fingerprints and len(tree_hashes) == 1 and "" not in tree_hashes


def candidate_reuse_fingerprint(
    *,
    unit_contract_hash: str,
    relevant_requirements_hash: str,
    relevant_evidence_hash: str,
    global_constraint_hash: str,
    owned_area_hash: str,
    dependency_set_hash: str,
    dependency_interface_hash: str,
    dependency_output_hash: str,
    integration_contract_subset_hash: str,
    environment_policy_hash: str,
) -> str:
    payload = {
        "unit_contract_hash": unit_contract_hash,
        "relevant_requirements_hash": relevant_requirements_hash,
        "relevant_evidence_hash": relevant_evidence_hash,
        "global_constraint_hash": global_constraint_hash,
        "owned_area_hash": owned_area_hash,
        "dependency_set_hash": dependency_set_hash,
        "dependency_interface_hash": dependency_interface_hash,
        "dependency_output_hash": dependency_output_hash,
        "integration_contract_subset_hash": integration_contract_subset_hash,
        "environment_policy_hash": environment_policy_hash,
    }
    if any(not str(value) for value in payload.values()):
        raise ValueError("candidate reuse fingerprint requires every contract and environment hash")
    return _normalized_hash(payload)


def validate_verification_case_order(
    case_kinds: Sequence[VerificationCaseKind | str],
    *,
    historical_required: bool,
) -> None:
    """Enforce the temporal RepairBill gate without ordering unrelated probes."""

    if not historical_required:
        return
    normalized = [
        item if isinstance(item, VerificationCaseKind) else VerificationCaseKind(str(item))
        for item in case_kinds
    ]
    historical_positions = [
        index
        for index, kind in enumerate(normalized)
        if kind == VerificationCaseKind.HISTORICAL_REGRESSION
    ]
    if not historical_positions:
        raise ValueError("verification must run historical RepairBill regressions first")
    risk_positions = [
        index
        for index, kind in enumerate(normalized)
        if kind in {
            VerificationCaseKind.CONTRACT_ADVERSARIAL,
            VerificationCaseKind.DIFF_RISK,
        }
    ]
    if risk_positions and max(historical_positions) > min(risk_positions):
        raise ValueError(
            "verification cases must run historical failures before adversarial and diff-risk cases"
        )


def _transitive_dependents(
    dependency_node_id: str,
    nodes: Mapping[str, AggregateSnapshot],
) -> set[str]:
    affected: set[str] = set()
    frontier = [dependency_node_id]
    while frontier:
        current = frontier.pop()
        for node_id, node in nodes.items():
            dependencies = {str(item) for item in list(node.payload.get("dependency_node_ids") or [])}
            if current in dependencies and node_id not in affected:
                affected.add(node_id)
                frontier.append(node_id)
    affected.discard(dependency_node_id)
    return affected


def _node_action(
    node: AggregateSnapshot,
    action_type: str,
    actor: str,
    payload: Mapping[str, Any],
) -> ActionEnvelope:
    return ActionEnvelope(
        action_type=action_type,
        workflow_id=node.workflow_id,
        aggregate_type=AggregateType.DAG_NODE_RUN,
        aggregate_id=node.aggregate_id,
        actor=actor,
        expected_version=node.version,
        idempotency_key=f"propagate:{node.aggregate_id}:{node.version}:{action_type}",
        payload=dict(payload),
    )


def _normalized_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
