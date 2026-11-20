"""Projection construction and replay are owned by LLM."""
from __future__ import annotations
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Sequence
from pal.shared.ports import ServiceRef
from pal.llm.contracts import LLMService
from pal.memory.contracts import MemoryServicePort
from pal.memory.history_contracts import LeftSnapshot
from uuid import uuid4
from pal.llm.ir import ImagePartIR, LLMMessageIR, LLMRequestIR, PromptRegionIR
from pal.memory.contracts import MemoryPackRequest


@dataclass(frozen=True)
class CompactionSource:
    replay_request: LLMRequestIR | None = None
    next_request: LLMRequestIR | None = None
    dialect: str = ""
    wire_shape: str = ""
    endpoint_id: str | None = None
    model_id: str | None = None


class CompactionSourceUnavailable(ValueError):
    pass


class HistoryProjection:
    """LLM-derived request projections over a published immutable Memory history."""

    def __init__(self, memory: ServiceRef[MemoryServicePort], diagnostics: list[dict[str, Any]],
        project_messages: Callable[..., list[LLMMessageIR]],
        capabilities: Callable[[str | None], dict[str, Any]]) -> None:
        self.memory = memory
        self.diagnostics = diagnostics
        self.project_messages = project_messages
        self.capabilities = capabilities

    def prepare(
        self, llm_runtime: Any, request: LLMRequestIR
    ) -> tuple | None:
        """Owner-side projection prepare for one ordinary LLM round (N3).

        F1 (review af51d74): the round is prepared from the runtime's
        immutable prepared-generation plan — the projection encodes the
        COMPILED effective request for the RESOLVED endpoint with its
        strong binding (real spec identity, continuation contract version,
        config fingerprint).  Placeholder-bound sessions are never used.

        Returns ``(EncodedRequest, EndpointBinding, handle)`` or None —
        None everywhere means the honest cold codec path.  The view is the
        effective request's non-preamble messages not yet frozen in the
        session; a message without a durable identity aborts the projection
        (transient compiler content must never freeze into the prefix).
        Preparation happens here, on the owner, before any worker thread
        touches the session (J5).
        """

        # Proactive requests deliberately exclude resident history. The shared
        # history root (including its left bootstrap) cannot encode this view.
        # Use the ordinary codec with the complete, isolated current request.
        if request.metadata.get("history_visibility") == "active_turn":
            return None
        from pal.llm.compaction import is_native_window
        if any(is_native_window(message) for message in request.messages):
            return None  # The complete canonical native window is encoded once by the codec.
        projection = llm_runtime.projection_port
        if projection is None:
            return None
        try:
            plan = projection.prepare_generation_plan(request)
        except Exception:
            return None
        if plan is None or plan.compact_required:
            return None
        scope_id = str(request.metadata.get("projection_scope_id") or request.metadata.get("prompt_cache_scope_id") or "").strip()
        if not scope_id:
            return None
        try:
            session = projection.endpoint_projection_session(scope_id, plan=plan)
        except Exception:
            return None
        if session is None:
            return None
        binding = session.binding
        identity = session.identity
        if binding is None or identity is None:
            return None
        if session.active_attempt is not None:
            # A round is already open (retry/reentry); do not interleave a
            # second prepare on one lineage.
            return None
        # Staleness guard (J7): a committed left replacement this session
        # never consumed leaves the frozen prefix replaying retired history.
        # Compare the root's left-replacement generation; mismatch = cold.
        memory_service = self.memory.optional
        if memory_service is None:
            return None
        root = memory_service.history_root
        root_generation = root.left_generation
        root_generation = int(root_generation or 0)
        # Durable storage is not visibility: scoped/excluded control records
        # remain in L1 after the compiler stops sending them. Rebuild once at
        # that legal projection boundary; never replay a hidden instruction.
        visible_ids = {m.message_id for m in plan.effective_request.messages}
        stored_turns = (memory_service.history if memory_service is not None else None)
        durable_ids: set[str] = set()
        hidden_context_ids: set[str] = set()
        for turn in (stored_turns.turns if stored_turns is not None else ()):
            for message in turn.messages:
                durable_ids.add(str(message.message_id))
                if message.semantic_kind == "pal_prompt_context" and message.message_id not in visible_ids:
                    hidden_context_ids.add(message.message_id)
        continuity = (stored_turns.continuity if stored_turns is not None else None)
        if continuity is not None:
            durable_ids.add(str(continuity.standalone_id))
        if hidden_context_ids.intersection(session.covered_message_ids()):
            session.rebind(binding, capabilities=plan.capabilities)
            identity = session.identity
        session_generation = int(session.history_left_revision or 0)
        if root_generation != session_generation:
            # C3 (review c9cb2d2): a fresh or rebound lineage owns no frozen or
            # pending wire bytes and cannot be replaying retired history — it
            # is simply not materialized yet.  The restored/rebound process
            # gives it ONE cold rebuild from the CURRENT canonical L model
            # view plus the current R through the same owner interface the
            # post-commit rebase uses; content and coverage are established
            # together, and only then does the consumed generation record the
            # rebuilt view.  A lineage that already holds wire content keeps
            # the strict stale refusal (explicit rebase required) — no
            # counter-only permission for a retired prefix.
            if not session.has_materialized_content():
                self.bootstrap(
                    scope_id, root,
                    memory_service=memory_service, session=session,
                )
                session_generation = int(
                    session.history_left_revision or 0
                )
        if root_generation != session_generation:
            diagnostics = self.diagnostics
            if diagnostics is not None:
                diagnostics.append({
                    "kind": "two_segment_turn_projection_stale_left",
                    "scope": scope_id,
                    "root_generation": root_generation,
                    "session_generation": session_generation,
                })
            return None
        # R3 (review 95373ef): "already in the prefix" covers committed
        # chunk spans AND the L-owned reference ids a rebase installed —
        # the next request must not re-append the compiler's identical
        # continuity injection as fresh tail (that doubled the summary).
        frozen = set(session.covered_message_ids())
        # Durable identity = present in the L1 store (or the L-owned
        # continuity reference).  A request message whose id is NOT durable
        # is transient compiler content: it must never freeze into the
        # prefix, so the whole round falls back to the cold codec path.
        # F6 (review af51d74): a lineage whose frozen chunk spans reference
        # history that is no longer durable (soft reset, out-of-band clear)
        # must fall back cold instead of replaying retired history.  Reset
        # rolls the incarnation and retires hosted sessions; this structural
        # guard is the seam that stays honest even when retirement was never
        # wired (direct service-level resets, restored stores).
        frozen_span_ids = {
            str(message_id)
            for chunk in session.chunks or ()
            for message_id in (chunk.semantic_span or ())
        }
        if frozen_span_ids and not frozen_span_ids <= durable_ids:
            diagnostics = self.diagnostics
            if diagnostics is not None:
                diagnostics.append({
                    "kind": "two_segment_turn_projection_lineage_stale",
                    "scope": scope_id,
                    "nondurable_spans": sorted(frozen_span_ids - durable_ids),
                })
            return None
        # R4/M17 (review 95373ef): only the LEADING run of system/developer
        # messages is shell-owned preamble (the codecs hoist exactly those
        # heads; mid-conversation developer contexts degrade chronologically).
        # Every other message — developer contexts included — rides the
        # conversation: durable-id checked, frozen-skipped, else tail.  The
        # old role-wide skip let each new pal_context revision jump ahead of
        # the frozen prefix, reordering wire bytes mid-payload.
        preamble_roles = {"system", "developer"}
        request_messages = tuple(plan.effective_request.messages)
        head_end = 0
        for message in request_messages:
            if message.role.value in preamble_roles:
                head_end += 1
            else:
                break
        tail: list[LLMMessageIR] = []
        for index, message in enumerate(request_messages):
            if index < head_end:
                continue
            message_id = str(message.message_id or "")
            if not message_id or message_id not in durable_ids:
                return None  # transient identity: cold fallback
            if message_id in frozen:
                continue
            tail.append(message)
        try:
            import json as _json

            from pal.llm.projection_contracts import AttemptKey, OwnerFence
            from pal.llm.projection_session import HistoryView
            from pal.llm.shapes.base import EncodedRequest

            attempt = AttemptKey(
                identity,
                OwnerFence(int(session.owner_fence or 0)),
                f"turn-{uuid4().hex[:12]}",
            )
            session.begin_round(attempt, requires_native=False)
            prepared = session.prepare_normal(
                HistoryView(cursor=session.frontier, messages=tuple(tail)),
                request_shell=plan.effective_request,
            )
            encoded = EncodedRequest(
                payload=_json.loads(prepared.payload_json),
                message_spans=tuple(prepared.message_spans),
                extra_body=dict(prepared.extra_body),
                applied_cache_breakpoint_message_ids=tuple(
                    prepared.applied_cache_breakpoint_message_ids
                ),
            )
        except Exception as exc:
            diagnostics = self.diagnostics
            if diagnostics is not None:
                diagnostics.append({
                    "kind": "two_segment_turn_projection_prepare_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                })
            try:
                if session.active_attempt is not None:
                    session.reject_commit(attempt.attempt_id, reason="prepare_failed")
            except Exception:
                pass
            return None
        return (encoded, binding, {"session": session, "attempt": attempt,
                                   "tail_ids": tuple(m.message_id for m in tail),
                                   "plan": plan})

    def abandon(self, projection_pack: tuple | None) -> None:
        """R6 (review 95373ef): close an unaccepted draft owned by THIS pack.

        Runs on every exit path of ``_handle_llm_request`` after prepare;
        a round the observation already closed (commit or reject) is a
        no-op, and a lineage that has moved on to a successor attempt is
        left untouched — late cleanup must never release someone else's
        round or roll back an accept that already committed.
        """

        if projection_pack is None:
            return
        session = projection_pack[2]["session"]
        attempt = projection_pack[2]["attempt"]
        active = session.active_attempt
        if active is None:
            return
        if active.attempt_id != attempt.attempt_id:
            return
        try:
            session.reject_commit(attempt.attempt_id, reason="round_abandoned")
        except Exception:
            return
        diagnostics = self.diagnostics
        if diagnostics is not None:
            diagnostics.append({
                "kind": "two_segment_turn_projection_abandoned",
                "reason": "round_abandoned",
                "attempt": attempt.attempt_id,
            })

    def accept(
        self, projection_pack: tuple | None, continuation: Any, outcome: Any
    ) -> None:
        """Close the accept loop for one projected round (N3/W5, F2/F3/F4).

        Only a receipt-authorized, semantically accepted outcome freezes:
        the typed send receipt must name THIS attempt and confirm the
        projected payload actually went on the wire (F2); failures,
        errors, dropped/fallback sends and pre-boundary discards reject
        the round so the next request re-sends the tail cold instead of
        freezing an answer that never happened. Native continuation derives
        only from the accepted message's replay envelope. Its tool names and
        arguments must match accepted IR, and structural continuation contracts
        must be preserved. Degraded material refuses the freeze; required
        thinking/signature content is never silently re-encoded or discarded.
        Projection bookkeeping never alters the accepted outcome.
        """

        if projection_pack is None:
            return
        session = projection_pack[2]["session"]
        attempt = projection_pack[2]["attempt"]
        tail_ids = projection_pack[2]["tail_ids"]
        diagnostics = self.diagnostics

        def _reject(reason: str) -> None:
            try:
                session.reject_commit(attempt.attempt_id, reason=reason)
            except Exception:
                pass
            if diagnostics is not None:
                diagnostics.append({
                    "kind": "two_segment_turn_projection_rejected",
                    "reason": reason,
                    "attempt": attempt.attempt_id,
                })

        receipt = outcome.projection_receipt
        if receipt is None:
            # No projection was offered, or the runtime never reported one:
            # without a receipt nothing may freeze (F2).
            _reject("no_send_receipt")
            return
        if str((receipt.attempt_id if receipt is not None else '')) != str(attempt.attempt_id):
            _reject("attempt_mismatch")
            return
        if not (receipt.applied if receipt is not None else False):
            _reject(f"projection_not_applied:{(receipt.detail if receipt is not None else '')}")
            return
        accepted = outcome.response.message
        finish_reason = outcome.response.finish_reason
        from pal.shared import LLMFinishReason as _FinishReason

        if accepted is None or finish_reason in (
            _FinishReason.ERROR,
            _FinishReason.COMPACT_REQUIRED,
        ):
            _reject("round_failed")
            return
        # L1's accepted contribution is the only replay authority. The send
        # receipt proves admission, but carries no independent response copy.
        from pal.llm.replay_acceptance import native_candidate_from_accepted_message

        closed_call_ids: tuple[str, ...] = ()
        accepted_messages: tuple = ()
        native_committed = False
        try:
            native = native_candidate_from_accepted_message(accepted)
            if native is not None:
                session.attach_native(attempt, native)
                native_committed = True
                closed_call_ids = tuple(native.call_ids)
            elif accepted.parts:
                # This also covers merged Completion replay: the codec uses
                # its accepted message envelope and preserves every piece.
                accepted_messages = (accepted,)
        except Exception as exc:
            if diagnostics is not None:
                diagnostics.append({
                    "kind": "two_segment_turn_projection_native_degraded",
                    "error": f"{type(exc).__name__}: {exc}",
                })
            _reject("native_degraded")
            return
        try:
            import hashlib as _hashlib

            from pal.llm.projection_contracts import (
                AppendReceipt, HistoryCommitReceipt, HistoryCursor,
            )

            before = session.frontier
            accepted_id = str((accepted.message_id if accepted is not None else '') or "")
            span_ids = (*tail_ids, accepted_id) if accepted_id else tail_ids
            after = HistoryCursor(
                history_epoch=before.history_epoch,
                block_sequence=before.block_sequence + 1,
                prefix_digest=_hashlib.sha256(
                    "".join(span_ids).encode("utf-8")
                ).hexdigest(),
            )
            session.observe_commit(
                HistoryCommitReceipt(
                    attempt=attempt,
                    append=AppendReceipt(
                        before=before, after=after, block_count=1),
                    closed_call_ids=closed_call_ids,
                    native_committed=native_committed,
                ),
                accepted_messages=accepted_messages,
                span_message_ids=span_ids,
                native_message_id=accepted_id if native_committed else "",
            )
        except Exception as exc:
            diagnostics = self.diagnostics
            if diagnostics is not None:
                diagnostics.append({
                    "kind": "two_segment_turn_projection_commit_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                })

    def rebase(
        self, llm_runtime: Any, scope_id: str, root: Any, *,
        memory_service: Any = None,
    ) -> None:
        """Drive on_left_replaced on the runtime-hosted projection session.

        Post-commit only (F13): rebase failures land in diagnostics and never
        roll back the installed left segment.

        R3/B1 (review 95373ef / 4b14ce4): the reseed uses the MODEL view of L
        — the standalone continuity reference that normal compiler injection
        and the handoff carry — and registers the ids of EVERYTHING that view
        materialized as L-owned coverage, so the next prepare never re-appends
        L content the rebuilt prefix already carries.
        """

        projection = llm_runtime.projection_port
        if projection is None:
            return
        try:
            session = projection.endpoint_projection_session(scope_id, rebind=False)
            if session is None:
                return
            self.install_left(
                session, memory_service=memory_service, root=root
            )
        except Exception as exc:
            diagnostics = self.diagnostics
            if diagnostics is not None:
                diagnostics.append({
                    "kind": "two_segment_projection_rebase_failed",
                    "scope": str(scope_id),
                    "error": f"{type(exc).__name__}: {exc}",
                })

    def bootstrap(
        self, scope_id: str, root: Any, *,
        memory_service: Any = None,
        session: Any,
    ) -> None:
        """C3 (review c9cb2d2): one cold rebuild for an unmaterialized lineage.

        A fresh or rebound projection that never consumed a left generation
        is initialized from the CURRENT canonical L model view + current R
        with the endpoint plan/profile its binding already carries.  Same
        seed/coverage rules as the post-commit rebase — this is
        that same entry, not a third recovery path.  Never called on a
        materialized lineage and never a per-round fallback: after this one
        rebuild the consumed generation matches the root and ordinary
        prepares resume; a later mismatch without an explicit rebase stays
        refused.
        """

        try:
            self.install_left(
                session, memory_service=memory_service, root=root
            )
        except Exception as exc:
            diagnostics = self.diagnostics
            if diagnostics is not None:
                diagnostics.append({
                    "kind": "two_segment_projection_bootstrap_failed",
                    "scope": str(scope_id),
                    "error": f"{type(exc).__name__}: {exc}",
                })

    def install_left(
        self, session: Any, *, memory_service: Any, root: Any
    ) -> None:
        """One left replacement install, shared by rebase and bootstrap (C3).

        Builds the SAME facts for both callers: the model-view seed (with
        the full L-owned coverage its encode materializes), the surviving
        frozen right side the session actually holds, and the post-install
        cursor base at the authority's own left generation.  The
        native/coverage rules live in ``on_left_replaced`` itself.
        """

        rebase = session.on_left_replaced
        import hashlib as _hashlib

        from pal.llm.projection_contracts import HistoryCursor
        from pal.llm.projection_session import LeftReplacement

        seed_messages: tuple = tuple(root.left_messages())
        seed_coverage_ids: tuple[str, ...] = ()
        if memory_service is not None:
            seed_messages, seed_coverage_ids = self.left_view(
                memory_service, root
            )
        # F4 (review): the keeper is the session's ACTUAL frozen right
        # side — unfrozen right messages stay in the open tail and must
        # not claim chunk survival; a session with no committed chunks
        # keeps nothing.  The cursor epoch comes from the history
        # authority's own left revision, never a hardcoded constant.
        frozen_ids = set(session.frozen_message_ids())
        kept = tuple(
            message
            for message in root.right_messages()
            if message.message_id in frozen_ids
        )
        left_revision = root.left_generation
        left_revision = int(left_revision or 0)
        digest = _hashlib.sha256(
            "".join(
                m.message_id for m in (*seed_messages, *kept)
            ).encode("utf-8")
        ).hexdigest()
        rebase(
            LeftReplacement(
                seed_messages=seed_messages,
                kept_frozen_messages=kept,
                cursor_after=HistoryCursor(
                    history_epoch=left_revision,
                    block_sequence=1,
                    prefix_digest=digest or "0" * 64,
                ),
                left_revision=left_revision,
                seed_coverage_ids=seed_coverage_ids,
            )
        )

    def left_view(
        self, memory_service: Any, root: Any
    ) -> tuple[tuple, tuple[str, ...]]:
        """R3/B1: the reseed as the MODEL view of L, with its full coverage.

        When the cut owns the compact seed, the model sees the standalone
        continuity reference — the same message normal compiler injection
        and the handoff carry — not the raw assistant seed the authority
        stores.  Encoding the raw form here would put the same summary on
        the wire twice (raw assistant seed + standalone user reference).

        B1 (review 4b14ce4): the seed is not always "just the summary".  A
        recovery/rebind bootstrap hands this entry the WHOLE current L model
        view (summary plus promoted ordinary groups), so the returned
        coverage is the model-view id of EVERY message the seed encode
        receives — one encode, one coverage set.  Returns
        ``(seed_messages, seed_coverage_ids)``; the ids are empty only for
        an empty seed.
        """

        def coverage(messages: Sequence[Any]) -> tuple[str, ...]:
            # Same coordinates as the encode: the standalone substitution
            # above keeps the canonical source id out of the model view.
            return tuple(
                dict.fromkeys(
                    str(message.message_id)
                    for message in messages
                    if str(message.message_id or "")
                )
            )

        from pal.memory.context_view import projected_messages
        from pal.memory.turn_ir import L1TurnState

        # Bootstrap uses the same visibility rules as ordinary compilation.
        # Raw L1 still retains expired instructions and excluded revisions.
        selected = []
        for turn in root.left_turns():
            visible = {m.message_id for m in projected_messages(
                turn, settled=turn.state != L1TurnState.ACTIVE)}
            selected.extend(
                message for message in turn.messages
                if message.semantic_kind != "pal_prompt_context" or message.message_id in visible
            )
        messages = tuple(selected)
        turns = (memory_service.history if memory_service is not None else None)
        continuity = (turns.continuity if turns is not None else None)
        if continuity is None or not str((continuity.source_id if continuity is not None else '') or ""):
            return messages, coverage(messages)
        summary_turn_id = (turns.summary_turn_id if turns is not None else None)
        if not summary_turn_id or not any(
            turn.turn_id == summary_turn_id for turn in root.left_turns()
        ):
            return messages, coverage(messages)
        if not any(
            message.message_id == continuity.source_id for message in messages
        ):
            return messages, coverage(messages)
        standalone = continuity.standalone_message()
        mapped = tuple(
            standalone if message.message_id == continuity.source_id else message
            for message in messages
        )
        return mapped, coverage(mapped)

    def left_model_ids(
        self, memory_service: Any, left_ids: tuple[str, ...]
    ) -> tuple[str, ...]:
        """R3: map raw L ids into the model-view coordinates.

        The cut-owned compact seed appears to the model as its standalone
        continuity reference (``continuity:<source>``), while the history
        authority stores the raw seed id.  Warm coverage compares like for
        like through this canonical mapping — shared with the rebase seed
        view and the normal compiler injection — instead of raw-vs-model
        equality, which refused every warm attempt on a two-segment
        history.  No cut-owned seed means no substitution.
        """

        ids = tuple(str(value) for value in left_ids)
        turns = (memory_service.history if memory_service is not None else None)
        continuity = (turns.continuity if turns is not None else None)
        if continuity is None or not str((continuity.source_id if continuity is not None else '') or ""):
            return ids
        root = (memory_service.history_root if memory_service is not None else None)
        summary_turn_id = (turns.summary_turn_id if turns is not None else None)
        if root is None or not summary_turn_id:
            return ids
        if not any(turn.turn_id == summary_turn_id for turn in root.left_turns()):
            return ids
        if continuity.source_id not in ids:
            return ids
        return tuple(
            continuity.standalone_id if value == continuity.source_id else value
            for value in ids
        )

    def prepare_compaction_source(
        self, memory_service: MemoryServicePort, llm_runtime: LLMService, left_snapshot: LeftSnapshot, *,
        logical_scope_id: str, preferred_endpoint_id: str | None,
        preferred_model_id: str | None, active_turn_id: str,
        strategy: str, build_request: Callable[[str | None, str | None], LLMRequestIR],
        response_continuation: bool = False,
    ) -> CompactionSource:
        """Derive native and cached request material from Memory's frozen cut."""
        from pal.llm.compaction import is_native_window
        from pal.llm.ir import MessageRole
        from pal.llm.serde import message_from_payload
        from pal.memory.continuity import Continuity

        captured = tuple(message for turn in left_snapshot.turns for message in turn.messages)
        source_ids = tuple(message.message_id for message in captured)
        try:
            replay, dialect, wire_shape = self.compaction_replay(
                memory_service, llm_runtime=llm_runtime, logical_scope_id=logical_scope_id,
                preferred_endpoint_id=preferred_endpoint_id, preferred_model_id=preferred_model_id,
                include_active=bool(active_turn_id), active_turn_id=active_turn_id,
                left_message_ids=source_ids,
            )
        except Exception:
            replay, dialect, wire_shape = None, "", ""
        if replay is not None:
            preferred_endpoint_id = preferred_endpoint_id or str(replay.metadata.get("preferred_endpoint_id") or "").strip() or None
            preferred_model_id = preferred_model_id or str(replay.metadata.get("preferred_model_id") or replay.model_hint or "").strip() or None
            self.diagnostics.append({"kind": "two_segment_warm_replay_engaged",
                "scope": logical_scope_id, "run_id": left_snapshot.run_id,
                "dialect": dialect, "wire_shape": wire_shape})
        native_source = any(is_native_window(message) for message in captured)
        next_request = None
        native_window = strategy == "openai_responses" or native_source
        reference_binding = None
        if response_continuation and replay is None and not native_window:
            bindings = {(m.replay.wire_shape.value, m.replay.endpoint_id, m.replay.model_id)
                        for m in captured if m.replay}
            if len(bindings) == 1:
                shape, endpoint_id, model_id = bindings.pop()
                if (shape in {"gemini_interactions", "openai_response"}
                        and preferred_endpoint_id in {None, endpoint_id}
                        and preferred_model_id in {None, model_id}):
                    reference_binding = (shape, endpoint_id, model_id)
        if native_window or reference_binding:
            try:
                capabilities = self.capabilities(preferred_endpoint_id)
                if reference_binding:
                    shape, endpoint_id, model_id = reference_binding
                    if (capabilities.get("endpoint_id", endpoint_id) != endpoint_id
                            or capabilities.get("model_id", model_id) != model_id):
                        return CompactionSource(endpoint_id=preferred_endpoint_id, model_id=preferred_model_id)
                    preferred_endpoint_id, preferred_model_id = endpoint_id, model_id
                source_id_set = set(source_ids)
                messages, tail, projected_ids = [], [], []
                turns = memory_service.history_root.all_turns() if native_window else left_snapshot.turns
                for turn in turns:
                    projected = self.project_messages(list(turn.messages), turn_id=turn.turn_id,
                        artifact_scope_key=logical_scope_id, capabilities=capabilities)
                    for message in projected:
                        if message.message_id in source_id_set:
                            projected_ids.append(message.message_id)
                            messages.append(Continuity.from_message(message, {}).standalone_message()
                                if message.semantic_kind == "runtime_context_summary" else message)
                        else:
                            tail.append(message)
                if reference_binding and tuple(projected_ids) != source_ids:
                    return CompactionSource(endpoint_id=preferred_endpoint_id, model_id=preferred_model_id)
                shell = build_request(preferred_endpoint_id, preferred_model_id)
                stable = tuple(message for message in shell.messages if message.role in {MessageRole.SYSTEM, MessageRole.DEVELOPER})
                source_stable = stable
                for turn in reversed(left_snapshot.turns):
                    frozen = turn.metadata.get("prompt_context_state", {}).get("stable", ())
                    if frozen:
                        source_stable = tuple(message_from_payload(message) for message in frozen)
                        break
                if reference_binding:
                    from pal.memory.prompt_context import applicable_context
                    history = applicable_context((*source_stable, *messages), active_turn_id="")
                    replay = replace(shell, messages=tuple(history), logical_scope_id=logical_scope_id)
                    dialect, wire_shape = "canonical_lineage", reference_binding[0]
                else:
                    if replay is None or native_source:
                        replay = replace(shell, messages=(*source_stable, *messages))
                    next_request = replace(shell, messages=(*stable, *tail))
                    wire_shape, dialect = "openai_response", "canonical_history"
            except Exception as exc:
                if native_window:
                    raise CompactionSourceUnavailable(str(exc)) from exc
                self.diagnostics.append({"kind": "compaction_continuation_unavailable",
                    "run_id": left_snapshot.run_id, "reason": type(exc).__name__})
        return CompactionSource(replay, next_request, dialect, wire_shape,
                                preferred_endpoint_id, preferred_model_id)

    def compaction_replay(
        self,
        memory_service: Any,
        *,
        llm_runtime: Any,
        logical_scope_id: str,
        preferred_endpoint_id: str | None,
        preferred_model_id: str | None = None,
        include_active: bool = False,
        active_turn_id: str = "",
        left_message_ids: tuple[str, ...] | None = None,
    ) -> tuple[LLMRequestIR | None, str, str]:
        """Build a warm handoff request: frozen anchor + accepted suffix.

        Eligibility (PLAN 8.3, conservative by default):
        - the anchor must carry a known dialect/wire shape (W16);
        - the anchor's own endpoint/model binding must match the current
          preferred binding (W09);
        - no forced tool_choice (W14) and no provider-hosted tools (W13):
          the handoff must be able to answer plain JSON;
        - no image representations inside the anchor prefix: their refresh
          lifetime cannot be proven from the IR, so warm is refused rather
          than guessed (W18);
        - coverage proof (W02/W03): every eligible live L1 message must be
          covered exactly once by the anchor prefix plus the suffix.

        ``left_message_ids`` (v3 two-segment warm split): the ordered LEFT
        segment message ids from the history authority, captured after the
        compact run opened and fenced the cut.  When present, the anchor
        prefix must cover L exactly up to the anchor (prefix equality), the
        suffix carries only L content after the anchor, and the RIGHT side
        never enters the summary source (I10) — an anchor reaching past
        the cut refuses warm instead of leaking R.
        """
        # R3/S1 (review 95373ef): a locally eligible anchor is material for
        # building the same-source handoff — read-observed confirmation is
        # evidence about the PAST, not a permission for trying (a hit saves
        # the prefix, a miss costs exactly what the cold alternative would).
        # The confirmed reader stays available for diagnostics and explicit
        # hot-only surfaces; ordinary autocompact uses eligibility.
        reader = llm_runtime.prompt_cache_eligible_anchor_request
        try:
            replay = dict(
                reader(
                    logical_scope_id=logical_scope_id,
                    endpoint_id=str(preferred_endpoint_id or ""),
                )
                or {}
            )
        except Exception:
            return None, "", ""
        anchor_request = replay.get("request")
        anchor_message_id = str(
            replay.get("anchor_message_id") or ""
        ).strip()
        dialect = str(replay.get("dialect") or "").strip()
        wire_shape = str(replay.get("wire_shape") or "").strip()
        if not isinstance(anchor_request, LLMRequestIR) or not anchor_message_id:
            return None, "", ""
        if not dialect or not wire_shape:
            return None, "", ""
        left_ids: tuple[str, ...] | None = None
        model_left_ids: tuple[str, ...] | None = None
        if left_message_ids is not None:
            left_ids = tuple(str(value) for value in left_message_ids)
            if not left_ids:
                return None, "", ""
            # R3 (review 95373ef): the anchor's ids are the MODEL view of L
            # (the cut-owned seed appears as its standalone continuity
            # reference), while left_ids are raw authority coordinates.
            # Compare through the ONE canonical mapping — shared with the
            # normal compiler injection and the rebase seed view — instead
            # of raw-vs-model equality that refuses every two-segment warm.
            model_left_ids = self.left_model_ids(memory_service, left_ids)
            if anchor_message_id not in model_left_ids:
                # The anchor is not LEFT content: its cached prefix either
                # reaches past the cut into R or predates the segment.
                return None, "", ""
            anchor_conversation_ids = tuple(
                str(message.message_id)
                for message in anchor_request.messages
                if message.role.value not in {"system", "developer"}
            )
            anchor_position = model_left_ids.index(anchor_message_id)
            if anchor_conversation_ids != model_left_ids[: anchor_position + 1]:
                # W02/W03 prefix equality: the cached prefix must be L
                # exactly up to the anchor — no gap, no R, no extras.
                return None, "", ""
        anchor_endpoint = str(
            anchor_request.metadata.get("preferred_endpoint_id") or ""
        ).strip()
        if (
            preferred_endpoint_id
            and anchor_endpoint
            and str(preferred_endpoint_id) != anchor_endpoint
        ):
            return None, "", ""
        anchor_model = str(anchor_request.model_hint or "").strip()
        if (
            preferred_model_id
            and anchor_model
            and str(preferred_model_id) != anchor_model
        ):
            return None, "", ""
        if str((anchor_request.policy.tool_choice if anchor_request.policy is not None else 'auto') or "auto") != "auto":
            return None, "", ""
        for tool in list(anchor_request.tools or ()):
            if isinstance(tool, Mapping):
                tool_payload = dict(tool)
            else:
                tool_payload = {}  # ToolDefinitionIR exposes local tools only.
            if str(tool_payload.get("execute_on") or "").strip().lower() in {
                "server", "provider", "hosted",
            }:
                return None, "", ""
        for message in anchor_request.messages:
            for part in message.parts:
                if isinstance(part, ImagePartIR):
                    return None, "", ""

        try:
            memory_pack = memory_service.build_pack(
                MemoryPackRequest(turn_kind="chat", include_l1_recent_context=False)
            )
        except Exception:
            return None, "", ""
        if anchor_request.metadata.get("continuity_id", "") != memory_pack.metadata.get("continuity_id", ""):
            return None, "", ""
        capabilities = self.capabilities(
            preferred_endpoint_id
        )
        from pal.memory.context_view import projected_messages

        suffix: list[LLMMessageIR] = []
        anchor_found = False
        live_ids: list[str] = []
        left_id_set = set(left_ids) if left_ids is not None else None
        anchor_ids = {
            str(message.message_id) for message in anchor_request.messages
        }

        def project_into_suffix(messages: list[LLMMessageIR], *, settled: bool) -> None:
            nonlocal anchor_found
            for message in messages:
                message_id = str(message.message_id)
                if left_id_set is not None and message_id not in left_id_set:
                    # v3 I10: anything outside the fenced LEFT segment is
                    # right-side territory — it never enters the source.
                    continue
                live_ids.append(message_id)
                if anchor_found:
                    suffix.append(
                        replace(
                            message,
                            prompt_region=PromptRegionIR.ACTIVE_DYNAMIC,
                        )
                    )
                elif message.message_id == anchor_message_id:
                    anchor_found = True

        for settled_turn in list((memory_pack.l1_turns if memory_pack is not None else ()) or ()):
            projected = self.project_messages(
                list(projected_messages(settled_turn, settled=True)),
                turn_id=str((settled_turn.turn_id if settled_turn is not None else '') or ""),
                artifact_scope_key=logical_scope_id,
                capabilities=capabilities,
            )
            project_into_suffix(projected, settled=True)
        if include_active and active_turn_id:
            active_turn_reader = (memory_service.active_l1_turn if memory_service is not None else None)
            active_turn = (
                active_turn_reader(str(active_turn_id))
                if active_turn_reader is not None
                else None
            )
            if active_turn is not None:
                projected_active = self.project_messages(
                    list(projected_messages(active_turn, settled=False)),
                    turn_id=str(active_turn_id),
                    artifact_scope_key=logical_scope_id,
                    capabilities=capabilities,
                )
                project_into_suffix(projected_active, settled=False)
        if not anchor_found:
            return None, "", ""
        if left_ids is not None:
            # The raw/model substitution is 1:1 positionally, so the anchor
            # position is identical in both coordinate systems; the suffix
            # stays raw (live L1 projections carry raw ids).
            expected_suffix_ids = left_ids[
                model_left_ids.index(anchor_message_id) + 1 :
            ]
            suffix_ids = [str(message.message_id) for message in suffix]
            if tuple(suffix_ids) != tuple(expected_suffix_ids):
                # W02/W03 coverage in v3 form: every L id after the anchor
                # rides the suffix exactly once, in order — anything else
                # refuses warm instead of guessing.
                return None, "", ""
        # Coverage proof (W02/W03): the confirmed-anchor contract covers the
        # live order up to anchor_message_id; every eligible live message
        # strictly after the anchor must be carried by the suffix exactly
        # once (duplicated live ids refuse warm instead of guessing).
        duplicated = {
            message_id
            for message_id in live_ids
            if live_ids.count(message_id) > 1
        }
        if duplicated:
            return None, "", ""
        return (
            replace(
                anchor_request,
                messages=(*anchor_request.messages, *suffix),
            ),
            dialect,
            wire_shape,
        )
