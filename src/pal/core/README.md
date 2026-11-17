# core

Owns:
- event main loop
- turn coordination
- turn computation suspend/resume
- minimal runtime-owned state
- subsystem orchestration
- module lifecycle governance
- generic local-control admission, dispatch, query, delivery and context-change coordination
- one-shot timer events, cancellation and main-loop wakeups
- registry ownership for visibility and mount policy
- prompt fragment registry and prompt assembly seam
- the shared `AgentTurnRuntime` prompt/turn-execution kernel used by Pal and
  Bunshin hosts
- compaction coordination: LLM produces a validated candidate; Memory owns capture, atomic installation and recovery
- stagnation guard orchestration; unchanged consecutive `search_tools` results
  receive a second-call reminder, without changing the guard's observation or
  termination threshold. Finalization retains tool schemas and tool choice;
  its directive appends chronologically while execution authority remains
  revoked and returned tool calls are replaced by a text fallback
- module runtime-state orchestration: deterministic snapshot/restore order,
  reverse-order whole-runtime reset, exact incarnation checks, and encrypted
  composite envelopes

Does not own:
- durable truth
- direct database access
- side effects
- plugin implementations
- module-specific slash-command parsing, presentation and business effects
- cache-reminder policy, timers, buttons or configuration
- failure recovery policy, prompts and memory records
- module schedule calculation or Dreaming failure handoff policy
- capability invocation mechanics

Exposes:
- `PalCore`
- `TurnManager`
- `MainLoop`
- `EventDispatcher`
- event and handler registries
- `PromptFragmentRegistry`
- `AgentTurnRuntime`
- `CoreControlRuntime` through the typed `CORE_CONTROL` port
- `TimerScopePort` and `TimerHandlePort` for owned, cancellable timer events
- mailbox and turn-effect contracts

Interaction rule:
- failure recovery presents a maintenance tool call once, identified by its
  call ID; repeated calls remain separate even when their text is identical.
  User-facing LLM failures place the cause in the failure owner's `Blocker`
  field rather than prepending a second copy to that feedback
- `PalCore` is the only governance center
- module-owned commands and button callbacks use declared action handlers;
  Core's own orchestration commands use the same registry. Ordinary control
  requests need no dialogue turn; `/btw` explicitly starts a temporary turn.
  See [Control](../control/README.md) for registration and delivery contracts
- timer expiry only enqueues a callback event; the main loop executes it.
  A module declares its `timer_scope` so withdrawal cancels pending and already
  queued events. Business-specific deadlines stay with their module
- pending control requests register their own expiry events. Renewal replaces
  the previous timer; executing requests suspend expiry. Core does not scan
  confirmation lists or parse timestamp strings on each loop iteration
- all channel input enters one resident mailbox and one L1/execution lifetime;
  channels select only the response transport and never create a persona,
  conversation, or execution scope
- Pal and Bunshin supply host policy and ports to the same `AgentTurnRuntime`;
  they do not maintain separate prompt compilers or turn executors
- automatic compaction is requested only by the real model context-budget
  path; host clocks annotate snapshots but never trigger fixed-round compact
- the immutable captured history is compaction's sole semantic input. Native
  strategies may prospectively include a continuous completed RIGHT prefix;
  pending RIGHT content stays outside capture and unchanged at install.
  Provider projections, recall caches, and role anchors are not parallel truth sources
- accepted reasoning and native continuation survive turn closure and restart
  until explicit LEFT compaction. Crash recovery repairs unfinished or invalid
  protocol without fabricating tool results
- projection send receipts prove request admission only. Accepted L1 messages,
  including their replay envelopes, are the sole response authority; native
  projection material derives from them after finalization, with tool ids,
  names and arguments checked against the accepted semantic parts
- one logical turn may create at most three compact generations. Semantic
  generation gets three attempts before that compact RPC fails with memory
  unchanged. The prompt plus local validator cap visible checkpoint JSON at
  half the selected input budget or the absolute 20,000-token ceiling,
  whichever is smaller, while preserving provider-declared model reasoning
  headroom
- one shared continuity policy owns schema, prompt, validation and full rendering.
  Host adapters retain only task-source semantics, clocks, and memory-candidate policy;
  replay keeps the original generation settings and appends a user compaction request
- compaction attempts retain full private diagnostics. A successful retry does
  not emit an error notice; terminal failures and failed post-commit work expose
  a concise cause with the run's service-log reference
- `Execution` is the only official invocation plane
- `Pal` should call capabilities through `PalCore -> Execution`
- turn computations yield effect requests and are resumed by `PalCore`
- `MainLoop` drains mailbox-backed sources after async wakeups rather than
  pulling module-private queues on an idle timer
- resident chat, proactive and background completion turns share the
  synchronous scheduler reducer in `scheduling.py`; ownership lasts through
  task teardown, including post-commit I/O and cancellation. Proactive waits
  in its trigger mailbox while busy and yields to queued user input.
- scheduler phases and orthogonal admission barriers are the authority for
  resident admission. `/btw` reads a captured immutable L in its transient
  branch and does not claim the resident writer slot. See
  [resident scheduling](../../../docs/core_turn_scheduling.md) for the model,
  transition boundaries and verification scope.
- resident reset interrupts the active turn, preserves already queued channel
  messages, and resets module-owned runtime state through their registered
  runtime-state ports before the same mailbox resumes
- modules may register ports, event sources, providers, and introspection
  surfaces with `PalCore`
- `register_with_core(...)` is also the hydration seam for Capability Forest
  subtree generation
- `PalCore` governs subtree mount/detach/reattach, but `Execution` physically
  stores and dispatches those subtrees
- modules must not directly call other modules' runtime, service, or repository
  implementations
- `PalCore` does not supervise OS processes; that belongs to `supervisor`

System display notifications share `MainContext.core_event_bus`; the former
`turn_event_bus` name aliases it. See [Core system observations](../../../docs/pal_core_events.md)
for topics, snapshots and plugin mailbox subscriptions.

Background modules use `CORE_CONTROL` for admission, turn execution, interruption
cleanup and diagnostics. Core retains the scheduler and the owning module's
background-work lease, without holding a process-wide reader for the whole turn;
module handlers retain their own run records and task cleanup. Only registered
timer events determine timed wakeups; Core does not probe event sources or name
individual scheduling services.

Package completion turns use the same background admission and task lifecycle
as proactive turns, with their explicit scheduler kind. Modules can capture an
active turn's immutable delivery binding through `CORE_CONTROL`; they never
edit the active-turn registry or initialize L1 independently. Cancellation before
the first coroutine step still completes the Core lease and module bookkeeping.

Reset dispatches through the resident history owner or registered runtime-state
ports. Execution history binding and turn initialization are required contracts;
projection retirement uses the LLM's declared optional projection port.

A Memory turn that cannot close remains registered as pending teardown. Core
still cancels the producer, records the cause, and retries that close before
admitting another resident writer. Successful Memory closure releases the
retained record; an explicit successful reset also clears obsolete teardown.
No close error is silently converted into successful cleanup.

Module publication is a generic, generation-owned notification seam. Consumers
can subscribe through `ModuleHandle.module_observer`; subscription replays the
currently published modules and withdrawal retires that exact owner. Core has
no Skill-specific registration calls. The LLM module supplies immutable turn
settings and output budgets; Core adds host logging policy to that snapshot.

Local control handlers and semantic observer callbacks retain the shared
lifecycle lease until completion. Dispatch rechecks the captured registration
after admission, so queued work cannot execute a retired generation. Core's tool
forwarder delegates lifecycle admission to the tool's declared access policy.
Interruption and reset coordinate their own shutdown and admission paths, so
they do not wait behind an outer shared lease before stopping active work.

Capability refresh stages a replacement without withdrawing the old registry
surface first. A rejected publication restores the previous compiled surface
with fresh admission tokens; a saved object reference is not a rollback.

Background work declares its exact module handle, or `owner=None` for resident
work such as package completion. An active owner cannot unload until its work
finishes or is interrupted. Tools inside that turn retain their own shared,
exclusive or owner-managed lifecycle admission. Task completion releases the
module even when cancellation precedes the coroutine's first step.

Reset's admission barrier is owned from setup through notifications and state
reset. Finalization retires only that barrier and drains even repeated
cancellation before queued work resumes; another shutdown owner stays intact.

Timer scopes bind to their module registration, and ordinary event handlers
capture the publishing module handle. Immediately before invoking a callback,
Core checks that the registration is still current and mounted. Withdrawn or
replaced registrations are skipped once, without replaying the event into the
replacement. Logical unload marks the handle before resource cleanup starts,
including when cleanup is retained for retry.

These are entry checks. A callback that has already begun retains its ordinary
coroutine behavior; unloading neither cancels it nor waits for it to return.

Module registration and event-source names use the production ownership reducer
in `registration_state.py`. A new owner cannot replace an occupied name;
withdrawal checks the original handle/source identity. PluginScope advances the
registration state through publication, withdrawal and private cleanup, so an
unclaimed attempt or an old cleanup retry cannot withdraw the current owner.
The same reducer generates the relation imported by the checked TLA+ model. See
[registration ownership](../../../docs/module_registration.md) for the state
map, model/runtime correspondence and verification limits.

`CORE_CONTROL.retain_modules` supplies exact-generation leases for module-owned
background jobs that do not create conversation turns. Completion/cancellation
releases those owners after worker cleanup. Generic module quiescence runs before
plugin teardown; data migration and publication remain with Memory.

Memory maintenance admission returns an opaque owner token. Sleep publication and
release require that exact token. Core checks release inside the admission lock;
a delayed original close or recovery snapshot cannot release a later admission.
Core owns this coordination identity; Memory still owns publication and sleep policy.
