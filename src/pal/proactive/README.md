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
- `BackgroundScheduleSource`

## Background execution and handoff

The built-in plugin admits occurrences durably into `proactive_runs` in the
same transaction that advances the schedule cursor. The original definition,
skills and route are frozen in the occurrence record. The shared background
workflow port starts one idempotent workflow under the system `__PROACTIVE__`
Task. Independent Tasks can run concurrently; this Task serializes scheduled
runs. Scheduler reload reattaches by run/workflow identity.

The dedicated `general.proactive_worker` profile uses the normal Bunshin LLM
broker, worktree, tool permissions, interaction and recovery protocols. It has
no independent verifier by default and cannot propose long-term memories.
Resident does not run an LLM turn at admission or when a workflow finishes.
There is no resident-turn execution fallback. Historical definitions and run
records remain readable without being re-executed.

The canonical business-data repository is `RUNTIME_ROOT/proactive`. Each
workflow gets an isolated Git worktree. Existing deployed business-data paths
are migrated by the offline runtime upgrade and remain compatibility links.
Memory conversation history is separate and can be captured as an immutable
read-only input when named in a scheduled task.

A workflow terminal result is appended to the durable background inbox, with
channel delivery tracked independently. The resident consumes pending neutral
results in its next ordinary user turn and checkpoints the consumption
identity with L1. No worker reasoning or model-native replay enters resident
history. Explicit result retrieval can consume the same receipt.

## Output destination tool

`set_proactive_destination(name, out_channel_name?, out_reply_target?)` replaces
the separate channel and target setters. At least one destination field must be
supplied; passing only the task name is rejected.

- Omit `out_channel_name` to retain the task's existing channel; target-only
  changes require an existing channel and use its destination validation.
- Supply a channel name to select it. Without a target, resolve the current
  conversation on that channel or its unique default; never carry a target
  across channels.
- For a silent task, select the `null` endpoint supplied by the built-in
  `null_channel` plugin: `set_proactive_destination(name=TASK,
  out_channel_name="null")`. Output is discarded by the channel, independently
  of the model's wording or invisible characters. This is the string `"null"`,
  distinct from JSON null, which clears the destination. Run history and normal
  execution/approval requirements still apply; a sink cannot host a review UI.
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

## Internal silent once handoffs

The stable shared `SilentOnceSchedulerPort` publishes
`ensure_silent_once(proactive_id, goal, method)`. It persists an immediately due
`once` definition targeting `null`; existing ids are preserved, including completed
or intentionally disabled tasks. Dreaming uses Core’s generic silent-task facility for failed dreaming handoffs. The
workflow produces a model-neutral result, which the resident consumes through
the same durable inbox at its next normal turn. Task definitions/cursors remain
in the existing proactive repository. This port's identity survives plugin reload.

The event source owns a timer scope and translates schedule deadlines and pending
workflow reconciliation into one-shot Core timers. Memory maintenance pauses
admission. Plugin unload cancels timer ownership; it does not cancel the
independent Bunshin workflow.
Handlers use the public Core control port, without accessing Core state or its
turn manager.
