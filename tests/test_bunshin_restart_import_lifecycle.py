"""Exercise public restart through real Git architecture and durable outbox paths."""
from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import yaml

from pal.bunshin.architecture_compilation import ArchitectureTemplateCompiler
from pal.bunshin.workflow_capabilities import (
    BunshinCapabilitiesBunshinPublicProviderStartWorkflowInput,
    BunshinPublicProvider,
)
from pal.bunshin.contract_submission import architect_path
from pal.bunshin.contracts import ActionEnvelope, AggregateType
from pal.bunshin.cycle_protocol import AssignmentKind, CycleSlot, CycleTransitionError, PlanCycleState
from pal.bunshin.graph_compiler import GraphCompiler
from pal.bunshin.graph_protocol import EdgeKind, graph_ir_from_mapping
from pal.bunshin.orchestration import BunshinOutboxProcessor
from pal.bunshin.role_gateway import RoleAssignmentGateway
from pal.bunshin.role_protocol import RoleAssignmentRequest
from pal.bunshin.semantic_orchestration.orchestrator import SemanticOrchestrator
from pal.bunshin.service import BunshinWorkflowService
from pal.bunshin.submission_drafts import AUTHORING_CONTRACT_VERSION
from pal.bunshin.workflow_runtime import WorkflowCoordinator
from pal.execution.contracts import CapabilityCall
from pal.shared import RuntimeStatus


class BunshinRestartImportLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pal-restart-import-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = BunshinWorkflowService(self.root)
        self.repository = self.service.repository
        self.worker = SemanticOrchestrator(self.service)
        self.processor = BunshinOutboxProcessor(self.service, semantic_effects=self.worker)
        self.provider = BunshinPublicProvider(runtime_root=self.root, wake_manager=lambda: None)
        self.gateway = RoleAssignmentGateway(self.service)
        self.source_id = "wf_restart_import_source"
        self.task_title = "Restart import regression"
        self.architect_submissions: list[dict] = []
        self.reviewed_manifests: list[dict] = []
        self.reviewed_workspaces: list[Path] = []
        self.contract = copy.deepcopy(
            ArchitectureTemplateCompiler().compile("software_engineering.v1").example
        )
        # Add an ordinary, non-sink produced consumer. Legacy software graphs
        # called this edge CONTRACT; the sink edge was already EXECUTION.
        application = copy.deepcopy(self.contract["modules"]["delivery"])
        application["definition"]["paths"] = {
            "contract_mode": "review_guarded",
            "contract_paths": ["include/consumer.hpp"],
            "implementation_scopes": [{"kind": "file", "path": "src/consumer.cpp"}],
            "reference_only": [],
        }
        self.contract["modules"]["consumer"] = application
        self.contract["modules"]["delivery"]["dependencies"] = {
            "consumer": {
                "consumes": ["application"],
                "purpose": "Deliver the consumer entrypoint.",
                "handoff": "Link the independently verified consumer product.",
            }
        }
        self.contract["scenarios"]["decode_one_frame"]["modules"] = [
            "decoder", "consumer", "delivery"
        ]
        self.contract["scenarios"]["decode_one_frame"]["contract_flow"] = [
            "bytes -> decoder -> consumer -> delivery -> application"
        ]
        self.repo_path = self.root / "source"
        self.repo_path.mkdir()
        for path, content in {
            "include/decoder.hpp": "// Decode complete frames through Decoder::feed.\nclass Decoder;\n",
            "include/consumer.hpp": "// Consume Decoder through its public contract.\nint run_consumer();\n",
            "include/application.hpp": "// Deliver the composed application.\nint run_application();\n",
            "src/decoder.cpp": "// implementation placeholder\n",
        }.items():
            target = self.repo_path / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self.service.create_task({
            "task_id": "task_restart_import",
            "title": self.task_title,
            "objective": "Decode complete frames and deliver the consumer.",
            "profile": "software_engineering.v2_coder",
            "workspace": {
                "kind": "existing_repo", "repo_path": str(self.repo_path),
                "project_name": "restart-import",
            },
            "actor": "nathan", "source_channel": "test",
        })
        prepared = self.service.prepare_requirements({
            "title": self.task_title,
            "task_spec": {"objective": "Decode complete frames and deliver the consumer."},
        })
        self.requirements_ref = prepared["requirements_ref"]
        self.service.start_workflow({
            "task_id": "task_restart_import", "workflow_id": self.source_id,
            "operation": "new_requirement", "requirements_ref": self.requirements_ref,
            "goal": "Decode complete frames and deliver the consumer.",
            "actor": "nathan", "source_channel": "test",
            "delivery_binding": {
                "channel_id": "socket_test", "channel_kind": "socket",
                "reply_target": {"session_id": "test-session", "request_id": "test-request"},
                "control_scope_key": "socket:socket_test:test-session",
            },
        })
        # Keep real semantic dispatch and claimed outbox processing, but await
        # each worker in place rather than starting an operating-system worker.
        async def inline_worker(effect, runner):
            return await runner(effect)
        for target, method, replacement in (
            (self.worker.components.effect_dispatch, "launch_background_worker", inline_worker),
            (self.worker.components.attempt_execution, "run_profile", self._run_profile),
        ):
            patcher = patch.object(target, method, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.processed_effects: list[dict] = []
        self.import_rollback_checked = False
        self.legacy_import = False
        original_process = self.processor.dispatcher._process_effect
        async def capture_effect(effect):
            self.processed_effects.append(dict(effect))
            if (effect["effect_type"] == "route_workflow"
                    and effect["workflow_id"] != self.source_id
                    and not self.import_rollback_checked
                    and not self.legacy_import):
                # This is still the same legitimately claimed outbox effect.
                # Crash after cycle binding, before the transaction commits;
                # its normal retry must leave no partial import or receipt.
                from pal.bunshin import imported_plan
                bind = imported_plan.bind_imported_plan_product
                def fail_after_binding(**options):
                    bind(**options)
                    raise RuntimeError("injected import transaction failure")
                with patch.object(imported_plan, "bind_imported_plan_product", fail_after_binding):
                    with self.assertRaisesRegex(RuntimeError, "injected import transaction failure"):
                        await self.processor._execute_mechanical(effect)
                workflow_id = effect["workflow_id"]
                self.assertIsNone(self._revision(workflow_id))
                self.assertIsNone(self.repository.cycles.read_plan_cycle(workflow_id=workflow_id))
                self.assertFalse(any(
                    item.aggregate_type == AggregateType.ARCHITECTURE_REVISION
                    for item in self.repository.queries.list_workflow_snapshots(workflow_id)
                ))
                self.import_rollback_checked = True
            return await original_process(effect)
        patcher = patch.object(self.processor.dispatcher, "_process_effect", capture_effect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _snapshot(self, kind, identity):
        return self.repository.snapshots.read_snapshot(kind, identity)

    def _revision(self, workflow_id):
        workflow = self._snapshot(AggregateType.WORKFLOW, workflow_id)
        identity = str(workflow.payload.get("architecture_revision_id") or "")
        return self._snapshot(AggregateType.ARCHITECTURE_REVISION, identity) if identity else None

    def test_public_restart_of_cancelled_plan_keeps_exact_architect_clarification(self):
        effect = {
            "aggregate_type": AggregateType.WORKFLOW.value,
            "aggregate_id": self.source_id,
            "workflow_id": self.source_id,
            "effect_key": "cancelled-plan-recovery",
        }
        self.processor._reconcile_workflow(effect)
        self.processor._route_workflow(effect)
        revision = self._revision(self.source_id)
        self.repository.transitions.dispatch(ActionEnvelope(
            action_type="START_ARCHITECT", workflow_id=self.source_id,
            aggregate_type=AggregateType.ARCHITECTURE_REVISION, aggregate_id=revision.aggregate_id,
            actor="bunshin-manager", expected_version=revision.version,
            payload={"fencing_token": 1, "active_worker_id": "architect-worker"},
        ))
        answer = "Require a 32-bit length field.\n\n"
        appended = self.service.append_architect_clarification({
            "workflow_id": self.source_id, "architecture_revision_id": revision.aggregate_id,
            "worker_id": "architect-worker", "clarification_id": "clarification-first",
            "title": "Frame size", "question": "What length field is required?", "answer": answer,
        })
        self.assertIn("restart_cancelled_plan=true", self.service._unproven_interaction_recovery(self.source_id))

        args = BunshinCapabilitiesBunshinPublicProviderStartWorkflowInput(
            task=self.task_title, restart_cancelled_plan=True,
        ).model_dump()
        call = CapabilityCall(
            name="op_bunshin_start_workflow", args=args,
            meta={"actor_id": "nathan", "channel_id": "socket:test"},
        )
        self.provider.manager_request = lambda method, params: self.service.start_workflow(params)
        self.assertEqual(self.provider.start_workflow(call).status, RuntimeStatus.INVALID)

        self.service.control_workflow(
            workflow_id=self.source_id, command="cancel", actor="nathan", source_channel="test",
        )
        self._drive_until(
            lambda: self._snapshot(AggregateType.WORKFLOW, self.source_id).state == "CANCELLED",
            label="unapproved plan cancellation",
        )
        ready_to_publish = Barrier(2, timeout=20)
        put_json = self.service.artifacts.put_json

        def publish_together(payload, **kwargs):
            if kwargs.get("artifact_type") == "WorkflowRequestArtifact":
                ready_to_publish.wait()
            return put_json(payload, **kwargs)

        with patch.object(self.service.artifacts, "put_json", publish_together):
            with ThreadPoolExecutor(max_workers=2) as workers:
                reopened = list(workers.map(lambda _: self.provider.start_workflow(call), range(2)))
        self.assertCountEqual([result.status for result in reopened], [RuntimeStatus.OK, RuntimeStatus.INVALID])
        invalid = next(result for result in reopened if result.status == RuntimeStatus.INVALID)
        self.assertIn("Task already has an active workflow", invalid.llm_text)
        active = self.repository.search.task_workflow_candidates(
            actor_id="nathan", task_id="task_restart_import", include_terminal=False,
        )
        self.assertEqual(len(active), 1)
        self.assertNotEqual(active[0]["workflow_id"], self.source_id)
        replacement = self._snapshot(AggregateType.WORKFLOW, active[0]["workflow_id"])
        replacement_request = self.service.artifacts.read_json(replacement.payload["request_ref"])
        self.assertEqual(replacement_request["requirements_ref"], appended["requirements_ref"])
        ledger = self.service.artifacts.read_json(replacement_request["requirements_ref"])
        self.assertEqual(ledger["original"], self.service.artifacts.read_json(self.requirements_ref)["original"])
        self.assertEqual(ledger["revisions"][0]["authority"]["answer"], answer)

    def _drive_until(self, predicate, *, label):
        for _ in range(60):
            if predicate():
                return
            result = asyncio.run(self.processor.process_once(limit=1))
            with sqlite3.connect(self.repository.database.db_path) as connection:
                errors = connection.execute(
                    "SELECT effect_type, last_error FROM bunshin_outbox WHERE last_error != ''"
                ).fetchall()
            self.assertEqual(result["failed"], 0, f"{label}: {errors}")
            self.assertGreater(result["claimed"], 0, f"{label}: outbox became idle")
        self.fail(f"{label}: did not settle")

    def _human_ready(self, workflow_id):
        revision = self._revision(workflow_id)
        return bool(revision and revision.state == "HUMAN_REVIEW" and revision.payload.get("human_review_card_ref"))

    def _public_decision(self, decision, **kwargs):
        return self.provider.submit_human_decision(CapabilityCall(
            name="op_bunshin_submit_human_decision",
            meta={"actor_id": "nathan", "channel_id": "socket:test"},
            args={"task": self.task_title, "decision": decision, **kwargs},
        ))

    async def _run_profile(self, **kwargs):
        revision = kwargs["snapshot"]
        role = kwargs["activation"].role.value
        assignment_id = ""
        if role == "architect":
            workspace = dict(kwargs["workspace_override"])
            path = architect_path(workspace)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(self.contract, sort_keys=False), encoding="utf-8")
            if revision.workflow_id != self.source_id:
                self.assertIn("edit_instruction", kwargs["reference_refs"])
                baseline = self.service.artifacts.read_json(revision.payload["base_architecture_manifest_ref"])
                self.assertEqual(baseline["graph_ir"]["graph_id"], self.source_id)
            submission, assignment_id, prompt_ref = self._submit_role(kwargs, workspace)
            self.architect_submissions.append(submission)
            filename = "architect.yaml"
        elif role == "reviewer":
            self.reviewed_manifests.append(dict(revision.payload["architecture_manifest_ref"]))
            self.reviewed_workspaces.append(Path(kwargs["workspace_override"]["repo_path"]))
            self.assertTrue(Path(kwargs["workspace_override"]["repo_path"]).is_dir())
            self.assertIn("contract_diff", kwargs["reference_refs"])
            submission = {"verdict": "PASS", "findings": [], "advisories": [], "work_items": []}
            filename = "contract_review.json"
            submission, assignment_id, prompt_ref = self._submit_role(
                kwargs, dict(kwargs["workspace_override"]), submission=submission
            )
        else:
            self.fail(f"Unexpected live execution role: {role}")
        output = self.root / "role-results" / revision.aggregate_id / filename
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(submission), encoding="utf-8")
        terminal = {"payload": {
            "artifacts": [{"path": str(output), "relative_path": filename}],
            **({"role_assignment_id": assignment_id} if assignment_id else {}),
        }}
        terminal_ref = self.service.artifacts.put_json(terminal, artifact_type="RoleTerminalArtifact")
        workflow = self._snapshot(AggregateType.WORKFLOW, revision.workflow_id)
        self.repository.role_invocations.record_role_invocation(
            invocation_id=kwargs["invocation_id"], workflow_id=revision.workflow_id,
            aggregate_type=revision.aggregate_type, aggregate_id=revision.aggregate_id,
            lease_resource_key=kwargs["lease_resource"], fencing_token=kwargs["fencing_token"],
            role=role, mode=kwargs["activation"].mode.value, role_profile_id=kwargs["profile"],
            family_binding_sha=workflow.payload["family_binding_ref"]["sha256"],
            authoring_contract_version=AUTHORING_CONTRACT_VERSION,
            prompt_pack_ref=prompt_ref.to_dict(),
        )
        return terminal, prompt_ref, terminal_ref

    def _submit_role(self, kwargs, workspace, *, submission=None):
        """Use the actual assignment gateway, validation and GraphIR compiler."""
        revision = kwargs["snapshot"]
        workflow = self._snapshot(AggregateType.WORKFLOW, revision.workflow_id)
        binding_sha = workflow.payload["family_binding_ref"]["sha256"]
        session_id = kwargs["invocation_id"]
        role = kwargs["activation"].role.value
        mode = kwargs["activation"].mode.value
        kind = "contract" if role == "architect" else "architecture_review"
        self.repository.role_sessions.ensure_role_session(
            session_id=session_id, workflow_id=revision.workflow_id,
            aggregate_type=revision.aggregate_type, aggregate_id=revision.aggregate_id,
            role=role, mode=mode, role_profile_id=kwargs["profile"],
            family_binding_sha=binding_sha, scope_kind="architecture_cycle",
            subject_key=revision.payload["architecture_cycle_id"],
        )
        assignment = self.repository.role_assignments.create_role_assignment(RoleAssignmentRequest(
            assignment_key=f"{revision.aggregate_id}:{kind}", session_id=session_id,
            workflow_id=revision.workflow_id, aggregate_type=revision.aggregate_type.value,
            aggregate_id=revision.aggregate_id, role=role, mode=mode,
            role_profile_id=kwargs["profile"], family_binding_sha=binding_sha,
            input_fingerprint=f"{revision.aggregate_id}:input", required_inputs=(), input_refs={},
            execution_spec={"effect_type": kwargs["effect"]["effect_type"]}, submission_kind=kind,
        ))
        assignment_id = assignment["assignment_id"]
        attempt = self.repository.role_assignments.claim_role_assignment(assignment_id)
        attempt_id = attempt["attempt_id"]
        resource = f"assignment:{assignment_id}"
        fence = self.repository.leases.claim_lease(resource, attempt_id, ttl_seconds=120).fencing_token
        context = {
            "workflow_id": revision.workflow_id, "invocation_id": attempt_id,
            "lease_resource_key": resource, "fencing_token": fence,
            "role": role, "mode": mode, "draft_kind": kind,
            "input_fingerprint": f"{revision.aggregate_id}:input",
            "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
        }
        prompt_ref = self.service.artifacts.put_json({
            "workspace": {**workspace, "architect_path": str(architect_path(workspace))},
            "metadata": {"bunshin": {
                **{key: value for key, value in context.items() if key != "draft_kind"},
                "authoring_input_fingerprint": context["input_fingerprint"],
                "work_item_seed": [{"kind": "phase", "summary": "design", "status": "pending", "required": True}],
            }},
        }, artifact_type="RolePromptPackArtifact")
        self.repository.role_attempts.start_role_attempt(
            assignment_id=assignment_id, attempt_id_value=attempt_id,
            lease_resource_key=resource, fencing_token=fence, prompt_pack_ref=prompt_ref.to_dict(),
        )
        token = self.repository.role_access.issue_role_attempt_access_token(
            assignment_id=assignment_id, attempt_id_value=attempt_id, fencing_token=fence,
        )
        def call(method, **params):
            return self.gateway.call(method, {"access_token": token, **params})
        item = {"item_id": "phase:design", "kind": "phase", "status": "completed",
                "summary": "design", "ordinal": 0, "origin": "role_playbook", "required": True}
        call("draft_mutate", context={**context, "draft_kind": "work_items"},
             operation_key="complete-design", request={"step": "design"}, expected_version=0,
             next_payload={"items": [item]}, result={"updated": True},
             seed={"items": [{**item, "status": "pending"}]})
        call("draft_read", context=context, seed={})
        def submit():
            return call("draft_submit", context=context, expected_version=0,
                        submission=(submission if submission is not None else
                                    {"source": "architect.yaml", "architecture": self.contract}))
        if role == "architect" and revision.workflow_id == self.source_id:
            # Model the historical compiler only while producing the original
            # immutable artifact; restart and fresh submission use current code.
            compile_graph = GraphCompiler.compile
            def legacy_compile(compiler, *args, **options):
                graph = compile_graph(compiler, *args, **options)
                return replace(graph, edges=tuple(
                    replace(edge, kind=EdgeKind.CONTRACT) if edge.consumer != graph.sink else edge
                    for edge in graph.edges
                ))
            with patch.object(GraphCompiler, "compile", legacy_compile):
                receipt = submit()
        else:
            receipt = submit()
        self.repository.leases.release_lease(resource, attempt_id, fence)
        return self.service.artifacts.read_json(receipt["submission_artifact_ref"]), assignment_id, prompt_ref

    def _restart_to_imported(self):
        self._drive_until(lambda: self._human_ready(self.source_id), label="initial architecture")
        source_revision = self._revision(self.source_id)
        self.old_architecture_path = Path(source_revision.payload["architecture_workspace_path"])
        old_ref = source_revision.payload["architecture_manifest_ref"]
        old_record = self.repository.artifacts.read_artifact_record(old_ref["sha256"])
        old_bytes = Path(old_record["storage_path"]).read_bytes()
        old_manifest = self.service.artifacts.read_json(old_ref)
        old_graph = graph_ir_from_mapping(old_manifest["graph_ir"])
        self.assertEqual(
            next(edge for edge in old_graph.edges if edge.consumer == "consumer").kind,
            EdgeKind.CONTRACT,
        )
        accepted = self._public_decision("accept")
        self.assertEqual(accepted.status, RuntimeStatus.OK, accepted)
        self._drive_until(
            lambda: self.repository.cycles.read_graph_generation(graph_id=self.source_id) is not None,
            label="source execution compilation",
        )
        old_installed = self.repository.cycles.read_graph_generation(graph_id=self.source_id)

        restarted = self.provider.restart_execution(CapabilityCall(
            name="op_bunshin_restart_execution",
            meta={"actor_id": "nathan", "channel_id": "socket:test"},
            args={"task": self.task_title, "reason": "Recompile the legacy dependency graph through Human Edit."},
        ))
        self.assertEqual(restarted.status, RuntimeStatus.OK, restarted)
        self._drive_until(
            lambda: bool(self._snapshot(AggregateType.WORKFLOW, self.source_id).payload.get("replacement_workflow_id")),
            label="replacement creation",
        )
        source = self._snapshot(AggregateType.WORKFLOW, self.source_id)
        replacement_id = source.payload["replacement_workflow_id"]
        self.assertEqual(source.state, "CANCELLED")
        self._drive_until(lambda: self._revision(replacement_id) is not None, label="import routing")
        imported = self._revision(replacement_id)
        return old_ref, old_record, old_bytes, old_installed, replacement_id, imported

    def test_public_restart_import_review_edit_recompiles_fresh_software_graph(self):
        old_ref, old_record, old_bytes, old_installed, replacement_id, imported = self._restart_to_imported()
        self.assertTrue(self.import_rollback_checked)
        self.assertEqual(imported.state, "REVIEW_QUEUED")
        self.assertFalse(self.old_architecture_path.exists())
        self.assertEqual(imported.payload["architecture_manifest_ref"], old_ref)
        self.assertEqual(imported.payload["requirements_ref"], self.requirements_ref)
        import_effect = next(effect for effect in self.processed_effects
                             if effect["workflow_id"] == replacement_id
                             and effect["effect_type"] == "route_workflow")
        imported_cycle = self.repository.cycles.read_plan_cycle(workflow_id=replacement_id)
        self.assertEqual(imported_cycle.state, PlanCycleState.CHECKER_READY)
        self.assertEqual(imported_cycle.product_ref, old_ref["sha256"])
        self.assertIsNone(imported_cycle.active_assignment)
        self.assertEqual(self.processor._route_workflow(import_effect), {"status": "import_replayed"})
        self.assertEqual(self.repository.cycles.read_plan_cycle(workflow_id=replacement_id), imported_cycle)
        self.assertEqual(Path(old_record["storage_path"]).read_bytes(), old_bytes)
        self.assertIsNone(self.repository.cycles.read_graph_generation(graph_id=replacement_id))

        self._drive_until(lambda: self._human_ready(replacement_id), label="imported architecture review")
        cycle = self.repository.cycles.read_plan_cycle(workflow_id=replacement_id)
        self.assertEqual(cycle.state, PlanCycleState.HUMAN_REVIEW)
        self.assertEqual(cycle.product_ref, old_ref["sha256"])
        self.assertEqual(len(self.architect_submissions), 1)
        self.assertEqual(self.reviewed_manifests[-1], old_ref)
        self.assertNotEqual(self.reviewed_workspaces[-1], self.old_architecture_path)
        self.assertFalse(self.reviewed_workspaces[-1].exists())
        # The imported graph still belongs to the old workflow. Acceptance must
        # fail before consuming the human token or installing that historical IR.
        rejected = self._public_decision("accept")
        self.assertNotEqual(rejected.status, RuntimeStatus.OK)
        self.assertTrue(self._human_ready(replacement_id))
        self.assertEqual(self.repository.cycles.read_plan_cycle(workflow_id=replacement_id), cycle)
        self.assertIsNone(self.repository.cycles.read_graph_generation(graph_id=replacement_id))
        edited = self._public_decision("edit", edit_instruction="Re-submit the unchanged contract with current dependency compilation.")
        self.assertEqual(edited.status, RuntimeStatus.OK, edited)
        self._drive_until(lambda: self._human_ready(replacement_id), label="fresh architecture submission and review")
        fresh_revision = self._revision(replacement_id)
        self.assertNotEqual(fresh_revision.aggregate_id, imported.aggregate_id)
        fresh_ref = fresh_revision.payload["architecture_manifest_ref"]
        self.assertNotEqual(fresh_ref["sha256"], old_ref["sha256"])
        fresh_manifest = self.service.artifacts.read_json(fresh_ref)
        fresh_graph = graph_ir_from_mapping(fresh_manifest["graph_ir"])
        self.assertEqual(fresh_manifest["contract"], self.service.artifacts.read_json(old_ref)["contract"])
        self.assertNotEqual(Path(fresh_revision.payload["architecture_workspace_path"]), self.old_architecture_path)
        self.assertEqual(fresh_graph.graph_id, replacement_id)
        self.assertEqual(fresh_graph.generation, 1)
        self.assertEqual(self.repository.cycles.read_plan_cycle(workflow_id=replacement_id).generation, 2)
        self.assertTrue(all(edge.kind == EdgeKind.EXECUTION for edge in fresh_graph.edges))
        self.assertEqual(len(self.architect_submissions), 2)
        # A delayed replay of the original route must never relink the old
        # imported revision or roll the fresh human-review cycle backward.
        fresh_cycle = self.repository.cycles.read_plan_cycle(workflow_id=replacement_id)
        self.assertEqual(self.processor._route_workflow(import_effect), {"status": "import_replayed"})
        self.assertEqual(self._revision(replacement_id).aggregate_id, fresh_revision.aggregate_id)
        self.assertEqual(self.repository.cycles.read_plan_cycle(workflow_id=replacement_id), fresh_cycle)
        self.assertEqual(self.reviewed_manifests[-1], fresh_ref)
        self.assertIsNone(self.repository.cycles.read_graph_generation(graph_id=replacement_id))
        self.assertEqual(Path(old_record["storage_path"]).read_bytes(), old_bytes)
        self.assertEqual(self.repository.cycles.read_graph_generation(graph_id=self.source_id), old_installed)

        accepted = self._public_decision("accept")
        self.assertEqual(accepted.status, RuntimeStatus.OK, accepted)
        self._drive_until(
            lambda: self.repository.cycles.read_graph_generation(graph_id=replacement_id) is not None,
            label="fresh execution compilation",
        )
        self.assertEqual(self.repository.cycles.read_graph_generation(graph_id=replacement_id), fresh_graph)
        nodes = [item for item in self.repository.queries.list_workflow_snapshots(replacement_id)
                 if item.aggregate_type == AggregateType.DAG_NODE_RUN]
        self.assertTrue(nodes)
        self.assertTrue(all(item.state != "ACCEPTED" for item in nodes))
        self.assertTrue(all(not item.payload.get("candidate_artifact_ref") for item in nodes))
        self.assertEqual(Path(old_record["storage_path"]).read_bytes(), old_bytes)


    def test_public_triage_recovers_legacy_import_after_eight_checker_failures(self):
        self.legacy_import = True
        # Reproduce the old manager behavior, rather than writing snapshots or
        # seeding cycle states. The old import omitted its product transition.
        with patch("pal.bunshin.imported_plan.bind_imported_plan_product", return_value=False):
            old_ref, old_record, old_bytes, old_installed, replacement_id, imported = self._restart_to_imported()
            self.assertIsNone(self.repository.cycles.read_plan_cycle(workflow_id=replacement_id))
            failures = 0
            now = datetime.now(timezone.utc)
            with (
                patch("pal.bunshin.storage.outbox_claims._utc_datetime", side_effect=lambda: now),
                patch("pal.bunshin.storage.outbox_results._utc_datetime", side_effect=lambda: now),
            ):
                for _ in range(30):
                    now += timedelta(seconds=6)
                    result = asyncio.run(self.processor.process_once(limit=1))
                    failures += result["failed"]
                    current = self._revision(replacement_id)
                    if current.state == "TRIAGE_REQUIRED":
                        break
                    self.assertGreater(result["claimed"], 0)
                else:
                    self.fail("legacy reviewer retries did not reach triage")
            self.assertEqual(failures, 8)
            with sqlite3.connect(self.repository.database.db_path) as connection:
                failed = connection.execute(
                    "SELECT attempt_count, last_error FROM bunshin_outbox "
                    "WHERE workflow_id = ? AND effect_type = 'run_reviewer_role' AND status = 'failed'",
                    (replacement_id,),
                ).fetchone()
            self.assertEqual(failed[0], 8)
            self.assertIn("CycleTransitionError", failed[1])
            self.assertIn("cannot start_checker from PRODUCER_READY", failed[1])
            self.assertEqual(current.payload["architecture_manifest_ref"], old_ref)
            self.assertEqual(current.payload["triage_resume_state"], "REVIEW_QUEUED")

        # Only the supported triage API may recover the now-fixed import. No
        # SQL update, cycle write, or synthetic producer submission repairs it.
        result = self.service.resolve_triage(
            workflow_id=replacement_id, actor="nathan", source_channel="test",
            subject="phase:architecture", resolution="The imported-plan cycle binding is fixed.",
        )
        self.assertEqual(result["state"], "REVIEW_QUEUED")
        self._drive_until(lambda: self._human_ready(replacement_id), label="legacy imported review recovery")
        cycle = self.repository.cycles.read_plan_cycle(workflow_id=replacement_id)
        self.assertEqual(cycle.state, PlanCycleState.HUMAN_REVIEW)
        self.assertEqual(cycle.product_ref, old_ref["sha256"])
        self.assertEqual(cycle.generation, 1)
        self.assertEqual(len(self.architect_submissions), 1)
        self.assertEqual(self.reviewed_manifests[-1], old_ref)
        self.assertNotEqual(self.reviewed_workspaces[-1], self.old_architecture_path)
        self.assertFalse(self.reviewed_workspaces[-1].exists())
        self.assertEqual(Path(old_record["storage_path"]).read_bytes(), old_bytes)
        self.assertEqual(self.repository.cycles.read_graph_generation(graph_id=self.source_id), old_installed)
        self.assertIsNone(self.repository.cycles.read_graph_generation(graph_id=replacement_id))


    def test_authored_revision_cannot_skip_producer_through_import_recovery(self):
        from pal.bunshin.imported_plan import bind_imported_plan_product
        self._drive_until(lambda: self._revision(self.source_id) is not None, label="new requirement routing")
        revision = self._revision(self.source_id)
        self.assertEqual(revision.state, "ARCHITECT_QUEUED")
        with self.repository.transaction() as transaction:
            self.assertFalse(bind_imported_plan_product(
                repository=self.repository, artifacts=self.service.artifacts,
                revision=revision, unit_of_work=transaction,
            ))
        with self.assertRaisesRegex(CycleTransitionError, "cannot start_checker from PRODUCER_READY"):
            WorkflowCoordinator(self.repository).start_plan_assignment(
                workflow_id=self.source_id, slot=CycleSlot.CHECKER,
                kind=AssignmentKind.INITIAL, input_fingerprint="unauthorized-checker-skip",
            )
        cycle = self.repository.cycles.read_plan_cycle(workflow_id=self.source_id)
        self.assertEqual(cycle.state, PlanCycleState.PRODUCER_READY)
        self.assertFalse(cycle.product_ref)
        self.assertIsNone(cycle.active_assignment)


def test_cancelled_plan_recovers_frozen_input_into_new_architecture_workspace(tmp_path, monkeypatch):
    import shutil
    import subprocess
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from pal.bunshin.artifacts import ArtifactRef
    from pal.bunshin.contract_protocol import software_contract_projection, validate_contract_payload
    from pal.bunshin.sandbox import build_sandboxed_runner_invocation, with_bunshin_sandbox_metadata
    from pal.bunshin.semantic_orchestration.attempt_inputs import AttemptInputs
    from pal.bunshin.workflow_control import reconcile_control_requests
    from pal.bunshin.workspace_git import _git
    from pal.shared import BunshinInvocationPack

    source = tmp_path / "source"
    source.mkdir()
    manager_cwd = tmp_path / "manager"
    manager_cwd.mkdir()
    (manager_cwd / "facts.txt").write_text("unrelated manager content\n", encoding="utf-8")
    (manager_cwd / "notes.txt").write_text("unrelated manager notes\n", encoding="utf-8")
    extra_reference = tmp_path / "extra.txt"
    extra_reference.write_text("ordinary external reference\n", encoding="utf-8")
    monkeypatch.chdir(manager_cwd)
    _git(source, "init")
    input_path = source / "facts.txt"
    input_path.write_text("original evidence\n", encoding="utf-8")
    (source / "notes.txt").write_text("original notes\n", encoding="utf-8")
    previous_target = source / "inputs/facts/facts.txt"
    previous_target.parent.mkdir(parents=True)
    previous_target.write_text("tracked product content\n", encoding="utf-8")
    (source / "include").mkdir()
    for header in ("decoder.hpp", "application.hpp"):
        (source / "include" / header).write_text("#pragma once\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Baseline")
    service = BunshinWorkflowService(tmp_path / "runtime ? inputs")
    original = service.start_workflow({
        "title": "Recover planned input", "actor": "nathan", "goal": "Use the declared facts",
        "profile": "software_engineering.v2_coder",
        "workspace": {"kind": "existing_repo", "repo_path": str(source)},
        "task_spec": {"objective": "Use the declared facts"},
        "references": [
            {"name": "facts", "path": "facts.txt", "repo_relative": True},
            {"name": "notes", "path": "notes.txt"},
            {"name": "extra", "path": str(extra_reference)},
        ],
        "delivery_binding": {
            "channel_id": "test", "channel_kind": "socket", "reply_target": {"session_id": "test"},
        },
    })
    source_id = original["workflow_id"]
    service.control_workflow(workflow_id=source_id, command="cancel", actor="nathan", source_channel="test")
    reconcile_control_requests(service.repository, source_id)
    input_path.unlink()
    reopened = service.start_workflow({
        "task_id": original["task_id"], "actor": "nathan", "restart_cancelled_plan": True,
    })
    workflow = service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, reopened["workflow_id"])
    request = service.artifacts.read_json(reopened["request_ref"])
    prior = service.artifacts.read_json(original["request_ref"])
    original_input = service.artifacts.read_json(prior["input_binding_ref"])
    rebound_input = service.artifacts.read_json(request["input_binding_ref"])
    assert rebound_input["workflow_id"] == workflow.workflow_id
    assert rebound_input["inputs"] == original_input["inputs"]
    assert request["requirements_ref"] == prior["requirements_ref"]

    input_path.write_text("new working tree content\n", encoding="utf-8")
    architecture = service.skeleton.provision_architecture_workspace(
        workflow_id=workflow.workflow_id, revision_name="first", workspace=request["workspace"],
        requirements_ref=ArtifactRef.from_mapping(request["requirements_ref"]),
    )
    revision = service.repository.transitions.dispatch(ActionEnvelope(
        action_type="CREATE_ARCHITECTURE_REVISION", workflow_id=workflow.workflow_id,
        aggregate_type=AggregateType.ARCHITECTURE_REVISION, aggregate_id="recovery-reference-review",
        actor="test", expected_version=0, payload={"requirements_ref": request["requirements_ref"]},
    )).snapshot
    stage = SemanticOrchestrator(service).components.architecture_stage
    _, data_refs = stage.architecture_stage_prompt("architect", revision)
    assert "user_facts" not in data_refs
    assert "user_notes" not in data_refs
    assert data_refs["user_extra"].media_type == str(extra_reference)
    author = stage.architecture_authoring
    with patch.object(author.attempt_execution, "run_profile", AsyncMock(return_value=(None, None, None))) as run_profile:
        asyncio.run(author.run_architect_with_bound_inputs(
            architecture, None, {}, None, "test-run", SimpleNamespace(fencing_token=1), "test-lease",
            request, ArtifactRef.from_mapping(request["requirements_ref"]), revision, None, None, None,
        ))
    git_refs = run_profile.call_args.kwargs["reference_refs"]
    assert "user_facts" not in git_refs
    assert "user_notes" not in git_refs
    assert git_refs["user_extra"].media_type == str(extra_reference)
    entries = AttemptInputs(service.artifacts).bind_role_attempt_inputs(
        workflow=workflow, request=request, workspace={"repo_path": str(architecture.worktree)},
        snapshot=workflow, workspace_source_root=str(source),
    )
    assert (Path(entries[0]["path"]) / "facts.txt").read_bytes() == b"original evidence\n"
    assert (Path(entries[1]["path"]) / "notes.txt").read_bytes() == b"original notes\n"
    assert _git(architecture.worktree, "status", "--porcelain") == ""
    assert architecture.base_sha != architecture.original_head
    assert _git(architecture.worktree, "cat-file", "-t", architecture.original_head).strip() == "commit"
    assert (architecture.worktree / previous_target.relative_to(source)).read_bytes() == b"tracked product content\n"
    assert (architecture.worktree / "facts.txt").read_bytes() == b"new working tree content\n"
    pack = with_bunshin_sandbox_metadata(
        service.runtime_root,
        BunshinInvocationPack(invocation_id="recover-bound-input", workspace={
            "repo_path": str(architecture.worktree), "reference_paths": entries,
        }),
        run_id="recover-bound-input",
    )
    assert pack.workspace["reference_paths"][0]["path"] == "/pal/references/frozen_input_1/facts.txt"
    assert pack.metadata["sandbox"]["reference_binds"][0]["include"] == ["facts.txt"]

    header = architecture.worktree / "include/decoder.hpp"
    if shutil.which("bwrap"):
        command = """
set -eu
test "$(cat /pal/references/frozen_input_1/facts.txt)" = 'original evidence'
if (printf changed > /pal/references/frozen_input_1/facts.txt) 2>/dev/null; then exit 31; fi
printf '#pragma once\nclass Decoder;\n' > include/decoder.hpp
"""
        argv, env = build_sandboxed_runner_invocation(
            runtime_root=service.runtime_root, pack=pack, argv=["/bin/sh", "-c", command],
            env={"PATH": "/usr/bin:/bin"}, role_runtime=False,
        )
        sandbox = subprocess.run(argv, env=env, cwd=architecture.worktree, capture_output=True, text=True, timeout=20)
        assert sandbox.returncode == 0, sandbox.stderr
    else:
        header.write_text("#pragma once\nclass Decoder;\n", encoding="utf-8")
    definition = ArchitectureTemplateCompiler().compile("software_engineering.v1")
    document = validate_contract_payload(definition.example, definition=definition)
    artifact = service.skeleton.snapshot_architect_result(
        workflow_name="Recovery", revision_name="first", architecture_workspace=architecture,
        submission=software_contract_projection(document.model_dump(mode="python")),
        requirements_ref=ArtifactRef.from_mapping(request["requirements_ref"]),
    )
    skeleton_commit = service.artifacts.read_json(artifact)["skeleton_commit_sha"]
    assert _git(architecture.worktree, "diff", "--name-only", architecture.base_sha, skeleton_commit).splitlines() == [
        "include/decoder.hpp",
    ]


def test_planned_execution_restart_reuses_frozen_input_after_source_is_removed():
    from pal.bunshin.workspace_git import _git

    case = BunshinRestartImportLifecycleTests()
    start_workflow = BunshinWorkflowService.start_workflow

    def start_with_reference(service, request, **kwargs):
        if request.get("workflow_id") == case.source_id:
            _git(case.repo_path, "init")
            (case.repo_path / "facts.txt").write_text("approved planning input", encoding="utf-8")
            _git(case.repo_path, "add", "-A")
            _git(case.repo_path, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Baseline")
            request = {**request, "references": [{"name": "facts", "path": "facts.txt"}]}
        return start_workflow(service, request, **kwargs)

    try:
        with patch.object(BunshinWorkflowService, "start_workflow", start_with_reference):
            case.setUp()
        case._drive_until(lambda: case._human_ready(case.source_id), label="initial architecture")
        accepted = case._public_decision("accept")
        assert accepted.status == RuntimeStatus.OK
        case._drive_until(
            lambda: case.repository.cycles.read_graph_generation(graph_id=case.source_id) is not None,
            label="source execution compilation",
        )
        source = case._snapshot(AggregateType.WORKFLOW, case.source_id)
        original_inputs = case.service.artifacts.read_json(source.payload["input_binding_ref"])["inputs"]
        (case.repo_path / "facts.txt").unlink()
        restart = case.provider.restart_execution(CapabilityCall(
            name="op_bunshin_restart_execution", meta={"actor_id": "nathan"},
            args={"task": case.task_title, "reason": "Retry from the approved plan"},
        ))
        assert restart.status == RuntimeStatus.OK
        case._drive_until(
            lambda: bool(case._snapshot(AggregateType.WORKFLOW, case.source_id).payload.get("replacement_workflow_id")),
            label="replacement creation without host input",
        )
        source = case._snapshot(AggregateType.WORKFLOW, case.source_id)
        replacement = case._snapshot(AggregateType.WORKFLOW, source.payload["replacement_workflow_id"])
        adopted = case.service.artifacts.read_json(replacement.payload["input_binding_ref"])
        assert adopted["workflow_id"] == replacement.workflow_id
        assert adopted["inputs"] == original_inputs
        assert case.service.artifacts.read_bytes(adopted["inputs"][0]["content_ref"]) == b"approved planning input"
    finally:
        case.doCleanups()
