"""Canonical imports retain durable checkpoint and harness identities."""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import pkgutil
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import pal.bunshin
from pal.bunshin.checkpoint import AgentSessionCheckpointError
from pal.bunshin.harnesses import BunshinHarnessRegistry, pal_harness_spec
from pal.bunshin.runner import BunshinRunner
from pal.bunshin.semantic_orchestration.attempt_harness_binding import HarnessBinding
from pal.bunshin.semantic_orchestration.role_checkpoints import RoleCheckpoints
from pal.bunshin.submission_drafts import SubmissionDraftContext
from pal.bunshin.verifier_tool_diagnostics import (
    capture_verifier_failure,
    record_verifier_failure,
)
from pal.shared import BunshinInvocationPack
from tests.test_bunshin_completion_resume import make_bundle, noop
from tests import test_bunshin_v2_role_protocol as protocol_fixture


def test_canonical_package_has_all_exports_and_no_parallel_implementation():
    assert not (Path(pal.bunshin.__file__).parent / "v2").exists()
    assert importlib.util.find_spec("pal.bunshin.v2") is None
    assert len(pal.bunshin.__all__) == len(set(pal.bunshin.__all__)) == 27
    for name in pal.bunshin.__all__:
        assert getattr(pal.bunshin, name) is not None
    # Includes storage, orchestration components, deferred worker imports, and
    # the three modules whose destination names resolve package collisions.
    modules = tuple(pkgutil.walk_packages(pal.bunshin.__path__, "pal.bunshin."))
    names = {module.name for module in modules}
    assert {"pal.bunshin.workflow_catalog", "pal.bunshin.workflow_capabilities",
            "pal.bunshin.architecture_compilation", "pal.bunshin.worker_main"} <= names
    for module in modules:
        assert ".v2" not in module.name
        importlib.import_module(module.name)


def test_relocated_verifier_validation_retains_real_failure_provenance():
    with capture_verifier_failure(enabled=True) as capture:
        try:
            SubmissionDraftContext.from_workspace({}, draft_kind="work_items")
        except ValueError as exc:
            record_verifier_failure(exc)
        else:
            pytest.fail("Invalid workspace unexpectedly passed validation")
    assert capture.provenance.error_type == "ValueError"
    assert capture.provenance.frames[-1].file == "submission_drafts.py"
    assert capture.provenance.frames[-1].function == "SubmissionDraftContext.from_workspace"


@pytest.fixture
def role():
    fixture = protocol_fixture.BunshinV2RoleProtocolTests()
    fixture.setUp()
    try:
        yield fixture
    finally:
        shutil.rmtree(fixture.runtime_root)


def legacy_pal_generation():
    current = pal_harness_spec()
    # Reconstruct the old launcher identity, without an old module or shim.
    old = replace(current, worker_argv=(*current.worker_argv[:-1], "pal.bunshin.v2.worker_main"), provider_generation="")
    registry = BunshinHarnessRegistry()
    registry.register(old)
    return registry.snapshot()


def runner_for(root, pack):
    return BunshinRunner(runtime_root=root, pack=pack, bunshin_id=pack.invocation_id,
                         run_id="migration-attempt", write_event=noop, read_decision=noop)


@pytest.mark.parametrize("completed", [False, True], ids=["session-generation", "completed-attempt-generation"])
def test_same_pal_harness_pins_old_generation_and_restores_checkpoint(role, completed):
    async def scenario():
        old = legacy_pal_generation()
        current = BunshinHarnessRegistry(include_pal=True).snapshot()
        assert current.select("implementation").worker_argv[-1] == "pal.bunshin.worker_main"
        assert old.generation_hash != current.generation_hash
        assignment = role.repository.role_assignments.create_role_assignment(role.request())
        attempt = role.repository.role_assignments.claim_role_assignment(
            assignment["assignment_id"], harness_id="pal", harness_generation=old.generation_hash)
        lease = role.repository.leases.claim_lease(
            f"assignment:{assignment['assignment_id']}", attempt["attempt_id"], ttl_seconds=120)
        role.repository.role_attempts.start_role_attempt(
            assignment_id=assignment["assignment_id"], attempt_id_value=attempt["attempt_id"],
            lease_resource_key=f"assignment:{assignment['assignment_id']}", fencing_token=lease.fencing_token,
            prompt_pack_ref=role.prompt_ref.to_dict())
        if completed:
            receipt = role.repository.role_submissions.record_role_submission(
                assignment_id=assignment["assignment_id"], attempt_id_value=attempt["attempt_id"],
                fencing_token=lease.fencing_token, artifact_ref=role.submission_ref.to_dict(),
                payload_hash="migration-submission", settlement_action={"action_type": "SUBMIT_CANDIDATE"})
            role.repository.role_submissions.settle_role_assignment(
                assignment_id=assignment["assignment_id"], submission_payload_hash=receipt.payload_hash)
            # A later shell can advertise the current registry, but its prior
            # completed shell still owns the existing checkpoint identity.
            assignment = role.repository.role_assignments.create_role_assignment(role.request(key="next-shell"))
            role.repository.role_assignments.claim_role_assignment(
                assignment["assignment_id"], harness_id="pal", harness_generation=current.generation_hash)
        assignment = role.repository.role_assignments.read_role_assignment(assignment["assignment_id"])
        session = role.repository.role_sessions.read_role_session("session-router")
        assert session["preferred_harness_generation"] == (current.generation_hash if completed else old.generation_hash)
        pack = BunshinInvocationPack(invocation_id="session-router", instruction="Synthetic migration checkpoint",
            workspace={"run_dir": str(role.runtime_root)}, metadata={"agent_session": {
                "session_id": "session-router", "workflow_id": "workflow-router",
                "stage_key": "module:router:implementation", "fencing_token": 1,
                "harness_id": "pal", "harness_generation": old.generation_hash,
                "continuation_output_path": str(role.runtime_root / "old-checkpoint.json"),
            }})
        checkpoints = RoleCheckpoints(role.artifacts, None, role.repository, role.runtime_root)
        bound = await HarnessBinding(role.artifacts, role.repository, checkpoints).execute(
            SimpleNamespace(invocation_id="session-router"), SimpleNamespace(pack=pack),
            SimpleNamespace(assignment=assignment, durable_prompt_reused=False, role_session=session),
            SimpleNamespace(harness_generation=current, role="implementation"))
        assert bound.effective_harness_generation == old.generation_hash
        assert bound.harness_spec.worker_argv[-1] == "pal.bunshin.worker_main"
        bundle = make_bundle()
        try:
            runner = runner_for(role.runtime_root, pack)
            await runner.components.session_checkpoints.persist_agent_session_checkpoint(
                bundle, SimpleNamespace(llm_round_count=39, tool_call_count=66,
                    memory_service=bundle.memory_service, memory_candidate_sink=SimpleNamespace(records=[])),
                SimpleNamespace(pending_tool_call_batch=[], pending_tool_results=[], tool_batch_count=0,
                    preferred_llm_endpoint_id="", preferred_llm_model_id=""),
                initial_instruction=pack.instruction, response_keys=[assignment["assignment_id"]])
            original = runner.components.session_checkpoints.agent_session_checkpoint
        finally:
            bundle.execution_runtime.shutdown()
        checkpoint_path = role.runtime_root / "old-checkpoint.json"
        encrypted_bytes = checkpoint_path.read_bytes()
        assert json.loads(encrypted_bytes)["schema_version"] == "8"
        pack.metadata["agent_session"].update({
            "continuation_input_path": str(checkpoint_path), "fencing_token": 2,
            "harness_generation": bound.effective_harness_generation,
            "continuation_output_path": str(role.runtime_root / "next-checkpoint.json"),
        })
        for generation, accepted in ((bound.effective_harness_generation, True), (current.generation_hash, False)):
            pack.metadata["agent_session"]["harness_generation"] = generation
            bundle = make_bundle()
            try:
                fresh = runner_for(role.runtime_root, pack)
                if accepted:
                    restored = await fresh.components.agent_session.restore_runtime(bundle, "session-router", pack.workspace)
                    assert restored == original
                    assert restored["coroutine_state"]["llm_round_count"] == 39
                    assert restored["runtime_spec_hash"] == original["runtime_spec_hash"]
                else:
                    with pytest.raises(AgentSessionCheckpointError, match="runtime specification"):
                        await fresh.components.agent_session.restore_runtime(bundle, "session-router", pack.workspace)
                    assert not fresh.components.session_checkpoints.agent_session_checkpoint
            finally:
                bundle.execution_runtime.shutdown()
        assert checkpoint_path.read_bytes() == encrypted_bytes

    asyncio.run(scenario())
