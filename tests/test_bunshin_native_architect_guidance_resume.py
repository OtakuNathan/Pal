"""A resumed native Architect consumes current guidance with its pinned profile."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pal.bunshin.runner import BunshinRunner
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.submission_drafts import AUTHORING_CONTRACT_VERSION
from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.shared import BunshinInvocationPack
from tests.test_bunshin_completion_resume import SyntheticModel, make_bundle, noop
from tests.test_bunshin_native_architect_guidance import pack


class CapturingModel(SyntheticModel):
    def __init__(self):
        super().__init__()
        self.requests = []

    def generate(self, request, **options):
        self.requests.append(request)
        return super().generate(request, **options)


def runner(root, value):
    return BunshinRunner(
        runtime_root=root, pack=value, bunshin_id=value.invocation_id,
        run_id="native-architect-attempt", write_event=noop, read_decision=noop,
    )


@pytest.mark.parametrize("response_key", ["original-assignment", "triage-resume-assignment"])
def test_restored_native_loop_consumes_new_contract_without_rewriting_profile(tmp_path, response_key):
    async def scenario():
        repository = BunshinRepository(tmp_path)
        lease = repository.leases.claim_lease("architect-guidance", "native-attempt", ttl_seconds=60)
        value = pack(tmp_path)
        # Preserve coverage for a checkpoint pinned before adapter removal.
        value.resolved_profile["output_contract_fragment"] = (
            "Manager validates and records the bound files after the harness finishes."
        )
        value.metadata["bunshin_v2"].update({
            "workflow_id": "architect-workflow", "invocation_id": "native-attempt",
            "lease_resource_key": lease.resource_key, "fencing_token": lease.fencing_token,
            "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
        })
        value.workspace["bunshin_v2"] = copy.deepcopy(value.metadata["bunshin_v2"])
        restore_path = tmp_path / "before-fix-checkpoint.json"
        value.metadata["agent_session"] = {
            "session_id": value.invocation_id, "workflow_id": "architect-workflow",
            "stage_key": "architecture_cycle:architecture-1:architect",
            "response_key": "original-assignment", "fencing_token": 1,
            "harness_id": "pal", "harness_generation": "pinned-pal-generation",
            "continuation_output_path": str(restore_path),
        }
        original_profile = copy.deepcopy(value.resolved_profile)
        assert "after the harness finishes" in original_profile["output_contract_fragment"]

        # Seed a real encrypted checkpoint with completed prior work. No prompt
        # is assembled here: its pinned profile still contains the old wording.
        prior = runner(tmp_path, value)
        bundle = make_bundle()
        try:
            memory = bundle.memory_service
            memory.begin_l1_turn("prior-architect-turn", user_text=value.instruction)
            memory.upsert_l1_assistant("prior-architect-turn", LLMMessageIR(
                role=MessageRole.ASSISTANT,
                parts=(TextPartIR("The declaration files are already authored."),),
            ))
            memory.settle_l1_turn("prior-architect-turn")
            await prior.components.session_checkpoints.persist_agent_session_checkpoint(
                bundle,
                SimpleNamespace(
                    llm_round_count=11, tool_call_count=7, memory_service=memory,
                    output_length_recovery_count=0, pending_output_length_recovery_note="",
                    memory_candidate_sink=SimpleNamespace(records=[]),
                ),
                SimpleNamespace(
                    opening_event=SimpleNamespace(event_id="original-assignment"),
                    pending_tool_call_batch=[], pending_tool_results=[], tool_batch_count=0,
                    preferred_llm_endpoint_id="", preferred_llm_model_id="",
                ),
                initial_instruction=value.instruction, response_keys=["original-assignment"],
            )
            old_checkpoint = prior.components.session_checkpoints.agent_session_checkpoint
        finally:
            bundle.execution_runtime.shutdown()

        # A new process shell restores the same logical session. Triage may
        # supply a new assignment key; an ordinary process retry keeps the key.
        resumed_value = BunshinInvocationPack.from_dict(copy.deepcopy(value.to_dict()))
        resumed_value.metadata["agent_session"].update({
            "response_key": response_key, "fencing_token": 2,
            "continuation_input_path": str(restore_path),
            "continuation_output_path": str(tmp_path / "resumed-checkpoint.json"),
        })
        pinned_pack = copy.deepcopy(resumed_value.to_dict())
        resumed = runner(tmp_path, resumed_value)
        model = CapturingModel()
        fresh_bundle = replace(make_bundle(), llm_runtime=model)
        try:
            assert not fresh_bundle.memory_service.history.turns
            await resumed.components.agent_session.run_agent_loop(fresh_bundle)
            assert model.calls == 1
            developer = "\n".join(
                part.text for message in model.requests[0].messages
                if message.role == MessageRole.DEVELOPER
                for part in message.parts if isinstance(part, TextPartIR)
            )
            assert "after the harness finishes" not in developer
            assert "call submit_contract with an empty argument object ({})" in developer
            assert "durable submission receipt" in developer
            assert "A final response does not submit the files" in developer
            assert "The declaration files are already authored." in str(fresh_bundle.memory_service.history.turns)
            checkpoint = resumed.components.session_checkpoints.agent_session_checkpoint
            assert checkpoint["logical_coroutine_id"] == old_checkpoint["logical_coroutine_id"]
            assert checkpoint["runtime_spec_hash"] == old_checkpoint["runtime_spec_hash"]
            assert checkpoint["coroutine_state"]["llm_round_count"] == 12
            assert checkpoint["sequence"] > old_checkpoint["sequence"]
            assert resumed_value.to_dict() == pinned_pack
            assert resumed_value.resolved_profile == original_profile
        finally:
            fresh_bundle.execution_runtime.shutdown()

    asyncio.run(scenario())
