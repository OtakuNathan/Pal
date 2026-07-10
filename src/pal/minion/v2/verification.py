from __future__ import annotations

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
    contract_refs: tuple[str, ...] = ()
    description: str = ""


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
    contract_refs: tuple[str, ...]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "case_kind": self.case_kind.value,
            "status": self.status.value,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "stdout_ref": dict(self.stdout_ref),
            "stderr_ref": dict(self.stderr_ref),
            "environment": dict(self.environment),
            "contract_refs": list(self.contract_refs),
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
            contract_refs=case.contract_refs,
            summary=summary,
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
        test_workspace_ref: Mapping[str, Any] | None = None,
    ) -> tuple[ArtifactRef, VerificationStatus]:
        if not case_results:
            raise ValueError("verification report requires at least one test case")
        if node.payload.get("historical_repair_bill_refs") and not any(
            item.case_kind == VerificationCaseKind.HISTORICAL_REGRESSION for item in case_results
        ):
            raise ValueError("verification must run historical RepairBill regressions first")
        _validate_case_order(case_results)
        status = aggregate_verification_status(item.status for item in case_results)
        payload = {
            "schema_version": "1",
            "workflow_id": node.workflow_id,
            "node_run_id": node.aggregate_id,
            "candidate_ref": dict(candidate_ref),
            "status": status.value,
            "cases": [item.to_dict() for item in case_results],
            "reviewer_summary": reviewer_summary,
            "test_workspace_ref": dict(test_workspace_ref or {}),
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
        candidate_sha: str,
        verification_ref: ArtifactRef,
        defect_kind: DefectKind,
        severity: str,
        contract_refs: Sequence[str],
        minimal_reproducer_ref: Mapping[str, Any],
        test_artifact_ref: Mapping[str, Any],
        expected: Any,
        actual: Any,
        suggested_repair_boundary: Sequence[str],
    ) -> tuple[ArtifactRef, str]:
        fingerprint = finding_fingerprint(
            defect_kind=defect_kind,
            contract_refs=contract_refs,
            reproducer_hash=str(minimal_reproducer_ref.get("sha256") or ""),
            expected=expected,
            actual=actual,
        )
        payload = {
            "schema_version": "1",
            "workflow_id": node.workflow_id,
            "node_run_id": node.aggregate_id,
            "candidate_sha": candidate_sha,
            "verification_artifact_ref": verification_ref.to_dict(),
            "defect_kind": defect_kind.value,
            "severity": severity,
            "contract_refs": list(contract_refs),
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
    ) -> DispatchResult:
        common = {"verification_artifact_ref": verification_ref.to_dict()}
        if status == VerificationStatus.PASS:
            action_type = "REVIEW_PASSED"
            payload = common
        elif status == VerificationStatus.NOT_APPLICABLE:
            action_type = "REVIEW_PASSED"
            payload = {**common, "not_applicable": True}
        elif status == VerificationStatus.UNKNOWN:
            policy = unknown_policy or UnknownPolicy(False, None, True)
            if policy.allows():
                action_type = "REVIEW_UNKNOWN_ALLOWED"
                payload = {
                    **common,
                    "policy_allows_unknown": True,
                    "assumption_ref": dict(policy.assumption_ref or {}),
                    "hard_or_core_semantics": policy.hard_or_core_semantics,
                    "human_waiver_ref": dict(policy.human_waiver_ref or {}),
                }
            else:
                if repair_bill_ref is None:
                    raise ValueError("blocking UNKNOWN requires a RepairBill")
                action_type = "REVIEW_FAILED"
                payload = {
                    **common,
                    "repair_bill_ref": repair_bill_ref.to_dict(),
                    "finding_fingerprint": finding_fingerprint_value,
                    "unknown_blocking": True,
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
                if not dependency_node_id:
                    raise ValueError("dependency defect requires dependency_node_id")
                action_type = "DEPENDENCY_DEFECT"
                payload = {
                    **common,
                    "repair_bill_ref": repair_bill_ref.to_dict(),
                    "dependency_node_id": dependency_node_id,
                    "finding_fingerprint": finding_fingerprint_value,
                    "failure_history": history,
                }
            elif defect_kind == DefectKind.CONTRACT:
                action_type = "CONTRACT_DEFECT"
                payload = {**common, "repair_bill_ref": repair_bill_ref.to_dict(), "failure_history": history}
            elif defect_kind == DefectKind.ARCHITECTURE:
                action_type = "ARCHITECTURE_DEFECT"
                payload = {**common, "repair_bill_ref": repair_bill_ref.to_dict(), "failure_history": history}
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
            )
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


def no_progress_detected(history: Sequence[Mapping[str, Any]]) -> bool:
    if len(history) < 3:
        return False
    latest = list(history)[-3:]
    fingerprints = {str(item.get("finding_fingerprint") or "") for item in latest}
    tree_hashes = {str(item.get("candidate_tree_hash") or "") for item in latest}
    return len(fingerprints) == 1 and "" not in fingerprints and len(tree_hashes) == 1 and "" not in tree_hashes


def candidate_reuse_fingerprint(
    *,
    module_contract_hash: str,
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
        "module_contract_hash": module_contract_hash,
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


def _validate_case_order(case_results: Sequence[VerificationCaseResult]) -> None:
    rank = {
        VerificationCaseKind.HISTORICAL_REGRESSION: 0,
        VerificationCaseKind.CONTRACT_ADVERSARIAL: 1,
        VerificationCaseKind.DIFF_RISK: 2,
        VerificationCaseKind.COMPILE: 3,
        VerificationCaseKind.LSP: 3,
        VerificationCaseKind.UNIT: 3,
        VerificationCaseKind.CONSUMER_PROBE: 3,
        VerificationCaseKind.PLATFORM_ASSUMPTION: 4,
    }
    ranks = [rank[item.case_kind] for item in case_results]
    if ranks != sorted(ranks):
        raise ValueError("verification cases must run historical failures before adversarial and diff-risk cases")


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
