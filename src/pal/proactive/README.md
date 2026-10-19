# proactive

Owns:
- proactive task definition and run semantics
- schedule boundary
- future output delivery target selection

Does not own:
- channel transport
- tool execution runtime
- worker lifecycle
- memory truth

Exposes:
- `ProactiveDefinition`
- `ProactiveTriggerEvent`
- `ProactiveManager`
- `ScheduleEngine`
- `ProactiveEventSource`

## Context and handoff

Triggers wait in the existing mailbox until core admits a resident turn.
Running/finalizing chat, dreaming, quiescence and compaction apply backpressure;
pending user input has priority. Admission is rechecked after event dispatch.
No run record, L1 turn or user reply is created before admission. While a
proactive turn runs, new user input remains queued for the next chat turn.

Each run uses a new model-cache scope and sees only its trigger and its own
active L1 turn. It does not consume queued user interjections or reuse the
resident history projection, including the projection's left-history bootstrap.
The ordinary codec sends the complete isolated request on every round.

Tool calls, results and output-snapshot references already belong to the shared
L1 store. Closing the turn makes these records available to later ordinary chat
requests; no summary-only handoff or duplicate transcript copy is needed.
Artifact ownership and file version/edit authority remain resident-owned.
File-read suppression additionally requires the source results to be visible in
the requesting model context, so another run's reads cannot hide file content.

A proactive run that exceeds its own context budget stops with its work recorded.
It must not compact the resident conversation to make space for an isolated
request. Independent proactive compaction is not implemented by this path.

## Output destination tool

`set_proactive_destination(name, out_channel_name?, out_reply_target?)` replaces
the separate channel and target setters. At least one destination field must be
supplied; passing only the task name is rejected.

- Omit `out_channel_name` to retain the task's existing channel; target-only
  changes require an existing channel and use its destination validation.
- Supply a channel name to select it. Without a target, resolve the current
  conversation on that channel or its unique default; never carry a target
  across channels.
- Explicit `out_channel_name: null` clears the channel and target together.
  A nonempty target cannot accompany that clear.
- Explicit `out_reply_target: {}` clears only the target, retaining the selected
  channel. This does not resolve a replacement default. Target null is invalid.

Resolution verifies the endpoint even for an empty target. Both fields are
persisted together before publishing the new task definition. Invalid input,
unresolved destinations and persistence failure leave the definition unchanged.
The tool preserves the goal, method, skill references, enabled flag and schedule
configuration; it does not send a message. Creation/replacement and schedule
updates remain separate tools.

## Task control tool

`manage_proactive_task(name, operation)` consolidates enable, disable and delete.
Both fields are required; `operation` is one of `enable`, `disable`, `delete`.

- `disable` pauses future scheduling while retaining the task and run history.
  It does not cancel a run already in progress.
- `enable` resumes future scheduling.
- `delete` permanently removes the task definition.

The facade delegates to the existing task-owner operations and preserves their
persistence/error behavior. The three old tool aliases are no longer published.
Creation/replacement, destination updates and schedule updates remain separate
tools; this control tool takes no operation-specific extra parameters.
