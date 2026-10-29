# Turn Runtime Structure

This document maps current turn execution. Pal and Bunshin use the same
`AgentTurnRuntime` kernel with host-owned ports and task policy. Resident
admission remains a separate Core scheduler responsibility.

## Reading Order

Recommended entry points:

1. [core/agent_turn_runtime.py](../src/pal/core/agent_turn_runtime.py)
   - shared prompt assembly, history/projection and effect execution seams
2. [core/runtime.py](../src/pal/core/runtime.py)
   - `PalCore`
   - `TurnManager`
   - `MainLoop`
3. [core/turns.py](../src/pal/core/turns.py)
   - turn programs
   - effect requests/results
   - channel turn program
4. [channel/runtime.py](../src/pal/channel/runtime.py)
   - mailbox
   - outbox
   - reply delivery boundary
5. [core/tool_stagnation.py](../src/pal/core/tool_stagnation.py)
   - repeated-tool-loop detection
   - finalization-only transition
6. [llm/contracts.py](../src/pal/llm/contracts.py) and
   [memory/contracts.py](../src/pal/memory/contracts.py)
   - turn-time preflight, compact, and commit contracts

## Main Data Flow

A normal user message follows this path:

1. A channel provider normalizes platform input.
2. The normalized `ChannelEnvelope` enters the channel mailbox.
3. `MainLoop` drains mailbox-backed sources after async wakeups; it does not
   poll module-private queues on an idle timer.
4. `TurnEventHandler` passes conversational ingress to
   `PalCore.process_channel_turn(...)`.
5. Runtime-private control ingress, such as slash commands, is handled
   deterministically and does not enter the LLM prompt or L1.
6. Core checks resident admission and `TurnManager` drives the generator-style
   `TurnProgram` under the shared agent kernel. Scheduler ownership continues
   through effect completion, cancellation and teardown.
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

### `src/pal/core/agent_turn_runtime.py`

Owns the shared agent kernel used by resident and Bunshin hosts. Host policy
provides task-source semantics and ports, rather than a second prompt compiler
or turn executor. Compaction uses frozen LEFT, shared continuity policy and
transactional installation; pending RIGHT remains outside the replacement.

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

## Admission, Side Turns And Proactive Work

All normal channel messages share one resident mailbox, conversation and
execution lifetime. The opening message's `TurnDeliveryBinding` retains reply
authority. Later interjections do not create per-channel agent scopes.

The [resident scheduler](core_turn_scheduling.md) admits chat, proactive and
background completion work. Proactive waits while resident work is busy and
queued user input takes priority. Admitted proactive runs use isolated model
context; they cannot compact resident history to make room for themselves.

`/btw` uses an immutable captured L and independent temporary history/execution
state. It does not claim the resident writer or publish into resident L1.
Reset, compaction, maintenance and shutdown have their own admission barriers;
program completion alone is not idle while teardown is pending.

## Accepted History And Recovery

LLM codecs and response hooks keep provider drafts private until validated IR
is accepted. Accepted L1 messages, including native replay, are the response
truth; projection send receipts prove admission only. Rebuilding a projection
must derive from accepted history, not a separate pre-hook response capture.

Resident admission and completed-round boundaries save encrypted recovery.
Restart restores interrupted context without replaying tools. Compact/reset
archive removed dialogue separately; tool-output files remain governed by L1
references and Execution's delivery/permission transaction.

## Common Confusions

- `active_turns` stores continuations; scheduler phases/barriers own admission
  and stay busy through teardown. Neither is workflow business truth.
- `reply.delivered` and `reply.failed` do not decide whether the turn completed.
- Prompt assembly still goes through prompt fragment providers.
- Channel inbox is a normalized mailbox view, not the raw adapter input queue.
- The stagnation guard is a separate process, not hard-coded inline branching.
- Provider wire shape is rendered by `llm.shapes`, not by changing L1 IR.
