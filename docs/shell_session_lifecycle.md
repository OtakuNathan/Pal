# Native session lifecycle (0.4.0)

Pal uses matching pal-shell-native 0.4.0 / native API 2 / worker protocol 3.
The native repository owns the independent TLA+ model, pure clock-driven transitions,
process timers and remote control journal. Pal owns observation delivery and L1.

## Three independent controls

- `run_shell.wait_ms` waits for the initial response (default 300000 ms, 1000 for PTY).
  It returns a running session at expiry; it does not kill the process.
- `timeout_ms` limits process lifetime. Omission means unlimited execution.
- `shell_session` observation controls decide whether/when Pal receives another event.

`read(wait_ms=0)` inspects or waits within a tool call. It never renews the deadline or
creates a background watch. `watch(wait_ms, extend_by_ms=0)` immediately acknowledges
one background wakeup and replaces any previous watch. It can atomically extend an
existing finite deadline. `extend(extend_by_ms)` only adds to that deadline. A fresh
watch can restore attention; `unwatch` cancels pending unsolicited notifications and
future watches without stopping execution. `terminate` still requests process exit.

The existing indirect `shell_session` tool is called through `call_tool`. Returned
conditional affordances include ready-to-use read/watch/unwatch arguments; a valid
contract already in context does not need rediscovery. No automatic periodic wakeup
is introduced. An unlimited command needs no extension; expired/terminating sessions
reject extensions, and signed remote management budgets are not extendable.

Unwatch does not release running write ownership or detach a process from its owner's
lifecycle. It is not proof of cancellation. Runtime shutdown still owns process cleanup.
Quiet terminal results remain queryable under bounded output retention; eviction is
not evidence that the command never ran.

## Events and evidence

Initial waits and explicit reads return real tool results. Background wait expiry and
completion become runtime-authored user-role events, with session, originating turn,
runtime epoch and event sequence. They are observations, not new user instructions.
Continue only the still-applicable original task; waiting or command completion does
not itself settle the task.

Waiting events are one-shot. Known terminal state supersedes undelivered waiting
state. Unwatch and termination invalidate pending waiting guidance; stale watch
generations are ignored. Terminal output is acknowledged/released only at its existing
successful delivery boundary. Control receipts do not copy complete logs.

Events append new output to immutable L1 history. Delivered byte offsets advance only
after the corresponding context reaches L1. Stable message IDs prevent replaying a
committed event after an acknowledgement failure; failed model turns do not silently
retry their effects. Pal's resident source waits for an idle protocol boundary and
Bunshin uses its existing safe points. Neither creates an execution deadline.

Remote operation IDs preserve extension idempotency after lost replies. Query the
original operation through the supplied reconciliation affordance; a new operation ID
would mean a new intentional extension. Remote disconnection does not establish exit.

## Prompt follow-through

Search locates code; `read_file` establishes delivered file-tool ranges for edits.
Shell output alone does not create that grant. Reuse valid reads and known contracts.
Search results expose concise purposes rather than the internal indexing document.
A confirming, fresh tool result needs no obligatory second verification. Layout
inspection detail is attached to the layout tool, and checklist closure creates no
additional validation ritual. User-requested verification limits remain applicable.

## Validation and activation

The native model checks two sessions, two watch generations and finite/unlimited
budgets. It assumes fair clock/termination scheduling, not that unlimited jobs finish.
Native tests separately cover real processes, virtual time, one-shot observation,
renewal idempotency, hard timeout and signed-operation boundaries. Host tests cover
multiple event deliveries, output deltas, unwatch during materialization and terminal
supersession, without paid LLM requests.

Activate the matching Pal checkout, extension and local remote package together when
the host and shell work are idle. This machine uses `/usr/bin/python3`, runtime root
`/home/nathan/.pal` and user service `pal.service`. Install native artifacts in a new
versioned directory and retain prior import paths. Do not overwrite a mapped binary.
Remote worker installations remain a separate operator action.

A compatible rollback base is Pal `cd1fb11` with the previous native 0.3.0 directory:
it understands the scoped L1 records from the prompt refactor. Merely reverting to an
older projector that cannot interpret those records is not a safe context rollback.
Preserve service configuration, runtime data and unrelated working-tree changes.
