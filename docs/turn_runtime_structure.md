# Turn Runtime Structure

This document describes the current turn-runtime skeleton and the code paths to
read when debugging a Pal conversation turn.

## Reading Order

Recommended entry points:

1. [core/runtime.py](../src/pal/core/runtime.py)
   - `PalCore`
   - `TurnManager`
   - `MainLoop`
2. [core/turns.py](../src/pal/core/turns.py)
   - turn programs
   - effect requests/results
   - channel turn program
3. [channel/runtime.py](../src/pal/channel/runtime.py)
   - mailbox
   - outbox
   - reply delivery boundary
4. [core/tool_stagnation.py](../src/pal/core/tool_stagnation.py)
   - repeated-tool-loop detection
   - finalization-only transition
5. [llm/contracts.py](../src/pal/llm/contracts.py) and
   [memory/contracts.py](../src/pal/memory/contracts.py)
   - turn-time preflight, compact, and commit contracts

## Main Data Flow

A normal user message follows this path:

1. A channel provider normalizes platform input.
2. The normalized `ChannelEnvelope` enters the channel mailbox.
3. `MainLoop` polls mailbox-backed sources.
4. `TurnEventHandler` passes conversational ingress to
   `PalCore.process_channel_turn(...)`.
5. Runtime-private control ingress, such as slash commands, is handled
   deterministically and does not enter the LLM prompt or L1.
6. `TurnManager` creates and drives a generator-style `TurnProgram`.
7. The turn program yields effects such as:
   - LLM preflight
   - memory compaction when required
   - LLM request
   - tool calls
   - mailbox reply
8. A successful `mailbox.reply` write completes the turn egress boundary.
9. `PalCore` performs post-turn L1 commit.
10. Actual channel delivery later emits `reply.delivered` or `reply.failed`.

Important consequences:

- A turn waits for the channel outbox to accept the final reply, not for remote
  platform delivery.
- Delivery events are channel-side diagnostics.
- Recognized slash control commands are not conversational input.
- Slash-like text with no registered command match is conversational input.
- The LLM can observe governance state, not raw matched control command text.

## Key Responsibilities

### `src/pal/core/runtime.py`

Owns runtime orchestration:

- polling sources
- dispatching mailbox events
- driving turn continuations
- assembling prompts
- invoking LLM, memory, and execution effects
- queueing final replies
- committing L1 after the turn

### `src/pal/core/turns.py`

Defines the turn computation contract:

- `TurnProgram`
- `TurnContinuation`
- `EffectRequest`
- `EffectResult`
- `channel_turn_program(...)`

The turn program expresses the ordered effect chain without owning concrete
runtime services.

### `src/pal/channel/runtime.py`

Defines the channel ingress/egress boundary:

- mailbox stores normalized internal events
- outbox stores final replies pending delivery
- `queue_reply(...)` writes completed replies
- `flush_outbox(...)` attempts real platform delivery

### `src/pal/core/tool_stagnation.py`

Detects tool-loop stagnation:

- repeated same tool call with same canonical arguments/result
- oscillation between equivalent failed attempts
- need to terminate tool use and force finalization

## Tool Call Loop Semantics

When the model emits tool calls, Pal preserves the provider protocol shape:

1. The assistant tool-call header is kept in the transcript, even if its text
   content is empty.
2. Tool calls from the same assistant message are executed by Pal.
3. Tool results are appended after execution.
4. The next LLM request receives the assistant tool-call header plus all tool
   results for that batch.

This avoids orphan tool results and preserves the exact causal relationship
between assistant tool intent and tool output.

Current execution is sequential inside a tool-call batch. The transcript update
is batch-shaped: Pal flushes back to the LLM after the batch's pending tool
results are available.

## Finalization-Only Mode

If `ToolStagnationGuardProcess` decides that the tool loop is stuck, Pal switches
the current turn to `finalization_only`.

Effects:

1. The next LLM request has tools physically removed.
2. Prompt assembly adds a finalization directive.
3. Execution rejects new tool calls for that finalization attempt.
4. The model gets one text-only chance to conclude.
5. If it still fails to conclude, Pal produces a runtime fallback final reply.

This guarantees that a turn can close even when the model/tool loop becomes
unproductive.

## Current Non-Stub Areas

The runtime is no longer just a skeleton:

- LLM generation and streaming use real endpoint invocation paths.
- Tool call/results are persisted into the turn transcript in protocol order.
- Memory compaction and L1 commit are active runtime effects.
- Channel mailbox/outbox boundaries are implemented.
- Recognized control slash commands bypass the LLM path. Slash-like text with no registered command match is re-emitted as an ordinary `user.message`.

Remaining areas may still be intentionally minimal or provider-dependent:

- detailed approval UX
- provider-specific channel delivery behavior
- full cross-provider streaming parity
- richer memory compaction policies

## Common Confusions

- `active_turns` stores continuations, not the full business state for a turn.
- `reply.delivered` and `reply.failed` do not decide whether the turn completed.
- Prompt assembly still goes through prompt fragment providers.
- Channel inbox is a normalized mailbox view, not the raw adapter input queue.
- The stagnation guard is a separate process, not hard-coded inline branching.
- Provider wire shape is rendered by `llm_adaptor`, not by changing L1 records.
