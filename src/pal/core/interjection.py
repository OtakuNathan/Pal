from __future__ import annotations

import asyncio
import inspect
import hashlib
import json
from dataclasses import replace
from typing import Any

from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.core.compaction_coordinator import compaction_gate_active
from pal.memory.persistence.contracts import RESIDENT_HISTORY
from pal.shared.payloads import extract_text_from_payload


async def inject_pending_interjection_async(
    *,
    context: Any,
    state: Any,
    continuation: Any,
) -> None:
    """Append the queued snapshot as one ordered user message after a tool batch.

    Routing metadata on the queued envelope is deliberately ignored: reply
    authority remains with the message that opened ``continuation``.
    """

    if not state.pending_channel_turns:
        return
    async with state.channel_turn_transition_lock:
        if not state.pending_channel_turns:
            return
        # Compaction admission door 1 (snapshot point): while a compaction
        # ticket holds this scope, queued input stays queued (Q03). It will
        # be admitted after release, before the next ordinary LLM request.
        if compaction_gate_active(state):
            return
        snapshot = tuple(state.pending_channel_turns)

    try:
        batch = []
        messages = []
        for envelope in snapshot:
            payload = envelope.event.payload
            if isinstance(payload, LLMMessageIR):
                message = replace(payload, semantic_kind="user_interjection")
            else:
                text = extract_text_from_payload(payload).strip()
                if not text:
                    break
                message = LLMMessageIR(
                    role=MessageRole.USER,
                    parts=(TextPartIR(text),),
                    message_id=envelope.event.event_id,
                    semantic_kind="user_interjection",
                )
            # Leave unsupported entries and their successors for normal queue
            # processing; never silently discard them or change FIFO order.
            if not message.parts or message.role != MessageRole.USER:
                break
            if context.port_registry.get(RESIDENT_HISTORY.name) is not None:
                message = replace(message, metadata={**dict(message.metadata),
                    "conversation_origin": "user", "conversation_timestamp": envelope.event.created_at})
            batch.append(envelope)
            messages.append(message)
        if not messages:
            return
        message = messages[0]
        if len(messages) > 1:
            source_ids = [item.message_id for item in messages]
            digest = hashlib.sha256(json.dumps(source_ids).encode()).hexdigest()
            parts = []
            for item in messages:
                if parts:
                    parts.append(TextPartIR("\n\n"))
                parts.extend(item.parts)
            message = LLMMessageIR(
                role=MessageRole.USER,
                parts=tuple(parts),
                message_id=f"interjection-batch:{digest}",
                semantic_kind="user_interjection",
                metadata={"interjection_sources": [
                    {"message_id": item.message_id, "metadata": dict(item.metadata)}
                    for item in messages
                ]},
            )
            if context.port_registry.get(RESIDENT_HISTORY.name) is not None:
                sources = [{"message_id": item.message_id, "metadata": dict(item.metadata),
                            "conversation_text": "".join(part.text for part in item.parts if isinstance(part, TextPartIR))}
                           for item in messages]
                message = replace(message, metadata={**dict(message.metadata), "interjection_sources": sources})
        memory_service = context.port_registry.get("memory:memory")
        if memory_service is None:
            return

        async def append_and_acknowledge() -> bool:
            async with state.channel_turn_transition_lock:
                # Compaction admission door 2 (final append+ack): a ticket
                # claimed after the snapshot must block this append; the
                # batch stays queued and enters the compacted context later.
                if compaction_gate_active(state):
                    return False
                if (
                    len(state.pending_channel_turns) < len(batch)
                    or any(current is not expected for current, expected in
                           zip(state.pending_channel_turns, batch))
                ):
                    return False
                try:
                    await memory_service.append_interjection(str(continuation.turn_id), message)
                except (Exception, asyncio.CancelledError):
                    committed = memory_service.contains_l1_message(str(continuation.turn_id), message.message_id)
                    if not committed:
                        raise
                # Append and acknowledgement share the channel transition
                # lock. A cancelled caller may leave this task running, but
                # the normal next-turn path cannot dequeue the same envelope
                # between the durable L1 write and this acknowledgement.
                for _ in batch:
                    state.pending_channel_turns.popleft()
                # Publish only after the durable append and queue acknowledgement.
                # Presentation observers cannot alter admission or cause a retry.
                for admitted in batch:
                    source = admitted.opening_delivery_binding
                    active = continuation.delivery_binding
                    if source is None or active is None:
                        continue
                    try:
                        context.core_event_bus.emit("turn.input_admitted", {
                            "turn_id": str(continuation.turn_id),
                            "endpoint_id": str(source.endpoint.endpoint_id),
                            "request_id": str(source.response_handle.reply_target.get("request_id") or ""),
                            "reply_endpoint_id": str(active.endpoint.endpoint_id),
                            "reply_request_id": str(active.response_handle.reply_target.get("request_id") or ""),
                            "scope_key": str(active.control_scope_key),
                        })
                    except Exception:
                        pass
                return True

        commit = asyncio.create_task(append_and_acknowledge())
        committed = False
        try:
            committed = await asyncio.shield(commit)
        except asyncio.CancelledError:
            # The append is idempotent by message_id. Let append+ack finish so
            # cancellation can never strand the message between L1 and queue.
            try:
                committed = await asyncio.shield(commit)
            except Exception:
                pass
            if committed:
                for envelope in batch:
                    await _acknowledge_cross_scope_interjection_async(
                        context=context,
                        envelope=envelope,
                        continuation=continuation,
                    )
            raise
        if committed:
            for envelope in batch:
                await _acknowledge_cross_scope_interjection_async(
                    context=context,
                    envelope=envelope,
                    continuation=continuation,
                )
    except Exception:
        # Leave the unacknowledged head in place for the normal queue flow.
        return


async def _acknowledge_cross_scope_interjection_async(
    *,
    context: Any,
    envelope: Any,
    continuation: Any,
) -> None:
    """Finish a consumed request whose reply authority belongs to another scope."""

    source_binding = envelope.opening_delivery_binding
    active_binding = continuation.delivery_binding
    if source_binding is None or active_binding is None:
        return
    if str(source_binding.control_scope_key) == str(active_binding.control_scope_key):
        return
    output_port = context.port_registry.get("agent_io:output") or context.port_registry.get(
        "channel:channel"
    )
    if output_port is None:
        return
    try:
        result = output_port.queue_reply(
            source_binding,
            "Message added to the active conversation; its response will be delivered there.",
        )
        if inspect.isawaitable(result):
            await result
    except Exception:
        # The interjection is already durable. A secondary-channel receipt
        # failure must not duplicate it by restoring the original envelope.
        return


__all__ = ["inject_pending_interjection_async"]
