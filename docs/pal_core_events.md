# Core system observations

`MainContext.core_event_bus` owns system observations and their current-state
snapshot. `turn_event_bus` and `pal.core.turn_events.TurnEventBus` remain aliases
for existing integrations; there is only one bus. New code imports topics from
`pal.core.core_events`.

This is a best-effort observation API, not the Core work queue. It does not enqueue
LLM turns, change failure policy, acknowledge deliveries, or write L1/history.
Memory requests sleep through Core's maintenance interface; Core publishes the
actual transition. Channel subscribes and projects it, rather than owning sleep.
The normal dreaming chat notices and `REPLY_FAILED` recovery flow remain separate.

## Topics

| Topic | Meaning and fields |
| --- | --- |
| `turn.start`, `turn.end` | Existing turn boundaries and routing fields; end includes `success`, `failed`, or `interrupted`. |
| `turn.tool_call_before` | Tool execution starts; `subsystem=execution`, `component`, `tool_name`, `call_id`, optional `turn_id`. |
| `turn.tool_call_failed` | Raw tool result is unsuccessful, **before** possible failure-flow escalation. Same identity fields, `ok=false`. |
| `turn.tool_call_after` | Existing result processing completion with `ok`. Do not also play a failure animation from this event when consuming `tool_call_failed`. |
| `failure.started`, `failure.finished` | Failure handling begins/ends, including report-only paths. `failure_id`, `subsystem`, `component`, `failure_kind`, `severity`, `related_ids`; finish includes verification status or `interrupted`. |
| `failure.safe_mode_started`, `failure.safe_mode_finished` | Actual inline safe-mode repair begins/ends, sharing the failure identity. LLM/persistence report-only paths do not emit these. |
| `memory.sleep` | Actual sleeping transition after maintenance admission and successful required notice, or after service recovery; `subsystem=memory`, `component=dreaming`, `sleeping`. |

Safe-mode tools use the same tool topics with `phase=safe_mode` and `scope_id`.
A turn ID is optional: channel delivery recovery can happen after a turn has ended.
Component identifies the observed failure location, not a proven root cause.
Broadcast payloads omit prompts, tool arguments/results, credentials and exception
bodies. Existing route-specific `tool_activity` still owns detailed tool UI.

## Plugin subscription

In `raii.v1 start(scope)`, use:

```python
observations = scope.subscribe_core_events(
    {"memory.sleep", "turn.tool_call_failed", "failure.started", "failure.finished"},
    max_pending=128,
)
```

Consume `(topic, payload)` with `observations.get(timeout=0.1)` in a plugin-owned
worker, handling `queue.Empty` and checking the worker's stop signal. Do not perform
hardware or socket I/O on Core's thread. Register worker cleanup using `scope.defer`.
The subscription starts only when the candidate plugin is published; scope cleanup
closes it and discards pending events on detach, failed attach or replacement.
`get` uses queue timeout semantics; close does not interrupt an indefinite get.

The first item is `runtime.snapshot`, containing `sleeping`, active `turns`, active
`failures`, and active `safe_modes`. Snapshots are copies, not mutable Core state.
The bounded mailbox never waits for the consumer. On overflow it drops pending
observations and supplies a fresh snapshot followed by the latest event;
`observations.dropped` counts discarded items. Apply start/end identities
idempotently. Transient reactions are not replayed to new subscriptions.

The low-level synchronous `subscribe/unsubscribe` API remains for small trusted
in-process projections and legacy consumers. Callbacks must only enqueue work;
exceptions are isolated and diagnostics bounded. Plugins should use the mailbox API.
`PluginManifest.subscribed_events` is not the registration API for this bus.

## Channel projection and activation

Channel subscribes to Core, forwards snapshots to `endpoint.on_runtime_state`,
and observations to optional `endpoint.on_core_event(topic, payload)`.
Both hooks must only enqueue. Providers may replay their snapshot to reconnecting
clients; transient observations should not enter durable delivery/history queues.
A broken display cannot initiate a new failure flow from these hooks.
Socket providers can override `is_ephemeral_frame(frame)` (default `False`) to
exclude observation frames from delivery ACKs and transport replay. Desktop-avatar
uses this for `core_event` and `runtime_state`; it supplies the current snapshot
at reconnect instead. Ordinary chat retains its existing ACK/recovery semantics.

The desktop-avatar adapter uses `runtime_state` snapshots and `core_event` frames.
Its failure display takes priority over ordinary turn/tool activity; plugins choose
their own animation mappings. Core never publishes animation commands.

Core/Channel/Failure and plugin-host changes require an external full Pal restart.
Provider source/resource updates additionally require their normal installation and
provider reload (or a fresh host); browser resources require refresh. Written source
is not evidence of runtime activation.
