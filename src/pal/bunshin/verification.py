from __future__ import annotations

import json
from pal.bunshin.unit_of_work import BunshinUnitOfWork
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Sequence

from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, DispatchResult
from pal.bunshin.repository import BunshinRepository


class VerificationStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class DefectKind(StrEnum):
    MODULE = "module_defect"
    DEPENDENCY = "dependency_defect"
    VERIFICATION = "verification_defect"
    CONTRACT = "contract_defect"
    ARCHITECTURE = "architecture_defect"
    SINK = "sink_defect"
    REQUIREMENTS = "requirements_defect"


MAX_VERIFICATION_CORRECTIONS = 3


def verification_correction_count(node: AggregateSnapshot) -> int:
    """Return the next invalid-submission ordinal in this producer cycle."""

    if node.payload.get("verification_correction_cycle") != int(node.payload.get("candidate_cycle") or 0):
        return 1
    return int(node.payload.get("verification_correction_attempts") or 0) + 1


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
class VerificationService:
    repository: BunshinRepository
    artifacts: ContentAddressedArtifactStore

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
        system_fingerprint: str = "",
        role_assignment_id: str = "",
        role_submission_payload_hash: str = "",
        accepted_candidate_ref: ArtifactRef | None = None,
        accepted_candidate_digest: str = "",
        unit_of_work: BunshinUnitOfWork | None = None,
        correction_errors: Sequence[str] = (),
    ) -> DispatchResult:
        if bool(accepted_candidate_ref) != bool(accepted_candidate_digest):
            raise ValueError(
                "accepted verifier candidate requires both artifact ref and digest"
            )
        # A failing verifier checkpoint is still a durable Module asset: the
        # next Coder repair must inherit the exact regression tests that
        # exposed the defect.
        common = {
            "verification_artifact_ref": verification_ref.to_dict(),
            "source_pending_verification_ref": dict(node.payload.get("pending_verification_ref") or {}),
            **(
                {
                    "candidate_ref": accepted_candidate_ref.to_dict(),
                    "candidate_digest": accepted_candidate_digest,
                }
                if accepted_candidate_ref is not None
                else {}
            ),
        }
        if correction_errors:
            common.update({
                "verification_correction_cycle": int(node.payload.get("candidate_cycle") or 0),
                "verification_correction_attempts": verification_correction_count(node),
            })
        dependency_targets = tuple(
            dict.fromkeys(
                str(item)
                for item in (dependency_node_id, *dependency_node_ids)
                if str(item)
            )
        )
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
            if (node.payload.get("execution_mode") == "direct"
                    and defect_kind in {DefectKind.CONTRACT, DefectKind.ARCHITECTURE, DefectKind.REQUIREMENTS}):
                from pal.bunshin.direct_contract import task_requirement_blocker
                action_type = "ENTER_TRIAGE"
                payload = {**common, "repair_bill_ref": repair_bill_ref.to_dict(),
                           "failure_history": history,
                           "blocker": task_requirement_blocker(self.artifacts, verification_ref)}
            elif correction_errors and verification_correction_count(node) >= MAX_VERIFICATION_CORRECTIONS:
                action_type = "ENTER_TRIAGE"
                payload = {
                    **common,
                    "repair_bill_ref": repair_bill_ref.to_dict(),
                    "finding_fingerprint": finding_fingerprint_value,
                    "failure_history": history,
                    "blocker": {
                        "kind": "invalid_verifier_submission",
                        "attempt_count": verification_correction_count(node),
                        "errors": list(correction_errors),
                    },
                }
            elif no_progress_detected(history):
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
                    "repair_target_node_id": dependency_targets[0],
                    "repair_target_node_ids": list(dependency_targets),
                    "finding_fingerprint": finding_fingerprint_value,
                    "failure_history": history,
                }
            elif defect_kind == DefectKind.CONTRACT:
                action_type = "CONTRACT_DEFECT"
                payload = {
                    **common,
                    "repair_bill_ref": repair_bill_ref.to_dict(),
                    "failure_history": history,
                }
            elif defect_kind in {
                DefectKind.ARCHITECTURE,
                DefectKind.REQUIREMENTS,
            }:
                action_type = (
                    "REQUIREMENTS_DEFECT"
                    if defect_kind == DefectKind.REQUIREMENTS
                    else "ARCHITECTURE_DEFECT"
                )
                payload = {
                    **common,
                    "repair_bill_ref": repair_bill_ref.to_dict(),
                    "failure_history": history,
                }
            elif defect_kind == DefectKind.VERIFICATION:
                action_type = "VERIFICATION_DEFECT"
                payload = {
                    **common,
                    "repair_bill_ref": repair_bill_ref.to_dict(),
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
        return (unit_of_work or self.repository).transitions.dispatch(
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
            role_assignment_id=role_assignment_id,
            role_submission_payload_hash=role_submission_payload_hash,
        )


@dataclass
class DefectPropagationService:
    repository: BunshinRepository

    def propagate_dependency_defects(
        self, *, workflow_id: str, epoch_id: str, dependency_node_ids: Sequence[str],
        repair_bill_ref: ArtifactRef, actor: str = "bunshin-manager",
        reopen_action: str = "REOPEN_DEPENDENCY",
    ) -> tuple[str, ...]:
        """Reopen a validated provider batch and invalidate consumers atomically."""

        if reopen_action not in {"REOPEN_DEPENDENCY", "REOPEN_VERIFICATION"}:
            raise ValueError(f"unsupported defect reopen action: {reopen_action}")
        target_ids = set(str(item) for item in dependency_node_ids)
        snapshots = self.repository.queries.list_workflow_snapshots(workflow_id)
        node_ids = {
            item.aggregate_id for item in snapshots
            if item.aggregate_type == AggregateType.DAG_NODE_RUN
            and str(item.payload.get("epoch_id") or "") == epoch_id
        }
        if not target_ids or target_ids - node_ids:
            raise ValueError("dependency node does not exist in epoch")
        with self.repository.transaction() as connection:
            applied = {target for target in target_ids if self.repository.queries.has_dependency_repair_receipt(
                target, repair_bill_ref.sha256, reopen_action,
            )}
            nodes = {
                node_id: connection.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
                for node_id in node_ids
            }
            if any(node is None for node in nodes.values()):
                raise ValueError("dependency node disappeared from epoch")
            affected = set().union(*(_transitive_dependents(target, nodes) for target in target_ids)) - target_ids
            # A single transaction applies the whole packet. A crash after its
            # commit must not reopen providers a second time after their repair.
            if all(target in applied or dict(nodes[target].payload.get("repair_bill_ref") or {}).get("sha256") == repair_bill_ref.sha256
                   for target in target_ids):
                return tuple(sorted(affected))
            for target in sorted(target_ids):
                dependency = nodes[target]
                if target in applied or dict(dependency.payload.get("repair_bill_ref") or {}).get("sha256") == repair_bill_ref.sha256:
                    continue
                if dependency.state != "ACCEPTED":
                    raise ValueError(f"repair target is not accepted: {target}")
                connection.transitions.dispatch(_node_action(
                    dependency, reopen_action, actor, {
                        "repair_bill_ref": repair_bill_ref.to_dict(),
                        "source_repair_packet_ref": repair_bill_ref.to_dict(),
                    },
                ))
            for node_id in sorted(affected):
                node = nodes[node_id]
                if node.state in {"STALE", "CANCELLED"}:
                    continue
                payload = {"stale_reason_ref": repair_bill_ref.to_dict(),
                           "stale_dependency_node_id": sorted(target_ids)[0],
                           "stale_dependency_node_ids": sorted(target_ids)}
                action_type = (
                    "MARK_STALE"
                    if node.state in {"BLOCKED_BY_DEPS", "QUEUED", "REVIEW_BLOCKED_BY_DEPS", "REVIEW_QUEUED", "REPAIR_QUEUED", "ACCEPTED"}
                    else "REQUEST_STALE"
                )
                connection.transitions.dispatch(_node_action(node, action_type, actor, payload))
        return tuple(sorted(affected))


def repair_bill_semantic_view(
    artifacts: ContentAddressedArtifactStore,
    repair_bill_ref: ArtifactRef | Mapping[str, Any],
    *, module_name: str = "",
) -> dict[str, Any]:
    """Compile a RepairBill for a worker without exposing manager identities."""

    bill = dict(artifacts.read_json(repair_bill_ref))
    if str(bill.get("artifact_kind") or "") == "semantic_repair_packet":
        findings = [dict(item) for item in list(bill.get("findings") or [])]
        related_findings: list[dict[str, Any]] = []
        owners = dict(bill.get("finding_targets") or {})
        if module_name and owners:
            related_findings = [item for item in findings if module_name not in owners.get(
                str(item.get("finding_id") or item.get("finding_key") or ""), [])]
            findings = [item for item in findings if item not in related_findings]
        return {
            "artifact_kind": "semantic_repair_packet",
            "module_name": module_name or str(bill.get("module_name") or ""),
            **({"source_module_name": str(bill.get("module_name") or "")} if module_name else {}),
            "route": str(bill.get("route") or "module_repair"),
            **{key: bill[key] for key in (
                "classification", "routing_errors", "original_outcome",
                "original_target_modules", "correction_instruction",
            ) if key in bill},
            "target_modules": [
                str(item) for item in list(bill.get("target_modules") or [])
            ],
            "findings": findings,
            **({"related_findings": related_findings,
                "ownership_instruction": "Repair only findings owned by the bound module; other findings remain tracked for their owners."}
               if related_findings else {}),
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
            "disposition": str(item.get("disposition") or "blocking"),
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
            # Current WorkItem findings have a stable Manager-assigned
            # identity rather than the legacy author-supplied case/key.
            # Preserve that identity as the regression's case name instead
            # of silently treating a real RepairPacket as empty history.
            or finding.get("finding_id")
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


def no_progress_detected(history: Sequence[Mapping[str, Any]]) -> bool:
    if len(history) < 3:
        return False
    latest = list(history)[-3:]
    fingerprints = {str(item.get("finding_fingerprint") or "") for item in latest}
    tree_hashes = {str(item.get("candidate_tree_hash") or "") for item in latest}
    return len(fingerprints) == 1 and "" not in fingerprints and len(tree_hashes) == 1 and "" not in tree_hashes


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
            dependencies = {str(item) for item in (*list(node.payload.get("dependency_node_ids") or []),
                                                          *list(node.payload.get("contract_dependency_node_ids") or []))}
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
