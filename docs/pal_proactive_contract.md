# Pal Proactive Contract

> Defines the `proactive` subsystem: durable future-action definitions, due triggers, run history, and optional output routing.

## Purpose

`proactive` owns actions Pal should perform across time:

- one-shot reminders
- recurring schedules
- periodic checks
- planned recaps or briefings
- proactive push work

It is not minion tasking and it is not channel transport. A proactive task may use normal Pal tools or minions during execution, but those remain owned by their modules.

## Owns

- `ProactiveDefinition`
- schedule state and due-trigger materialization
- `ProactiveRun` lifecycle records
- output channel and reply-target selection

## Does Not Own

- channel adapters or delivery mechanics
- tool execution runtime
- minion worker lifecycle
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

## Tables

Current truth tables:

- `proactive_definitions`
- `proactive_runs`

Only proactive table and identifier names are active. Hot/top-of-mind state belongs in runtime memory, not a durable identity table.

## Capabilities

Management actions are exposed under proactive capability names:

- `op_proactive_mgmt_create`
- `op_proactive_mgmt_destroy`
- `op_proactive_mgmt_enable`
- `op_proactive_mgmt_disable`
- `op_proactive_mgmt_set_output_channel`
- `op_proactive_mgmt_set_output_target`
- `op_proactive_mgmt_update_schedule`

Introspection actions expose module status, task detail, latest run, and run history.

## Invariants

- A proactive definition must describe what to do; `goal` is required.
- Disabled definitions do not generate due triggers.
- Due triggers are materialized into the main loop as `ProactiveTriggerEvent`.
- Execution side effects still go through the normal Pal turn runtime and execution module.
- Run history is durable; transcript and memory projection remain owned by memory.
