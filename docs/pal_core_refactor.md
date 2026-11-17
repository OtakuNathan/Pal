# Core service contracts

Core coordinates semantic effects. LLM owns generation, request projection and
compaction execution. Memory owns history capture, installation and L3 maintenance.

## Generation and metadata

`LLMService` is the published service contract. `metadata()` returns typed
endpoint facts; there is no parallel Query port or signature negotiation.
`generate()` exposes one asynchronous semantic response stream. A complete HTTP
response is one terminal update; streaming adds semantic deltas before it.
Transport framing and endpoint response mode stay inside LLM.

The terminal update carries a call-local `LLMGenerationResult`, including its
endpoint, model and projection receipt. Core does not construct results from
shared `last_*` inspection telemetry. Concurrent resident and BTW requests use
the same LLM instance and transport with separate request facts and projection scopes.

## Compaction and maintenance

`CompactionOperation` coordinates the transaction through `CompactionService`
without implementing provider strategies or constructing execution limits. Memory captures the frozen left segment. LLM prepares native or
cached request material, executes bounded attempts and returns a validated
candidate. Memory stages the archive and installs the candidate atomically.
Core then coordinates checkpointing, projection rebase and result retirement.
A post-commit fault is classified from Memory's committed record and does not
turn a successful installation into a claim that history is unchanged.

Sleep first takes Core's admission barrier. Memory then fences its selected L3
repository, performs maintenance and mounts the published generation. Freeze
failure releases admission; a published generation that cannot be mounted keeps
admission closed. Backend and repository differences remain in Memory.

## Publication and execution

Consumers subscribe once with `PortRegistry.observe(key)`. Publication validates
the candidate and pushes the new owner into `ServiceRef.current`. Reading the
reference requires no registry lookup or generation comparison. In-flight
operations retain their captured owner. Withdrawal checks owner identity so an
old generation cannot remove its replacement.

First-party execution implementations share one explicit contract with Bunshin.
Turn execution calls declared methods directly, including delivery acknowledgement
and result retirement; it does not negotiate method presence or tool-call signatures.
L1 turn registration is synchronous and propagates invalid lifecycle or input errors.
It never falls back to generating an answer outside its required L1 turn.

Tool lifecycle access is declared in immutable `ToolExecutionSemantics` as
shared, exclusive or owner managed. Aliases do not select locks. Registered
calls capture the binding and its policy together; a queued call cannot adopt
a replacement binding after obtaining a different generation's lock.
The generic cancellation-safe gate belongs to Foundation. L3 provider selection
and registration belong to Memory rather than Execution.

## Temporary side questions

BTW borrows an immutable closed L1 prefix and keeps a private tail. Its
restriction and question are appended together as the last new user message.
Existing instructions and tool schemas remain unchanged. Temporary execution
visibility and cleanup use Execution's explicit request-context boundary.

## Module-owned controls

Modules declare commands, button parsers and async business action handlers on
`ModuleHandle`. Control parses registered inputs and builds the common catalog.
Core dispatches admitted actions without a command-name switch. Its own status,
interrupt, compact and reset commands follow the same registration path.

LLM owns thinking controls, model-selection workflows, endpoint refresh and
their presentation. Memory owns review controls. Each owner performs its effects
and uses `CORE_CONTROL` for basic queries, reply delivery and coordination. Core
owns admission, context-change leases, atomic commit and generic request expiry;
it does not inspect model-selection stages or endpoint internals.

Ordinary slash commands complete as local control requests without an LLM turn
or L1 write. `/btw` starts its transient dialogue explicitly. Commands, callbacks,
status providers and configuration observers publish and withdraw with their
module generation; stale cleanup cannot remove a successor's contribution.

## Timed events and optional reminders

Core owns a monotonic one-shot timer queue and cancellation scopes. Timers
enqueue events; callback execution stays on the main loop. Module withdrawal
revokes its pending and already queued timer events. Control requests schedule
their own expiry timers, replace them on renewal and suspend them during
execution. Their internal deadlines are UTC datetimes; no per-loop scan or UTC
string parser remains in PalCore.

`cache_reminder` is an optional first-party plugin. It owns reminder policy,
independent show/withdraw timers, cards, compact/dismiss/disable callbacks and
configuration handlers. It observes generic lifecycle notifications and uses
Core's delivery and context-change interfaces. PalCore has no reminder field,
cache-epoch branch or feature-specific configuration. The confirmed-cache guard
is supplied by the module to LLM's compaction executor; estimated notices retain
ordinary manual-compaction semantics. The configuration router dispatches
module-declared operations and withdraws them with their owner.

Internal Python interfaces change together with their consumers. Retired Core
and Plugins package exports, Query and side-runtime factories are removed.
Persistent configuration, archives and recovery formats are preserved. This
checkout requires an external host restart for resident activation.
