# Pal Proactive Contract

> Defines the `proactive` subsystem: durable future-action definitions, due triggers, run history, and optional output routing.

## Purpose

`proactive` owns actions Pal should perform across time:

- one-shot reminders
- recurring schedules
- periodic checks
- planned recaps or briefings
- proactive push work

It is not bunshin tasking and it is not channel transport. A proactive task may use normal Pal tools or bunshins during execution, but those remain owned by their modules.

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

A run records one concrete execution attempt.

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

If `out_channel_id` is set, proactive execution resolves it through the channel runtime and emits intermediate/final output through that endpoint. `out_reply_target` is merged with endpoint defaults and trigger metadata, which allows a proactive task to reply into a specific socket request, Telegram thread, or other endpoint-specific target.

If `out_channel_id` is unset, the proactive run still executes and records history, but it does not emit channel output.

A configured output channel that cannot be resolved is a run failure, not an
implicit request to execute without output. Failures during run creation, turn
execution, cleanup, and completion recording retain their exception chains.
If durable failure recording also fails, run inspection exposes the in-process
`runtime_failures` fallback for the current runtime lifetime.

Run status `completed` confirms turn execution only. Run inspection explicitly
reports `completion_scope=turn_execution` and
`delivery_confirmation=not_recorded`; it is not a channel delivery receipt.
Asynchronous delivery failures remain channel-owned failure events.

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

Due triggers wait in the existing mailbox until resident admission; no run row
or L1 turn opens before admission. Pending user input has priority. Proactive
runs use their own model-cache scope and isolated context, not the resident
history projection or queued interjections. New user input waits for the next
chat turn while proactive owns the resident slot.

Tool calls, results and output references use the shared L1 store and are visible
to later ordinary chat after turn closure. An isolated proactive context-budget
failure does not compact the resident conversation. See the
[subsystem README](../src/pal/proactive/README.md) for destination validation and
context/handoff details.

## Invariants

- A proactive definition must describe what to do; `goal` is required.
- Disabled definitions do not generate due triggers.
- Due triggers are materialized into the main loop as `ProactiveTriggerEvent`.
- Execution side effects still go through the normal Pal turn runtime and execution module.
- Run history is durable; transcript and memory projection remain owned by memory.
