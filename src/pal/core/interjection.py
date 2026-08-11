from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from typing import Any

from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.shared.payloads import extract_text_from_payload


async def inject_pending_interjection_async(
    *,
    context: Any,
    state: Any,
    continuation: Any,
) -> None:
    """Append one queued Pal channel message to L1 after a tool batch.

    Routing metadata on the queued envelope is deliberately ignored: reply
    authority remains with the message that opened ``continuation``.
    """

    if not state.pending_channel_turns:
        return
    async with state.channel_turn_transition_lock:
        if not state.pending_channel_turns:
            return
        pending = state.pending_channel_turns[0]
    envelope = pending

    try:
        payload = getattr(getattr(envelope, "event", None), "payload", None)
        if isinstance(payload, LLMMessageIR):
            message = replace(payload, semantic_kind="user_interjection")
        else:
            text = extract_text_from_payload(payload).strip()
            if not text:
                return
            message = LLMMessageIR(
                role=MessageRole.USER,
                parts=(TextPartIR(text),),
                message_id=str(getattr(getattr(envelope, "event", None), "event_id", "") or "interjection"),
                semantic_kind="user_interjection",
            )
        if not message.parts:
            return
        memory_service = context.port_registry.get("memory:memory")
        append_user = getattr(memory_service, "append_l1_user", None)
        if not callable(append_user):
            return

        async def append_and_acknowledge() -> None:
            async with state.channel_turn_transition_lock:
                if (
                    not state.pending_channel_turns
                    or state.pending_channel_turns[0] is not pending
                ):
                    return
                try:
                    result = append_user(str(continuation.turn_id), message)
                    if inspect.isawaitable(result):
                        await result
                except (Exception, asyncio.CancelledError):
                    committed = await _l1_contains_message_async(
                        memory_service,
                        turn_id=str(continuation.turn_id),
                        message_id=message.message_id,
                    )
                    if not committed:
                        raise
                # Append and acknowledgement share the channel transition
                # lock. A cancelled caller may leave this task running, but
                # the normal next-turn path cannot dequeue the same envelope
                # between the durable L1 write and this acknowledgement.
                del state.pending_channel_turns[0]

        commit = asyncio.create_task(append_and_acknowledge())
        committed = False
        try:
            await asyncio.shield(commit)
            committed = True
        except asyncio.CancelledError:
            # The append is idempotent by message_id. Let append+ack finish so
            # cancellation can never strand the message between L1 and queue.
            try:
                await asyncio.shield(commit)
                committed = True
            except Exception:
                pass
            if committed:
                await _acknowledge_cross_scope_interjection_async(
                    context=context,
                    envelope=envelope,
                    continuation=continuation,
                )
            raise
        if committed:
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

    source_binding = getattr(envelope, "opening_delivery_binding", None)
    active_binding = getattr(continuation, "delivery_binding", None)
    if source_binding is None or active_binding is None:
        return
    if str(source_binding.control_scope_key) == str(active_binding.control_scope_key):
        return
    output_port = context.port_registry.get("agent_io:output") or context.port_registry.get(
        "channel:channel"
    )
    queue_reply = getattr(output_port, "queue_reply", None)
    if not callable(queue_reply):
        return
    try:
        result = queue_reply(
            source_binding,
            "Message added to the active conversation; its response will be delivered there.",
        )
        if inspect.isawaitable(result):
            await result
    except Exception:
        # The interjection is already durable. A secondary-channel receipt
        # failure must not duplicate it by restoring the original envelope.
        return


async def _l1_contains_message_async(
    memory_service: Any,
    *,
    turn_id: str,
    message_id: str,
) -> bool:
    contains = getattr(memory_service, "contains_l1_message", None)
    if callable(contains):
        try:
            result = contains(turn_id, message_id)
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        except Exception:
            pass

    # Compatibility fallback for small host/test memory ports. This query is
    # only used after append raised, to distinguish "failed before commit"
    # from "committed, then the adapter raised".
    active_turn = getattr(memory_service, "active_l1_turn", None)
    if not callable(active_turn):
        return False
    try:
        turn = active_turn(turn_id)
        if inspect.isawaitable(turn):
            turn = await turn
        return any(
            str(getattr(item, "message_id", "") or "") == message_id
            for item in (getattr(turn, "messages", ()) or ())
        )
    except Exception:
        return False


__all__ = ["inject_pending_interjection_async"]
