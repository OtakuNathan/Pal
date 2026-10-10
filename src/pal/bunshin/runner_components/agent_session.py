from __future__ import annotations
from pal.bunshin.runner_components.prompt_values import bunshin_output_length_recovery_note
from pal.bunshin.runner_components.models import BunshinRuntimeBundle
from pal.bunshin.runner_components.models import BunshinAgentLoopState
from pal.bunshin.runner_components.llm_settings import _resolve_bunshin_max_output_tokens
from pal.bunshin.runner_components.llm_settings import _bunshin_turn_settings_snapshot
from pal.bunshin.runner_components.prompt_values import _bunshin_prompt_context
from pal.bunshin.runner_components.prompt_values import _BUNSHIN_TOOL_RESULT_RETENTION_CALLS
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from pal.core.runtime_state import RuntimeSnapshotIdentity
from pal.core.turns import AgentLoopFrame, EffectResult, L1CommitPayload, TurnContinuation, TurnOutcome, agent_turn_program
from pal.foundation import EventEnvelope
from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.memory import L1MessageKind, L1TranscriptMessage
from pal.bunshin.checkpoint import AgentSessionCheckpointError
from pal.bunshin.scoped_execution import BunshinScopedExecutionRuntime
from pal.bunshin.prompt_adapter import build_bunshin_task_envelope as _bunshin_task_envelope, bunshin_primary_input as _bunshin_primary_input
from pal.shared import ChannelEnvelope, EventKind, SourceKind, TurnDeliveryBinding, BunshinInvocationPack
from pal.bunshin.runner_components.agent_runtime import AgentRuntime
from pal.bunshin.runner_components.artifacts import Artifacts
from pal.bunshin.runner_components.control import Control
from pal.bunshin.runner_components.llm_rounds import LlmRounds
from pal.bunshin.runner_components.memory_results import MemoryResults
from pal.bunshin.runner_components.reporter import Reporter
from pal.bunshin.runner_components.review_evidence import ReviewEvidence
from pal.bunshin.runner_components.session_checkpoints import SessionCheckpoints
from pal.bunshin.runner_components.session_memory import SessionMemory
from pal.bunshin.runner_components.status import Status
from pal.bunshin.runner_components.tool_session import ToolSession


@dataclass
class AgentSession:
    agent_runtime: AgentRuntime
    artifacts: Artifacts
    control: Control
    llm_rounds: LlmRounds
    memory_results: MemoryResults
    reporter: Reporter
    review_evidence: ReviewEvidence
    session_checkpoints: SessionCheckpoints
    session_memory: SessionMemory
    status: Status
    tool_session: ToolSession
    bunshin_id: str
    pack: BunshinInvocationPack
    run_id: str
    runtime_root: Path
    runtime_initialized: bool = field(default=False, init=False, repr=False)
    completed_loop_state: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    completed_loop_binding: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    completed_loop_bundle: BunshinRuntimeBundle | None = field(default=None, init=False, repr=False)

    async def run_agent_loop(self, bundle: BunshinRuntimeBundle, *, forced_retry_note: str = "") -> str:
        if self.tool_session.execution_sessions is None:
            self.tool_session.bind_execution(bundle.execution_runtime.create_role_session_driver())
        memory_service = bundle.memory_service
        memory_candidate_sink = self.memory_results.runner_memory_candidate_sink()
        workspace = self.build_workspace()
        session_metadata = dict((self.pack.metadata or {}).get("agent_session") or {})
        session_id = str(session_metadata.get("session_id") or "").strip()
        restored = await self.restore_runtime(bundle, session_id, workspace)
        restored_state = dict(restored.get("coroutine_state") or {})
        execution_runtime = _build_scoped_execution_runtime(
            self, bundle, workspace, memory_candidate_sink,
        )
        channel_envelope, current_channel_envelope, initial_instruction, response_key, response_keys, semantic_input_is_new = self.prepare_input(
            memory_candidate_sink, restored, restored_state, session_metadata,
        )
        state = BunshinAgentLoopState(
            execution_runtime=execution_runtime,
            memory_service=memory_service,
            memory_candidate_sink=memory_candidate_sink,
            channel_envelope=channel_envelope,
            llm_round_count=max(0, int(restored_state.get("llm_round_count") or 0)),
            tool_call_count=max(0, int(restored_state.get("tool_call_count") or 0)),
            output_length_recovery_count=max(
                0,
                int(restored_state.get("output_length_recovery_count") or 0),
            ),
            pending_output_length_recovery_note=str(
                restored_state.get("pending_output_length_recovery_note") or ""
            ),
        )
        self.tool_session.observe_count(max(
            self.tool_session.observed_tool_call_count,
            state.tool_call_count,
        ))
        max_output_tokens = max(
            1,
            int(_resolve_bunshin_max_output_tokens(bundle.llm_runtime, self.pack)),
        )

        continuation, program, turn_id = self.build_continuation(
            bundle, current_channel_envelope, forced_retry_note, max_output_tokens, restored_state,
            semantic_input_is_new, session_metadata, state,
        )
        begin_tool_results = getattr(state.execution_runtime, "begin_tool_result_turn", None)
        if callable(begin_tool_results):
            begin_tool_results(
                turn_id=turn_id,
                scope_key=session_id or f"bunshin:{self.run_id}",
                retention_user_turns=_BUNSHIN_TOOL_RESULT_RETENTION_CALLS,
                input_id=response_key or turn_id,
            )
        if semantic_input_is_new:
            with contextlib.suppress(Exception):
                state.memory_service.tick_heat()
        agent_turn_runtime = self.agent_runtime.build_bunshin_agent_runtime(bundle, state, continuation)
        executor = agent_turn_runtime.executor
        current: EffectResult | None = None
        if self.session_checkpoints.continuation_is_restart_safe(continuation, state.memory_service):
            await self.session_checkpoints.persist_agent_session_checkpoint(
                bundle,
                state,
                continuation,
                initial_instruction=initial_instruction,
                response_keys=response_keys,
            )
        if (
            session_metadata.get("checkpoint_initialization_required")
            and not self.session_checkpoints.agent_session_checkpoint
        ):
            raise AgentSessionCheckpointError("role cannot begin work before its initial checkpoint")
        while True:
            await self.control.raise_if_cancel_requested()
            if self.session_checkpoints.continuation_is_restart_safe(continuation, state.memory_service):
                await self.control.raise_if_restart_requested()
            try:
                yielded = program.send(current) if current is not None else next(program)
            except StopIteration as completed:
                outcome = completed.value
                if not isinstance(outcome, TurnOutcome):
                    raise RuntimeError("bunshin agent loop ended without a turn outcome")
                active_l1_turn = state.memory_service.active_l1_turn(continuation.turn_id)
                if (
                    active_l1_turn is not None
                    and not active_l1_turn.semantic_delta_seen
                    and str(outcome.final_reply or "").strip()
                ):
                    # The completion gate can finish without issuing a real
                    # LLMRequestEffect. Preserve that final reply in the same
                    # invocation-scoped L1 turn before closing it.
                    state.memory_service.upsert_l1_assistant(
                        continuation.turn_id,
                        LLMMessageIR(
                            role=MessageRole.ASSISTANT,
                            parts=(TextPartIR(str(outcome.final_reply)),),
                            semantic_kind="assistant_reply",
                        ),
                    )
                settled = await executor.schedule_post_turn_commit_async(outcome)
                if str(getattr(settled, "state", "")) != "settled":
                    raise RuntimeError(
                        "Bunshin L1 working-set settlement failed; refusing to "
                        "checkpoint a false completion"
                    )
                if not self.status.blocked_summary:
                    await self.reporter.emit_progress(
                        "invocation_finalizing",
                        round=state.llm_round_count,
                        tool_call_count=state.tool_call_count,
                    )
                executor.clear_execution_cursors(continuation)
                self.llm_rounds.sync_bunshin_state_from_continuation(state, continuation)
                self.memory_results.collect_candidates(state.memory_candidate_sink)
                await self.session_checkpoints.persist_agent_session_checkpoint(
                    bundle,
                    state,
                    continuation,
                    initial_instruction=initial_instruction,
                    response_keys=response_keys,
                )
                await self.control.raise_if_cancel_requested()
                await self.control.raise_if_restart_requested()
                # Completion feedback stays in this process, including when
                # live resources forbid a restart-safe durable checkpoint.
                self.completed_loop_state = self.session_checkpoints.capture_coroutine_state(
                    state, continuation, initial_instruction=initial_instruction,
                    response_keys=response_keys,
                )
                self.completed_loop_binding = self.in_process_binding(bundle, session_id)
                self.completed_loop_bundle = bundle
                return outcome.final_reply
            current = await self.llm_rounds.execute_bunshin_agent_effect(
                executor,
                continuation,
                state,
                yielded,
                max_output_tokens=max_output_tokens,
            )
            await self.control.raise_if_cancel_requested()
            if self.session_checkpoints.continuation_is_restart_safe(continuation, state.memory_service):
                await self.session_checkpoints.persist_agent_session_checkpoint(
                    bundle,
                    state,
                    continuation,
                    initial_instruction=initial_instruction,
                    response_keys=response_keys,
                )
                await self.control.raise_if_restart_requested()

    def build_continuation(
        self, bundle: BunshinRuntimeBundle, current_channel_envelope: Any, forced_retry_note: str,
        max_output_tokens: Any, restored_state: Any, semantic_input_is_new: Any, session_metadata: Any, state: Any,
    ) -> tuple[Any, Any, Any]:
        forced_retry_note = str(forced_retry_note or "").strip()

        def build_context(frame: AgentLoopFrame):
            retry_note = str(
                bunshin_output_length_recovery_note(self.pack)
                if state.pending_output_length_recovery_note
                else frame.retry_note or forced_retry_note or ""
            )
            metadata = {"retry_note": retry_note}
            return _bunshin_prompt_context(
                self.pack,
                run_id=self.run_id,
                event=state.channel_envelope.event,
                metadata=metadata,
            )

        active_input_id = str(getattr(state.channel_envelope.event, "event_id", "") or "input")
        turn_id = self.session_memory.resume_or_reopen_l1_turn(
            state.memory_service,
            run_id=self.run_id,
            active_input_id=active_input_id,
            user_text=_bunshin_primary_input(current_channel_envelope),
            fencing_token=int(session_metadata.get("fencing_token") or 0),
            reuse_active=not semantic_input_is_new,
        )
        self.session_memory.abort_stale_l1_turns(
            state.memory_service,
            active_turn_id=turn_id,
        )

        def build_commit_payload(final_reply: str, observations: list[Any], reply_texts: list[str]) -> L1CommitPayload:
            _ = reply_texts
            transcript = [
                L1TranscriptMessage(
                    role="assistant",
                    content=final_reply or "bunshin completed",
                    kind=L1MessageKind.ASSISTANT_REPLY,
                    payload={
                        "_pal_input_id": str(
                            getattr(
                                state.channel_envelope.event,
                                "event_id",
                                "",
                            )
                            or turn_id
                        )
                    },
                ),
            ]
            # The IR turn is opened by TurnExecutor with this invocation-scoped
            # id.  Keep the payload on that same identity so terminal settlement
            # closes the active turn instead of creating a second legacy turn.
            return L1CommitPayload(turn_id=turn_id, transcript=transcript, tool_observations=list(observations))

        program = agent_turn_program(
            turn_id=turn_id,
            build_assembly_context=build_context,
            render_final_text=lambda outcome: str(getattr(outcome, "text", "") or "") if outcome is not None else "",
            build_commit_payload=build_commit_payload,
            max_output_tokens=max_output_tokens,
            build_retry_note=lambda outcome, observations, retry_count: self.llm_rounds.build_bunshin_retry_note(
                outcome,
                observations,
                retry_count,
                state=state,
            ),
        )
        continuation = TurnContinuation(
            turn_id=turn_id,
            opening_event=state.channel_envelope.event,
            delivery_binding=TurnDeliveryBinding.from_envelope(
                state.channel_envelope,
                control_scope_key=f"bunshin:{self.run_id}",
            ),
            program=program,
            correlation_id=self.run_id,
            control_scope_key=f"bunshin:{self.run_id}",
            turn_settings_snapshot=_bunshin_turn_settings_snapshot(self.pack, bundle.llm_runtime),
            tool_batch_count=max(0, int(restored_state.get("tool_batch_count") or 0)),
            preferred_llm_endpoint_id=str(restored_state.get("preferred_llm_endpoint_id") or "") or None,
            preferred_llm_model_id=str(restored_state.get("preferred_llm_model_id") or "") or None,
        )
        return continuation, program, turn_id

    def prepare_input(self, memory_candidate_sink: Any, restored: Any, restored_state: Any, session_metadata: Any) -> tuple[Any, Any, Any, Any, Any, Any]:
        initial_instruction = str(restored_state.get("initial_instruction") or self.pack.instruction or self.pack.goal).strip()
        current_channel_envelope = _bunshin_task_envelope(
            self.pack,
            bunshin_id=self.bunshin_id,
            run_id=self.run_id,
        )
        prompt_pack = self.pack
        if initial_instruction and initial_instruction != str(self.pack.instruction or ""):
            prompt_pack = BunshinInvocationPack.from_dict(
                {**self.pack.to_dict(), "goal": initial_instruction, "instruction": initial_instruction}
            )
        channel_envelope = _bunshin_task_envelope(prompt_pack, bunshin_id=self.bunshin_id, run_id=self.run_id)
        response_keys = [str(item) for item in list(restored_state.get("response_keys") or []) if str(item)]
        previous_response_keys = set(response_keys)
        response_key = str(session_metadata.get("response_key") or "").strip()
        response_text = ""
        if restored and response_key and response_key not in response_keys:
            response_text = self.session_memory.new_session_response_message(
                restored_state,
                response_key=response_key,
                response_text=_bunshin_primary_input(current_channel_envelope),
            )
            response_keys.append(response_key)
        elif not restored and response_key:
            response_keys.append(response_key)
        semantic_input_is_new = (
            not restored
            or bool(response_key and response_key not in previous_response_keys)
        )
        self.session_checkpoints.restore_invocation_checkpoint_state(
            restored_state,
            active_response_key=response_key,
            memory_candidate_sink=memory_candidate_sink,
        )
        if restored:
            event_text = response_text if semantic_input_is_new else ""
            active_input_id = str(restored_state.get("active_input_id") or "").strip()
            channel_envelope = ChannelEnvelope(
                event=EventEnvelope(
                    event_kind=EventKind.USER_MESSAGE,
                    source_kind=SourceKind.BUNSHIN,
                    payload={"text": event_text},
                    correlation_id=self.run_id,
                    event_id=(
                        response_key
                        if semantic_input_is_new
                        else active_input_id or response_key or f"{self.run_id}:resume"
                    ),
                ),
                endpoint=current_channel_envelope.endpoint,
                response_handle=current_channel_envelope.response_handle,
            )
        return channel_envelope, current_channel_envelope, initial_instruction, response_key, response_keys, semantic_input_is_new

    def in_process_binding(self, bundle: BunshinRuntimeBundle, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "session_metadata": dict((self.pack.metadata or {}).get("agent_session") or {}),
            "static_identity": (
                self.session_checkpoints.agent_session_static_identity(bundle)
                if session_id else {}
            ),
        }

    async def restore_runtime(self, bundle: BunshinRuntimeBundle, session_id: Any, workspace: Any) -> Any:
        if self.runtime_initialized:
            if not self.completed_loop_state:
                raise AgentSessionCheckpointError(
                    "in-process continuation has no completed loop handoff"
                )
            if (
                self.completed_loop_bundle is not bundle
                or self.completed_loop_binding != self.in_process_binding(bundle, session_id)
            ):
                raise AgentSessionCheckpointError(
                    "in-process continuation has an incompatible runtime or session binding"
                )
            # Keep the actual runtime and live handles. Reinstalling either the
            # immutable attempt input or an older safe point would erase work.
            state = self.completed_loop_state
            self.completed_loop_state = {}
            return {"coroutine_state": state}
        restored = self.session_checkpoints.load_agent_session_checkpoint(
            workspace,
            session_id=session_id,
            bundle=bundle,
        )
        if restored:
            try:
                await bundle.runtime_state_coordinator.restore(
                    dict(restored["runtime_snapshot"]),
                    expected_identity=RuntimeSnapshotIdentity(
                        logical_coroutine_id=str(restored["logical_coroutine_id"]),
                        workflow_id=str(restored["workflow_id"]),
                        stage_key=str(restored["stage_key"]),
                        sequence=int(restored["sequence"]),
                        producer_fencing_token=int(restored["producer_fencing_token"]),
                        runtime_spec_hash=str(restored["runtime_spec_hash"]),
                    ),
                )
            except (TypeError, ValueError, RuntimeError) as exc:
                raise AgentSessionCheckpointError(
                    "manager-selected agent continuation contains invalid runtime module state"
                ) from exc
        self.runtime_initialized = True
        return restored

    def build_workspace(self, ) -> Any:
        workspace = dict(self.pack.workspace)
        workspace.setdefault("runtime_root", str(self.runtime_root))
        workspace.setdefault("run_id", self.run_id)
        workspace.setdefault("bunshin_id", self.bunshin_id)
        workspace.setdefault("bunshin_profile", self.pack.bunshin_profile)
        workspace.setdefault("invocation_id", self.pack.invocation_id)
        workspace.setdefault("goal", self.pack.instruction or self.pack.goal)
        if isinstance(self.pack.metadata, dict):
            if isinstance(self.pack.metadata.get("requirements_brief"), dict):
                workspace.setdefault("requirements_brief", dict(self.pack.metadata.get("requirements_brief") or {}))
            if isinstance(self.pack.metadata.get("bunshin_v2"), dict):
                # ``workspace.bunshin_v2`` already carries the assignment-bound
                # fields from the prompt pack.  The Manager also puts derived
                # role contracts (for example the verifier's
                # ``swe_verification_tool_contract``) in metadata.  A
                # ``setdefault`` here silently discarded those derived fields
                # whenever the workspace had any binding at all, so a verifier
                # could record a perfectly located finding and still fail to
                # route it because its repair-owner map was missing.  Merge the
                # two projections, with the prompt-pack workspace remaining the
                # authoritative value for fields that are bound there.
                bound_bunshin_v2 = dict(workspace.get("bunshin_v2") or {})
                bound_bunshin_v2.update(
                    {
                        key: value
                        for key, value in dict(self.pack.metadata.get("bunshin_v2") or {}).items()
                        if key not in bound_bunshin_v2
                    }
                )
                workspace["bunshin_v2"] = bound_bunshin_v2
        workspace.setdefault("review_tool_evidence_refs", self.review_evidence.review_tool_evidence_refs)
        return workspace


def _build_scoped_execution_runtime(
    session: AgentSession, bundle: BunshinRuntimeBundle,
    workspace: dict[str, Any], memory_candidate_sink: Any,
) -> BunshinScopedExecutionRuntime:
    """Project current tool contracts without changing the persisted pack."""
    profile = dict(session.pack.resolved_profile or {})
    runtime = BunshinScopedExecutionRuntime(
        bundle.execution_runtime, session.pack.allowed_capabilities, workspace,
        produced_artifacts=session.artifacts.produced_artifacts,
        memory_candidate_sink=memory_candidate_sink,
        capability_guidance_overrides=dict(profile.get("capability_guidance_overrides") or {}),
        capability_policy=dict(profile.get("effective_capability_policy", profile.get("capability_policy")) or {}),
        role_execution_sessions=session.tool_session.execution_sessions,
        check_cancel=session.control.raise_if_cancel_requested,
        request_user_clarification=session.control.request_architecture_clarification,
    )
    session.tool_session.bind_capabilities([
        str(dict(spec.get("function") or {}).get("name") or "").strip()
        for spec in runtime.build_llm_tool_contracts()
        if str(dict(spec.get("function") or {}).get("name") or "").strip()
    ])
    return runtime
