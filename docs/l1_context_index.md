# Active round records and immutable request visibility

`L1TurnStore` owns an immutable history and the current streaming round of each
active turn. `MemoryService.stream_l1_assistant` updates that round without
materializing a whole-turn snapshot. Every fragment is still recorded in L1;
`active_l1_turn`, snapshots and exports immediately see its latest revision.
Previously returned `L1TurnIR` objects never change.

Stream validation visits the current assistant message only. Frozen predecessors'
call IDs are collected once when the round starts, for duplicate detection. A
new response cannot start with unresolved tool calls or overwrite an earlier
round. Tool-result transactions, explicit snapshot reads, interruption and
restoration retain full snapshot validation. These boundaries may traverse the
selected turn; normal text/reasoning fragments do not. Settling or replacing a
turn drops its streaming state. No per-history message/result index is retained.

The public `turns` sequence remains read-only. Runtime snapshot schema stays at
version 1: active buffers are materialized into the existing messages schema;
restore reconstructs active round state on the next update. Transaction rollback
installs the previous immutable snapshot and invalidates derived state together.

`MemoryService.l1_context_view(active_turn_id, settled_turns)` selects exactly the
history used by a request. This request-scoped immutable view supports
`contains(turn_id, message_id)`, `tool_result(turn_id, call_id)` and
`state_proof(namespace, key, revision)`. Call IDs are scoped by turn. Stored
messages excluded by context projection or expired instruction scope are not
visible delivery proof. Repeated reads with identical source snapshots reuse the
view, including the execution hook's no-change path.

Prompt assembly prepares ordinary scoped context, then passes the view to
`prepare_model_context`. Extensions may append ready facts and their coverage
atomically; they cannot rewrite past shell results or perform network I/O here.
Ordinary stream updates invalidate the request view without constructing another.
History is traversed when a request needs it, not on each streaming fragment.

Tests cover 100 frozen rounds followed by 1,000 updates (one-message protocol
validation per update), immutable snapshots, duplicate rejection, rollback,
restoration, scoped visibility and view reuse in `test_l1_context_index.py`.
