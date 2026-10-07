"""Atomically project a captured, exactly closed dependency-repair cohort.

No runtime owner is stopped here. The graph ledger is the authority for closure
and replay; immutable capture artifacts supply evidence, never a fresh verdict
on an invalidated checker binding.
"""
from __future__ import annotations

from typing import Any, Mapping

from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, SubmissionInvariantError
from pal.bunshin.dependency_repair_protocol import DependencyRepairCohort
from pal.bunshin.graph_executor import GraphExecution
from pal.bunshin.repository import BunshinV2Repository
from pal.bunshin.semantic_orchestration.dependency_repair_facts import append_refs, node_name
from pal.bunshin.storage.queries import QueriesStore
from pal.bunshin.semantic_orchestration.dependency_repair_budget import dependency_failure_history


_INACTIVE = frozenset({"BLOCKED_BY_DEPS", "QUEUED", "REVIEW_BLOCKED_BY_DEPS", "REVIEW_QUEUED",
                       "REPAIR_QUEUED", "ACCEPTED", "STALE"})
_REF_KEYS = frozenset(ArtifactRef.__dataclass_fields__)


def apply_dependency_repair_cohort(
    *, repository: BunshinV2Repository, artifacts: ContentAddressedArtifactStore,
    graph_execution: GraphExecution, cohort_key: str,
    capture_refs: Mapping[str, ArtifactRef | Mapping[str, Any]] | None = None,
    command_id: str = "",
) -> GraphExecution:
    """Commit provider repairs, consumer invalidation, and exact report receipts.

    ``capture_refs`` is keyed by frozen incarnation key. If omitted, the refs
    are recovered from the complete closure receipt sets in the ledger. Passed
    references must be exactly those receipts, not replacement report payloads.
    """
    expected = _cohort(graph_execution, cohort_key)
    first = next(iter(expected.intents.values()))
    with repository.transaction() as unit:
        source = unit.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, first.source_aggregate_id)
        if source is None:
            raise SubmissionInvariantError("dependency repair source aggregate disappeared")
        current = unit.cycles.read_graph_execution(workflow_id=source.workflow_id)
        if current is None or (current.graph.graph_id, current.graph.generation, current.graph.generation_hash) != (
            graph_execution.graph.graph_id, graph_execution.graph.generation, graph_execution.graph.generation_hash,
        ):
            raise SubmissionInvariantError("dependency repair graph generation changed")
        cohort = _cohort(current, cohort_key)
        if cohort.status == "superseded":
            raise SubmissionInvariantError("superseded dependency repair cannot apply")
        store = ContentAddressedArtifactStore(artifacts.runtime_root, unit.transitions.artifacts)
        queries = QueriesStore(unit.transitions.database, unit.transitions.projections)
        captures = _captures(store, cohort, capture_refs)
        if any(capture.get("workflow_id") != source.workflow_id for _, capture in captures.values()):
            raise SubmissionInvariantError("dependency repair capture belongs to another workflow")
        _validate_intents(cohort, captures)
        if cohort.status == "applied":
            _validate_replay(store, queries, cohort, captures)
            return current
        # Re-read under the same admission write lock. In particular, a later
        # joined report must change the key rather than silently disappear.
        if not cohort.ready:
            raise SubmissionInvariantError("dependency repair frontier is not completely closed")
        nodes = _nodes(queries, source, cohort)
        by_node = {member.node_name: member for member in cohort.frontier.values()}
        for name, node in nodes.items():
            member = by_node.get(name)
            if member is None and node.state not in _INACTIVE:
                raise SubmissionInvariantError(f"dependency repair omitted active aggregate {name}")
            if member is not None and (member.aggregate_id != node.aggregate_id or member.key not in cohort.closures):
                raise SubmissionInvariantError("dependency repair aggregate has no exact closure")
            if node.state in {"CANCELLED", "PAUSED", "PAUSE_REQUESTED", "TRIAGE_REQUIRED"}:
                raise SubmissionInvariantError("operator or terminal control dominates dependency repair")
            if node.state == "CANCEL_REQUESTED" and node.payload.get("cancel_target") != "STALE":
                raise SubmissionInvariantError("user cancellation dominates dependency repair")
        updated, route = current.apply_dependency_repair(cohort_key=cohort_key)
        if route is None:
            raise SubmissionInvariantError("pending dependency repair unexpectedly replayed")
        capture_values = [ref.to_dict() for ref, _ in captures.values()]
        applied_ref = _publish(store, {
            "schema_version": "1", "cohort_key": cohort.key, "graph_id": current.graph.graph_id,
            "generation": current.graph.generation, "generation_hash": current.graph.generation_hash,
            "provider_nodes": list(cohort.providers), "scope": list(cohort.scope),
            "closed_incarnation_keys": sorted(cohort.closures), "capture_refs": capture_values,
            "source_packet_refs": [dict(intent.packet_ref) for _, intent in sorted(cohort.intents.items())],
        }, "DependencyRepairAppliedArtifact")
        # Historical bills also retain each preserved corpus checkpoint, even
        # for a PASS/no-submission peer. Such a packet carries no invented case.
        histories: dict[str, list[dict[str, Any]]] = {}
        for name, node in nodes.items():
            history = _history(store, node.payload)
            member = by_node.get(name)
            if member is not None:
                capture_ref, capture = captures[member.key]
                history = append_refs(history, *_history(store, capture))
                packet = dict(capture.get("repair_packet_ref") or {})
                history = append_refs(history, packet)
                preserved = _publish(store, {
                    "schema_version": "1", "artifact_kind": "semantic_repair_packet", "module_name": name,
                    "route": "dependency_repair_preservation", "target_modules": [], "findings": [],
                    "candidate_ref": dict(capture.get("candidate_ref") or {}),
                    "candidate_digest": str(capture.get("candidate_digest") or ""),
                    "verification_ref": dict(capture.get("report_ref") or {}),
                    "repair_packet_ref": packet, "capture_ref": capture_ref.to_dict(),
                    "settlement_status": "invalidated", "cohort_key": cohort.key,
                }, "RepairPacketArtifact")
                history = append_refs(history, preserved.to_dict())
            history = append_refs(history, *(dict(intent.packet_ref) for intent in cohort.intents.values()
                                               if intent.source_node == name))
            histories[name] = history
        packets = {name: _combined_packet(store, cohort, name, histories[name], captures)
                   for name in cohort.providers}
        for name in sorted(nodes):
            node = nodes[name]
            member = by_node.get(name)
            payload: dict[str, Any] = {
                "dependency_repair_applied_ref": applied_ref.to_dict(), "dependency_repair_cohort_key": cohort.key,
                "historical_repair_bill_refs": histories[name], "stale_reason_ref": applied_ref.to_dict(),
                "stale_dependency_node_ids": [nodes[provider].aggregate_id for provider in cohort.providers],
            }
            if member is not None:
                capture_ref, capture = captures[member.key]
                payload["dependency_repair_capture_ref"] = capture_ref.to_dict()
                if any(intent.source_node == name for intent in cohort.intents.values()):
                    # A validated routed dependency failure consumes the same
                    # no-progress history as the former direct verdict path.
                    # Invalidated peer PASS/local/correction reports do not.
                    payload["failure_history"] = dependency_failure_history(store, node, capture)
                candidate = dict(capture.get("candidate_ref") or {})
                if candidate:
                    payload.update(candidate_ref=candidate, candidate_digest=str(capture.get("candidate_digest") or ""))
                report_ref = dict(capture.get("report_ref") or {})
                if report_ref:
                    pending_ref = _pending(capture)
                    report = store.read_json(report_ref)
                    if _sha(report.get("source_pending_verification_ref")) != _sha(pending_ref):
                        raise SubmissionInvariantError("captured report lost its exact pending verification binding")
                    receipt = _publish(store, {
                        "schema_version": "1", "status": "invalidated", "settlement_status": "invalidated",
                        "reason": "dependency inputs changed after the admitted checker binding",
                        "cohort_key": cohort.key, "incarnation_key": member.key, "node_run_id": node.aggregate_id,
                        "invocation_id": str(capture.get("invocation_id") or ""),
                        "source_pending_verification_ref": pending_ref, "verification_artifact_ref": report_ref,
                        "capture_ref": capture_ref.to_dict(), "candidate_ref": candidate,
                        "dependency_repair_applied_ref": applied_ref.to_dict(),
                    }, "DependencyRepairSettlementArtifact")
                    payload.update(source_pending_verification_ref=pending_ref,
                                   verification_artifact_ref=receipt.to_dict(), verification_status="invalidated")
            # This projection carries no cleanup effect; the exact process,
            # lease and workspace proofs have already been checked by the graph.
            node = unit.transitions.dispatch(_action(node, "SETTLE_DEPENDENCY_REPAIR", payload, cohort.key, command_id)).snapshot
            if name in packets:
                packet = packets[name]
                unit.transitions.dispatch(_action(node, "REOPEN_DEPENDENCY", {
                    "repair_bill_ref": packet.to_dict(), "source_repair_packet_ref": packet.to_dict(),
                    "dependency_repair_applied_ref": applied_ref.to_dict(),
                    "historical_repair_bill_refs": append_refs(histories[name], packet.to_dict()),
                }, cohort.key, command_id))
        unit.cycles.store_graph_execution(workflow_id=source.workflow_id, execution=updated)
        return updated


def _cohort(execution: GraphExecution, key: str) -> DependencyRepairCohort:
    for cohort in (*execution.dependency_repairs.history,
                   *((execution.dependency_repairs.pending,) if execution.dependency_repairs.pending else ())):
        if cohort.key == key:
            return cohort
    raise SubmissionInvariantError("dependency repair cohort changed or does not exist")


def _durable(store: ContentAddressedArtifactStore, value: ArtifactRef | Mapping[str, Any]) -> ArtifactRef:
    supplied = value.to_dict() if isinstance(value, ArtifactRef) else dict(value)
    if set(supplied) - _REF_KEYS:
        raise SubmissionInvariantError("dependency repair accepts artifact references, not payloads")
    row = store.metadata_repository.read_artifact_record(str(supplied.get("sha256") or ""))
    if row is None or not row.get("durable"):
        raise SubmissionInvariantError("dependency repair evidence is not durable")
    ref = ArtifactRef.from_mapping(row)
    if any(supplied.get(key, expected) != expected for key, expected in ref.to_dict().items()):
        raise SubmissionInvariantError("dependency repair reference metadata changed")
    store.read_bytes(ref)
    return ref


def _captures(store, cohort, requested):
    if requested is not None and set(requested) != set(cohort.frontier):
        raise SubmissionInvariantError("dependency repair captures must cover the exact frozen frontier")
    captures = {}
    for key, member in sorted(cohort.frontier.items()):
        if key not in cohort.closures:
            raise SubmissionInvariantError("dependency repair capture has no exact closure")
        refs = [_durable(store, value) for value in cohort.closures[key].receipt_refs]
        candidates = [ref for ref in refs if ref.artifact_type == "DependencyRepairCaptureArtifact"]
        if len(candidates) != 1:
            raise SubmissionInvariantError("dependency repair requires one final capture per incarnation")
        ref = candidates[0]
        if requested is not None and _durable(store, requested[key]) != ref:
            raise SubmissionInvariantError("dependency repair capture was not in the exact closure receipt set")
        value = dict(store.read_json(ref))
        expected = {"incarnation_key": key, "node_name": member.node_name,
                    "node_run_id": member.aggregate_id, "slot": member.slot,
                    "source_assignment_id": member.role_assignment_id,
                    "lease_resource_key": member.lease_resource, "fencing_token": member.fencing_token,
                    "invocation_id": member.lease_owner}
        if any(value.get(name) != expected_value for name, expected_value in expected.items()):
            raise SubmissionInvariantError("dependency repair capture does not match its frozen incarnation")
        for name in ("candidate_ref", "source_candidate_ref", "repair_packet_ref", "report_ref", "pending_ref", "source_pending_ref"):
            if value.get(name):
                _durable(store, value[name])
        captures[key] = (ref, value)
    return captures


def _validate_intents(cohort, captures):
    for intent in cohort.intents.values():
        members = [member for member in cohort.frontier.values() if member.node_name == intent.source_node]
        if len(members) != 1:
            raise SubmissionInvariantError("dependency repair intent has no exact source incarnation")
        capture = captures[members[0].key][1]
        if (not intent.pending_verification_ref or _sha(_pending(capture)) != _sha(intent.pending_verification_ref)
                or _sha(capture.get("source_candidate_ref")) != _sha(intent.candidate_ref)
                or capture.get("source_candidate_digest") != intent.candidate_digest
                or capture.get("source_assignment_id") != intent.source_assignment_id
                or capture.get("source_payload_hash") != intent.submission_payload_hash
                or _sha(capture.get("repair_packet_ref")) != intent.packet_sha256
                or capture.get("status") != "FAIL" or capture.get("defect_kind") != "dependency_defect"
                or capture.get("routing_errors")
                or not str(capture.get("finding_fingerprint") or "")
                or set(capture.get("target_modules") or []) != set(intent.provider_nodes)):
            raise SubmissionInvariantError("dependency repair intent changed its captured source receipt")


def _validate_replay(store, queries, cohort, captures):
    for key, (_, capture) in captures.items():
        if not capture.get("report_ref"):
            continue
        pending = _pending(capture)
        member = cohort.frontier[key]
        receipt_ref = queries.read_verification_settlement_ref(member.aggregate_id, _sha(pending))
        if not receipt_ref:
            raise SubmissionInvariantError("applied dependency repair lost its exact settlement receipt")
        receipt = store.read_json(_durable(store, receipt_ref))
        if (receipt.get("settlement_status") != "invalidated" or receipt.get("cohort_key") != cohort.key
                or receipt.get("incarnation_key") != key or _sha(receipt.get("source_pending_verification_ref")) != _sha(pending)
                or _sha(receipt.get("verification_artifact_ref")) != _sha(capture.get("report_ref"))):
            raise SubmissionInvariantError("dependency repair replay receipt changed its original binding")


def _nodes(queries, source, cohort):
    epoch = str(source.payload.get("epoch_id") or "")
    nodes = {}
    for node in queries.list_workflow_snapshots(source.workflow_id):
        if (node.aggregate_type != AggregateType.DAG_NODE_RUN or node_name(node) not in cohort.scope
                or str(node.payload.get("epoch_id") or "") != epoch):
            continue
        if node_name(node) in nodes:
            raise SubmissionInvariantError("dependency repair has ambiguous aggregates in its epoch")
        nodes[node_name(node)] = node
    if set(nodes) != set(cohort.scope):
        raise SubmissionInvariantError("dependency repair scope has missing aggregate projections")
    return nodes


def _history(store, payload):
    refs = append_refs(payload.get("historical_repair_bill_refs"), dict(payload.get("repair_bill_ref") or {}))
    for ref in refs:
        _durable(store, ref)
    return refs


def _combined_packet(store, cohort, provider, own_history, captures):
    refs = append_refs(own_history, *(dict(intent.packet_ref) for intent in cohort.intents.values()
                                     if provider in intent.provider_nodes))
    for _, capture in captures.values():
        if provider == capture["node_name"] or provider in list(capture.get("target_modules") or []):
            refs = append_refs(refs, dict(capture.get("repair_packet_ref") or {}))
    findings, owners, commands, paths = [], {}, set(), set()
    identities = {}
    for ref in sorted(refs, key=lambda item: item["sha256"]):
        packet = dict(store.read_json(_durable(store, ref)))
        mapping = dict(packet.get("finding_targets") or {})
        origin = str(packet.get("module_name") or provider)
        for index, raw in enumerate(packet.get("findings") or []):
            finding = dict(raw)
            identity = str(finding.get("finding_id") or finding.get("finding_key") or index)
            # Correction/revision packets stay historical obligations. They
            # cannot become implementation ownership merely by aggregation.
            correction = (packet.get("classification") == "invalid_verifier_submission"
                          or packet.get("route") in {"verification_correction", "contract_revision",
                                                    "architecture_revision", "requirements_revision"})
            targets = list(mapping.get(identity, [] if correction else [origin]))
            original = (finding, targets)
            scoped_identity = identity
            if identity in identities:
                if identities[identity] == original:
                    continue
                scoped_identity = f"{ref['sha256']}:{identity}"
            identities[scoped_identity] = original
            finding = {**finding, "finding_id": scoped_identity}
            if correction:
                finding["source_packet_route"] = str(packet.get("route") or "")
                finding["source_packet_classification"] = str(packet.get("classification") or "")
            findings.append(finding)
            owners[scoped_identity] = targets
        commands.update(str(item) for item in packet.get("regression_commands") or [])
        paths.update(str(item) for item in packet.get("changed_test_paths") or [])
    return _publish(store, {
        "schema_version": "1", "artifact_kind": "semantic_repair_packet", "module_name": provider,
        "route": "dependency_repair_cohort", "target_modules": [provider], "cohort_key": cohort.key,
        "findings": findings, "finding_targets": owners,
        "source_packet_refs": sorted(refs, key=lambda item: item["sha256"]),
        "regression_commands": sorted(commands), "changed_test_paths": sorted(paths),
    }, "RepairPacketArtifact")


def _pending(capture):
    pending = dict(capture.get("source_pending_ref") or capture.get("pending_ref") or {})
    if not pending.get("sha256"):
        raise SubmissionInvariantError("captured verifier report has no immutable pending receipt")
    return pending


def _publish(store, payload, kind):
    children = tuple((str(ref["sha256"]), name) for name, value in payload.items()
                     for ref in (value if isinstance(value, list) else [value])
                     if isinstance(ref, Mapping) and ref.get("sha256"))
    return store.put_json(payload, artifact_type=kind, provenance={"owner": "manager", "purpose": "dependency_repair_apply"},
                          child_refs=children)


def _sha(value):
    return str(dict(value or {}).get("sha256") or "")


def _action(node, action, payload, cohort_key, command_id):
    return ActionEnvelope(action_type=action, workflow_id=node.workflow_id, aggregate_type=AggregateType.DAG_NODE_RUN,
                          aggregate_id=node.aggregate_id, actor="bunshin-manager", payload=payload,
                          expected_version=node.version, correlation_id=command_id,
                          idempotency_key=f"dependency-repair:{cohort_key}:{node.aggregate_id}:{action}")
