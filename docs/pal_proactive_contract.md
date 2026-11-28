# Pal Proactive Contract

> Defines the `proactive` subsystem: durable future-action definitions, due triggers, run history, and optional output routing.

## Purpose

`proactive` owns actions Pal should perform across time:

- one-shot reminders
- recurring schedules
- periodic checks
- planned recaps or briefings
- proactive push work

Proactive owns scheduling, not execution or channel transport. Each due occurrence is executed by Bunshin under the system Task `__PROACTIVE__`.

## Owns

- `ProactiveDefinition`
- schedule state and due-trigger materialization
- `ProactiveRun` lifecycle records
- output channel and reply-target selection

## Does Not Own

- channel adapters or delivery mechanics
- tool execution runtime
- bunshin worker lifecycle
- memory truth
- governance and approval policy

## ProactiveDefinition

A proactive definition is a durable future-action contract.

Current fields:

- `proactive_id`: stable identifier
- `goal`: what Pal should accomplish
- `method`: optional execution guidance
- `skill_refs`: optional skill/manual references
- `out_channel_id`: optional channel endpoint for emitted output
- `out_reply_target`: optional channel-specific reply metadata
- `schedule`: scheduling config
- `enabled`: whether due triggers should be generated

Supported schedule shapes are implemented by `pal.proactive.scheduling`:

- `{"cadence": "manual"}`: no automatic due time
- `{"cadence": "once", "run_at_utc": "..."}`
- `{"cadence": "daily", "hour": 9, "minute": 0, "timezone": "Asia/Shanghai"}`
- `{"cadence": "cron", "cron": "0 9 * * *", "timezone": "UTC"}`

## ProactiveRun

A run records one durable schedule occurrence and its associated workflow.

Current fields:

- `proactive_run_id`
- `proactive_id`
- `trigger_kind`
- `status`
- `trigger_metadata`
- `turn_id`
- `output_summary`
- `error_text`
- `started_at`
- `completed_at`

## Channel Output

When a channel is configured, the scheduler resolves its destination before
workflow admission, freezes the route in the dispatch request and lets Bunshin
publish the terminal result. A configured destination that cannot be resolved
is recorded as a failure rather than silently discarded. Admission retries
reuse the original run identity and routing request.

When no channel is configured, execution still produces a durable result for
Resident. The string `"null"` explicitly discards channel output.

Run inspection reports `completion_scope=workflow_execution` for new runs;
historical resident records retain `turn_execution`. Workflow completion is
not a channel delivery receipt. Delivery status is tracked independently in the
background inbox and workflow delivery records.

## Tables

Current truth tables:

- `proactive_definitions`
- `proactive_runs`

Only proactive table and identifier names are active. Hot/top-of-mind state belongs in runtime memory, not a durable identity table.

## Capabilities

Current public aliases are:

- `inspect_proactive_status`
- `list_proactive_tasks`
- `read_proactive_task(name)`
- `list_proactive_runs(name)`
- `upsert_proactive_task`
- `manage_proactive_task(name, operation="enable"|"disable"|"delete")`
- `set_proactive_destination(name, out_channel_name?, out_reply_target?)`
- `update_proactive_schedule`

Destination changes persist channel and target together. Omit the channel to
retain it; JSON null clears channel and target; an empty target object clears
only the target. At least one destination field must be supplied. Invalid
resolution or persistence failure leaves the definition unchanged.

For silent work select the built-in sink with
`set_proactive_destination(name=TASK, out_channel_name="null")`. This is the
string `"null"`, not JSON null. The sink discards output without transport/replay;
run history and normal effect/approval rules remain. Do not ask a model to emit
an invisible character to simulate silence, or use the recovery socket as a sink.

## Admission And Context

Due occurrences and schedule cursors are persisted atomically before dispatch.
The scheduler submits one Direct workflow per occurrence; the system Task
allows at most one active workflow. It can execute independently of Resident
chat, and recovers by persisted run/workflow identity.

Worker tool results and native replay remain in Bunshin. A model-neutral result
enters Resident L1 only at the next ordinary user turn, with its receipt
identity checkpointed before acknowledgement. Historical L1 and run records are
not rewritten. See the [subsystem README](../src/pal/proactive/README.md).

## Invariants

- A proactive definition must describe what to do; `goal` is required.
- Disabled definitions do not generate due triggers.
- Due triggers are materialized into the main loop as `ProactiveTriggerEvent`.
- Execution side effects go through the normal Bunshin worker and execution module.
- Run history is durable; transcript and memory projection remain owned by memory.
