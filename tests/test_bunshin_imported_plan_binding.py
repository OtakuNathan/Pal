"""Exact provenance and transaction guards for imported PlanCycle products."""
from dataclasses import replace

import pytest

from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateType, SubmissionInvariantError
from pal.bunshin.cycle_protocol import CycleAction, CycleAssignment, CycleSlot, AssignmentKind, CycleTransitionError, PlanCycle, PlanCycleState
from pal.bunshin.imported_plan import bind_imported_plan_product
from pal.bunshin.repository import BunshinRepository


class ImportedBinding:
    def __init__(self, root):
        self.repo = BunshinRepository(root)
        self.artifacts = ContentAddressedArtifactStore(root, self.repo.artifacts)
        self.requirements = self.put({}, "TaskLedgerArtifact")
        self.product = self.put({"requirements_ref": self.requirements}, "ContractArtifact")
        self.request = {"operation": "review_then_execute", "input_artifact_ref": self.product,
                        "requirements_ref": self.requirements}
        self.action(AggregateType.WORKFLOW, "wf-import", "CREATE_WORKFLOW", {
            "request_ref": self.put(self.request, "WorkflowRequestArtifact")})
        self.action(AggregateType.WORKFLOW, "wf-import", "START_WORKFLOW")
        self.action(AggregateType.ARCHITECTURE_REVISION, "revision-import", "IMPORT_ARCHITECTURE_REVISION", {
            "architecture_manifest_ref": self.product, "requirements_ref": self.requirements, "revision_number": 1})
        self.action(AggregateType.WORKFLOW, "wf-import", "LINK_ARCHITECTURE_REVISION", {
            "architecture_revision_id": "revision-import"})

    def put(self, body, kind):
        return self.artifacts.put_json(body, artifact_type=kind).to_dict()

    def action(self, kind, identifier, action, payload=None):
        current = self.repo.snapshots.read_snapshot(kind, identifier)
        return self.repo.transitions.dispatch(ActionEnvelope(action_type=action, workflow_id="wf-import",
            aggregate_type=kind, aggregate_id=identifier, actor="test", expected_version=current.version if current else 0,
            idempotency_key=identifier + ":" + action, payload=payload or {})).snapshot

    def revision(self):
        return self.repo.snapshots.read_snapshot(AggregateType.ARCHITECTURE_REVISION, "revision-import")

    def bind(self):
        with self.repo.transaction() as work:
            return bind_imported_plan_product(repository=self.repo, artifacts=self.artifacts,
                                              revision=self.revision(), unit_of_work=work)

    def change_payload(self, kind, identifier, **fields):
        with self.repo.database.write_connection() as connection:
            old = self.repo.snapshots.read_snapshot(kind, identifier, _connection=connection)
            self.repo.snapshots.write_snapshot_locked(connection, old,
                replace(old, payload={**old.payload, **fields}))


def test_import_binds_product_without_admitting_producer_or_accepting_graph(tmp_path):
    case = ImportedBinding(tmp_path)
    assert case.bind()
    cycle = case.repo.cycles.read_plan_cycle(workflow_id="wf-import")
    assert cycle.state == PlanCycleState.CHECKER_READY
    assert cycle.product_ref == case.product["sha256"]
    assert cycle.active_assignment is None and not cycle.accepted_product_ref
    assert case.repo.cycles.read_graph_execution(workflow_id="wf-import") is None
    assert case.bind()
    assert case.repo.cycles.read_plan_cycle(workflow_id="wf-import") == cycle


@pytest.mark.parametrize("corruption", ["request_operation", "request_product", "requirements", "pointer", "manifest", "metadata", "non_durable", "missing_import"])
def test_import_binding_rejects_unproven_current_product_atomically(tmp_path, corruption):
    case = ImportedBinding(tmp_path)
    if corruption.startswith("request_"):
        request = dict(case.request)
        if corruption == "request_operation":
            request["operation"] = "new_requirement"
        else:
            request["input_artifact_ref"] = case.put({"other": True}, "ContractArtifact")
        case.change_payload(AggregateType.WORKFLOW, "wf-import", request_ref=case.put(request, "WorkflowRequestArtifact"))
    elif corruption == "requirements":
        case.change_payload(AggregateType.ARCHITECTURE_REVISION, "revision-import", requirements_ref=case.put({"other": True}, "TaskLedgerArtifact"))
    elif corruption == "pointer":
        case.change_payload(AggregateType.WORKFLOW, "wf-import", architecture_revision_id="another-revision")
    elif corruption in {"manifest", "metadata"}:
        product = case.put({"other": True}, "ContractArtifact") if corruption == "manifest" else {**case.product, "byte_size": 1}
        case.change_payload(AggregateType.ARCHITECTURE_REVISION, "revision-import", architecture_manifest_ref=product)
    else:
        with case.repo.database.write_connection() as connection:
            if corruption == "non_durable":
                connection.execute("UPDATE bunshin_v2_artifacts SET durable=0 WHERE sha256=?", (case.product["sha256"],))
            else:
                connection.execute("DELETE FROM bunshin_v2_domain_events WHERE aggregate_id='revision-import'")
    if corruption == "missing_import":
        assert not case.bind()  # ordinary authored reviews get no import privilege
    else:
        with pytest.raises(SubmissionInvariantError):
            case.bind()
    assert case.repo.cycles.read_plan_cycle(workflow_id="wf-import") is None


def test_rollback_cannot_leave_import_product_bound(tmp_path):
    case = ImportedBinding(tmp_path)
    with pytest.raises(RuntimeError, match="after import"):
        with case.repo.transaction() as work:
            bind_imported_plan_product(repository=case.repo, artifacts=case.artifacts,
                                       revision=case.revision(), unit_of_work=work)
            raise RuntimeError("after import")
    assert case.repo.cycles.read_plan_cycle(workflow_id="wf-import") is None
    assert case.bind()


def test_import_does_not_relax_generic_checker_admission_or_replace_active_cycle(tmp_path):
    cycle = PlanCycle(cycle_id="plan")
    checker = CycleAssignment(CycleSlot.CHECKER, AssignmentKind.INITIAL, 1, "checker")
    with pytest.raises(CycleTransitionError):
        cycle.transition(CycleAction.START_CHECKER, assignment=checker)
    for old in (replace(cycle, generation=2), replace(cycle, product_ref="another-product"),
                cycle.transition(CycleAction.START_PRODUCER,
                    assignment=CycleAssignment(CycleSlot.PRODUCER, AssignmentKind.INITIAL, 1, "producer"))):
        with pytest.raises(CycleTransitionError):
            old.transition(CycleAction.IMPORT_PRODUCT, product_ref="imported-product")
