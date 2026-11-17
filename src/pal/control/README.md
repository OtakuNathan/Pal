# control

Owns:
- channel-neutral command and interaction protocols
- deterministic invocation parsing and registered parser dispatch
- command catalog, menus and the generic control panel
- protocol error presentation

Does not own:
- model settings, endpoint refresh or model-selection workflows
- memory review, compaction or reset semantics
- module business effects or their domain-specific presentation
- tool execution, open-ended reasoning or durable truth

Modules declare slash commands in `ModuleHandle.control_commands` and button
parsers in `control_interactions`. `ControlCommandSpec.handler` and
`ControlInteractionSpec.handler` are pure parsers returning a `ControlAction`.
`ControlPlane` starts empty; resident modules and plugins use the same declaration
path. Only generic `control.command.run` and `control.action.dispatch` button
protocols live in the router.

`ModuleHandle.control_action_handlers` maps action kinds to `ControlActionSpec`.
Its async handler executes the owning module's business operation and returns
`None`. It uses the `CORE_CONTROL` port (`core:control`) for queries, replies,
interactive delivery and coordination. There is no result-shape negotiation or
Core branch for a module's command name. Domain rendering lives with its module.

A normal slash request is local control work: Core admits it, supplies status and
routing, dispatches its action, and releases its reservation on completion or
failure. It does not create an LLM turn or append L1 history. `/btw` explicitly
starts a transient dialogue turn through its Core-owned handler.

Core's control port supplies runtime snapshots, pending-request leases, delivery,
current prompt capture, configuration-change coordination and context-change
leases. The module decides which operation to request and what to tell the user.
Core owns scheduling barriers and atomic commit checks. Modules opt into controls
allowed during maintenance using `ControlActionSpec.allow_during_maintenance`.

`open_timer_scope()` schedules cancellable one-shot events for main-loop
execution. Declare the scope as `ModuleHandle.timer_scope`; withdrawal cancels
its timers even when their events have already entered the queue. Pending
requests use separate expiry events, with typed UTC datetimes internally and
ISO strings only when rendering UI. Renewal and execution update their timers.

`ModuleHandle.configuration_handlers` contributes owner-specific async
configuration operations to the resident RPC router. They publish and withdraw
with the module generation; Control only routes operation names and awaits the owner’s complete RPC result.
Configuration-service shutdown owns its drain task through caller cancellation;
already-started owner operations finish before their dependencies can close.
Identity, Memory, Control and optional modules use this same path.

Modules can contribute `control_status_provider` for `/status` and
`control_observers` for configuration and semantic lifecycle notifications,
including `core.user_activity`, `core.turn.settled` and `core.context_change.started`.
Observers are declared async
callbacks; Core does not scan ports for optional methods. Observer failures keep
private diagnostics and report the owner's concise cause to the initiating module.

PluginHost publishes declarations only after startup and capability publication
succeed. Names, aliases and button keys are validated before a declaration becomes
visible. A collision rejects the complete declaration. Unload and rollback
withdraw commands, buttons, action handlers, status and observers before resource
cleanup. Retirement checks identity so an old generation cannot remove its
replacement. Declarations published before Control attaches are installed when
its router becomes available.

Set `show_in_panel=True` and provide a description to publish a command to channel
menus; `panel_button=True` also adds a control-panel button. Catalog changes wake
Core and are coalesced. Core delivers catalog updates without a model call;
providers render that catalog instead of maintaining independent command lists.

The runtime snapshot includes prompt-log policy for module startup. Background
work uses `delivery_binding_for_turn()` and `start_background_turn(owner=..., kind=...)`;
Core owns admission and all turn initialization/teardown, while the module owns
its observations and completion bookkeeping.

Slash handlers, configuration RPCs and module notification callbacks share the
runtime lifecycle fence with tools. Unload waits for admitted calls to finish;
queued calls recheck their captured registration and cannot enter a replacement.
Control actions declare `lifecycle_access` when they require exclusive access or
manage admission themselves, as Core's tool forwarder does. Configuration RPCs
use shared access and receive the resident gate at composition.

Module timer callbacks check their bound registration immediately before
execution. A retired owner skips that trigger; a callback already executing
keeps its normal coroutine behavior. Local slash/RPC handlers retain their
separately declared lifecycle admission policy.
