"""Core coordinates Memory transactions and LLM compaction candidates."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from pal.foundation.diagnostics import exception_report
from pal.llm.compaction_engine import CompactionClockKind, CompactionRunResult
from pal.llm.compaction_contracts import CompactionService
from pal.llm.conversions import tool_definition_ir_from_dict
from pal.memory.persistence.contracts import RESIDENT_HISTORY
from pal.shared.tool_protocol import ToolResultIR

LOGGER = logging.getLogger(__name__)


class CompactionOperation:
    def __init__(self, *, context, state, llm, projection, engine: CompactionService | None,
                 clock: Callable[[], int], build_prompt, build_tools, clear_cursors):
        self.context = context
        self.state = state
        self.llm = llm
        self.projection = projection
        self.engine = engine
        self.clock = clock
        self._build_canonical_prompt = build_prompt
        self._build_llm_tool_contracts = build_tools
        self.clear_execution_cursors = clear_cursors

    @property
    def accepts_memory_candidates(self) -> bool:
        return self.engine is not None and self.engine.policy.accepts_memory_candidates

    def deadline_seconds(self, *, max_attempts: int | None = None) -> float:
        engine = self.engine
        if engine is None:
            return 180.0
        attempts = engine.max_attempts if max_attempts is None else max_attempts
        # The admission ticket also spans prompt preparation and model apply.
        return engine.timeout_seconds * max(1, attempts) + 30.0


    async def run(
        self,
        memory_service: Any,
        *,
        target_input_budget: int,
        reserved_output_tokens: int,
        assembly_context: Any | None = None,
        continuation: Any | None = None,
        preferred_endpoint_id: str | None = None,
        preferred_model_id: str | None = None,
        max_attempts: int | None = None,
        timeout_seconds: float | None = None,
        replay_guard: Callable[[], bool] | None = None,
        commit_guard: Callable[[], bool] | None = None,
        force_semantic: bool = False,
    ) -> CompactionRunResult:
        engine = self.engine
        if engine is None:
            return CompactionRunResult(
                status="engine_unavailable",
                clock_kind=CompactionClockKind.USER_TURN,
            )
        llm_runtime = self.llm.optional
        if llm_runtime is None:
            return CompactionRunResult(
                status="engine_unavailable",
                clock_kind=engine.policy.clock_kind,
            )
        if max_attempts is not None or timeout_seconds is not None:
            engine = engine.with_limits(max_attempts=max_attempts, timeout_seconds=timeout_seconds)
        metadata = dict(assembly_context.metadata) if assembly_context is not None else {}
        logical_scope_id = str(
            metadata.get("prompt_cache_scope_id") or ""
        ).strip()
        if not logical_scope_id:
            bunshin_scope_key = (
                (assembly_context.work_order_id if assembly_context is not None else "")
                or (continuation.turn_id if continuation is not None else None)
            )
            logical_scope_id = (
                f"bunshin:{bunshin_scope_key}"
                if assembly_context is not None and assembly_context.core_mode == "bunshin"
                else "pal:resident"
            )
        metadata["prompt_cache_scope_id"] = logical_scope_id
        preferred_endpoint_id = (
            preferred_endpoint_id
            or metadata.get("preferred_endpoint_id")
            or (continuation.preferred_llm_endpoint_id if continuation is not None else None)
        )
        preferred_model_id = (
            preferred_model_id
            or metadata.get("preferred_model_id")
            or (continuation.preferred_llm_model_id if continuation is not None else None)
        )
        if preferred_endpoint_id:
            metadata["preferred_endpoint_id"] = preferred_endpoint_id
        if preferred_model_id:
            metadata["preferred_model_id"] = preferred_model_id
        try:
            clock_value = max(
                0,
                int(self.clock() or 0),
            )
        except Exception:
            clock_value = 0
        execution_runtime = self.context.execution_runtime
        execution_runtime.bind_result_history(memory_service)

        def current_l1_result_ids() -> tuple[str, ...]:
            turns = memory_service.history.turns if memory_service is not None else ()
            ids: list[str] = []
            for turn in list(turns or ()):
                for message in turn.messages:
                    for part in message.parts:
                        if isinstance(part, ToolResultIR):
                            ids.append(part.call_id)
                # Successor segments keep logical references for results the
                # compaction carried forward: they are not removable (F25).
                for ref in dict(turn.metadata or {}).get(
                    "compact_retained_result_refs", ()
                ) or ():
                    ids.append(str(ref))
            return tuple(dict.fromkeys(ids))

        result_ids_before_compact = current_l1_result_ids()
        plan = llm_runtime.plan_compaction(preferred_endpoint_id, force_semantic=force_semantic)
        strategy = plan.strategy
        metadata["compaction_strategy"] = strategy
        metadata["retained_result_call_ids"] = result_ids_before_compact


        after_compact = None
        if result_ids_before_compact:
            def retire_compacted_l1_results() -> None:
                remaining_ids = set(current_l1_result_ids())
                removed = tuple(
                    result_id
                    for result_id in result_ids_before_compact
                    if result_id not in remaining_ids
                )
                if removed:
                    retirement_turn_id = (
                        str(continuation.turn_id)
                        if continuation is not None
                        else None
                    )
                    execution_runtime.retire_tool_results(
                        turn_id=retirement_turn_id,
                        result_ids=removed,
                        execution_lifetime_id=(
                            "" if retirement_turn_id else logical_scope_id
                        ),
                    )

            after_compact = retire_compacted_l1_results

        run_id = str(metadata.get("compaction_op_id") or "").strip()
        # v3: compact the LEFT segment only.  Closed history is promoted
        # at this request boundary, the run is opened on the history
        # owner (single-writer arbitration), and the right side never
        # enters the summary source.  Warm replay anchors are a later
        # increment; the first cut is the honest cold-left path.
        import time as _time

        root = (memory_service.history_root if memory_service is not None else None)
        if root is None:
            return CompactionRunResult(
                status="engine_unavailable",
                failures=("memory service has no history_root",),
                clock_kind=engine.policy.clock_kind,
            )
        history = self.context.port_registry.get(RESIDENT_HISTORY.name)
        if history is not None and not await history.reconcile_async():
            failure = (
                "resident history recovery failed; persistence must be repaired before compaction"
                if history.inspect().get("restore_failed")
                else "pending dialogue archive could not be reconciled"
            )
            return CompactionRunResult(
                status="commit_failed", failures=(failure,),
                clock_kind=engine.policy.clock_kind,
            )
        try:
            if continuation is None and plan.promote_before_capture:
                root.promote()
        except Exception as exc:
            LOGGER.warning("compact promote_failed failed_at=%s\n%s",
                           datetime.now(timezone.utc).isoformat(), exception_report(exc))
            return CompactionRunResult(
                status="error",
                failures=("promote failed: " + exception_report(exc),),
                clock_kind=engine.policy.clock_kind,
                clock_value=clock_value,
            )
        if not run_id:
            run_id = uuid4().hex
            metadata["compaction_op_id"] = run_id
        # One absolute deadline spans preflight, generation, repair, and
        # the install gate (F1/N03): the run never resets its clock, and
        # commit eligibility is checked on the owner, not trusted from
        # engine internals.
        deadline_at = (
            _time.monotonic()
            + engine.timeout_seconds * max(1, engine.max_attempts)
        )
        try:
            left_snapshot = memory_service.begin_left_compaction(
                run_id,
                reason="auto",
                parent_turn_id=(
                    str(continuation.turn_id) if continuation is not None else ""
                ),
                deadline_at=deadline_at,
                include_completed_tail=plan.include_completed_tail,
                allow_summary=force_semantic,
            )
        except Exception as exc:
            LOGGER.warning("compact begin_failed run=%s failed_at=%s\n%s",
                           run_id, datetime.now(timezone.utc).isoformat(), exception_report(exc))
            # NoBeneficialCompaction (minimal seed / empty left) and
            # CompactLaneBusy are structured outcomes, not failures to
            # retry: report them without touching L or R.
            return CompactionRunResult(
                status="no_benefit"
                if type(exc).__name__ == "NoBeneficialCompaction"
                else "error",
                failures=(exception_report(exc),),
                clock_kind=engine.policy.clock_kind,
                clock_value=clock_value,
            )

        def build_compaction_request(endpoint_id, model_id):
            from pal.shared import PromptAssemblyContext
            shell = self._build_canonical_prompt(
                assembly_context or PromptAssemblyContext(metadata={"typed_l1_projection": True}),
                max_output_tokens=max(512, reserved_output_tokens), model_hint=model_id)
            return replace(shell,
                tools=tuple(tool_definition_ir_from_dict(tool) for tool in self._build_llm_tool_contracts()),
                metadata={**shell.metadata, **metadata, "preferred_endpoint_id": endpoint_id})

        def prepare_source(left_snapshot):
            return self.projection.prepare_compaction_source(
                memory_service, llm_runtime, left_snapshot,
                logical_scope_id=logical_scope_id, preferred_endpoint_id=preferred_endpoint_id,
                preferred_model_id=preferred_model_id,
                active_turn_id=str(continuation.turn_id) if continuation is not None else "",
                strategy=plan.strategy, build_request=build_compaction_request,
                response_continuation=plan.response_continuation)
        try:
            source = prepare_source(left_snapshot)
            while (plan.include_completed_tail and left_snapshot.cut != root.cut
                    and source.replay_request is not None
                    and await asyncio.wait_for(llm_runtime.native_source_over_standard_limit(source.replay_request),
                                               timeout=max(0, deadline_at - _time.monotonic()))):
                left_snapshot = root.restrict_compaction_to_left(run_id)
                source = prepare_source(left_snapshot)
            if plan.include_completed_tail and left_snapshot.cut != root.cut and source.replay_request is not None:
                replay = source.replay_request
                source = replace(source, replay_request=replace(replay, metadata={
                    **replay.metadata, "compact_includes_right_tail": True}))
        except asyncio.CancelledError:
            memory_service.close_compaction(run_id, cancelled=True, reason="source_selection_cancelled")
            raise
        except Exception as exc:
            memory_service.close_compaction(run_id, cancelled=False, reason="source_unavailable")
            return CompactionRunResult(status="source_unavailable", failures=(exception_report(exc),),
                                       clock_kind=engine.policy.clock_kind, clock_value=clock_value)
        metadata.update(preferred_endpoint_id=source.endpoint_id, preferred_model_id=source.model_id,
            compact_source_ids=tuple(message.message_id for turn in left_snapshot.turns for message in turn.messages))

        def close_run_terminal(*, cancel: bool, reason: str) -> None:
            """Idempotent close of OUR run (F1 / review R2-b).

            A run already terminal (e.g. Memory committed), a run
            retired by reset, or a superseded run is left alone — a
            vanished run is terminal by construction and late cleanup
            must never raise StaleRun out of its own finally path.
            """

            memory_service.close_compaction(run_id, cancelled=cancel, reason=reason)

        def post_commit_result(fault: str) -> CompactionRunResult:
            """Committed outcome with post-fault diagnostics (review R2-a).

            The install already won; the owner's terminal record is the
            authority.  The caller gets a success carrying the accepted
            memory result and the packaging fault — never a fabricated
            empty success, never a `history unchanged` claim, and the
            required projection rebase still fires.
            """

            memory_result = memory_service.compaction_result(run_id)
            assert memory_result is not None
            diagnostics = self.state.diagnostics
            if diagnostics is not None:
                diagnostics.append({
                    "kind": "two_segment_post_commit_packaging_fault",
                    "scope": str(logical_scope_id),
                    "run_id": run_id,
                    "error": fault,
                })
            self.projection.rebase(
                llm_runtime, logical_scope_id, root,
                memory_service=memory_service)
            return CompactionRunResult(
                status="compacted",
                summary_entry=None,
                memory_result=memory_result,
                failures=(f"post-commit packaging fault: {fault}",),
                clock_kind=engine.policy.clock_kind,
                clock_value=clock_value,
            )

        try:
            history = self.context.port_registry.get(RESIDENT_HISTORY.name)
            history_reference = history.prepare_history_reference(left_snapshot) if history is not None else None
            snapshot = memory_service.capture_compaction(
                left_snapshot,
                target_input_budget=target_input_budget,
                reserved_output_tokens=reserved_output_tokens,
                clock_kind=engine.policy.clock_kind,
                clock_value=clock_value,
                metadata=metadata,
                replay_request=source.replay_request,
                next_request=source.next_request,
                replay_dialect=source.dialect,
                replay_wire_shape=source.wire_shape,
            )
            run_result = await asyncio.wait_for(
                engine.run(
                    snapshot,
                    llm_runtime=llm_runtime,
                    replay_guard=replay_guard,
                    commit_hooks=history_reference,
                ),
                timeout=max(deadline_at - _time.monotonic(), 0.05),
            )
            if run_result.status == "candidate":
                installed = await memory_service.install_compaction(snapshot,
                    summary_entry=run_result.summary_entry, after_commit=after_compact,
                    commit_guard=commit_guard, commit_hooks=history_reference)
                if isinstance(installed, Exception):
                    run_result = replace(run_result, status="commit_failed", failures=(*run_result.failures, exception_report(installed)))
                else:
                    run_result = replace(run_result, status="compacted", memory_result=installed)
        except asyncio.CancelledError:
            if memory_service.compaction_result(run_id) is not None:
                # Post-commit external cancel (review R2-a): keep the
                # COMMITTED fact, mark derived state, then honor the
                # stop intent — never swallow the cancellation.
                self.projection.rebase(
                    llm_runtime, logical_scope_id, root,
                    memory_service=memory_service)
            else:
                close_run_terminal(cancel=True,
                                   reason="orchestration_cancelled")
            raise
        except TimeoutError:
            if memory_service.compaction_result(run_id) is not None:
                return post_commit_result("deadline exceeded after commit")
            close_run_terminal(cancel=False, reason="deadline")
            return CompactionRunResult(
                status="error",
                failures=("deadline: compaction run exceeded its absolute "
                          "time budget",),
                clock_kind=engine.policy.clock_kind,
                clock_value=clock_value,
            )
        except Exception as exc:
            fault = exception_report(exc)
            if memory_service.compaction_result(run_id) is not None:
                # Memory installed and then failed while packaging
                # the result; classification follows the owner's terminal
                # state, not the await's exception kind.
                return post_commit_result(fault)
            close_run_terminal(cancel=False,
                               reason=f"orchestration_error:{type(exc).__name__}")
            return CompactionRunResult(
                status="error",
                failures=(fault,),
                clock_kind=engine.policy.clock_kind,
                clock_value=clock_value,
            )
        finally:
            # A packaging fault or cancellation after install still requires
            # saving the new work state before publishing its archive.
            if history is not None and memory_service.compaction_result(run_id) is not None:
                await history.checkpoint_async("compact", rebase=True)
        if not run_result.success:
            # The engine returns instead of raising on generation
            # failure; close our run idempotently (reset may already
            # have retired it — that is a valid terminal outcome).
            close_run_terminal(cancel=False,
                               reason=f"engine:{run_result.status}")
        else:
            # The install already won: rebasing the hosted projection
            # session is post-commit work (F13) — a failure here is
            # recorded, never a rollback of the new left.
            self.projection.rebase(
                llm_runtime, logical_scope_id, root,
                memory_service=memory_service)
        if not run_result.success or continuation is None:
            return run_result
        self.clear_execution_cursors(continuation)
        return run_result
