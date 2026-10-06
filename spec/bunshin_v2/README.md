# Bunshin V2 state-machine models

These specifications model the domain-independent orchestration contract before
the Python worker spine implements it.

The executable layering is deliberately small: Family-authored YAML is
compiled to immutable `GraphIR`; `WorkflowCoordinator` owns one `PlanCycle`
and one `GraphExecution`; each graph node owns a producer/checker `NodeCycle`;
and `RoleProcessShell` is only the RAII process incarnation of a durable role
session. Aggregate/outbox machines below remain process and effect projections,
not a second DAG scheduler or semantic lifecycle owner.

- `ModuleLifecycle.tla` models one durable module with Coder and Verifier
  sessions that survive immutable Candidates, repairs, and replans for the
  Module's lifetime; this single-Module model closes them when the workflow
  terminates. A role may yield only after Manager records and settles its
  result receipt. Module deletion is composed in `ReplanReuseLifecycle.tla`.
- `ProduceCheckCycle.tla` models the shared producer/checker protocol used by
  both planning and graph nodes, including generation-bound products and
  verdicts, human review, and triage resumption at an assignment boundary.
- `GraphGenerationLifecycle.tla` separates authored architecture revisions
  from installed GraphIR generations. Superseded human-edit revisions consume
  no graph generation; every candidate targets the next append-only slot and
  installation preserves a gap-free `1..N` history.
- `GraphExecutionLifecycle.tla` models an authored terminal sink whose producer
  runs in parallel from accepted contracts while its checker waits for accepted
  dependency products, plus checker acceptance, dependency-finding reverse
  propagation, repair barriers, and publication only from the accepted sink.
- `ProcessCapacityLifecycle.tla` proves that capacity and concrete-attempt
  leases count only materialized OS-process incarnations. Durable logical
  sessions consume neither, and both remain owned through process-group reap
  and checkpoint closure.
- `DagLifecycle.tla` models dependency readiness, graph-wide pause/cancel, and
  architecture-defect freeze/replan propagation.
- `ArchitectureLifecycle.tla` models Architect/Reviewer sessions that survive
  immutable correction revisions and close only on the human terminal
  decision, plus control requests and restart recovery.
- `StandaloneReviewLifecycle.tla` models review-only execution, report
  publication, pause/cancel, and triage recovery.
- `OrchestrationLifecycle.tla` composes Workflow, Execution Epoch, parallel
  module producers, dependency-gated acceptance/checking, and the authored
  terminal sink. It checks
  hierarchical control ownership, replan freeze, stale propagation, and
  completion safety.
- `DurableEffects.tla` models Action deduplication, atomic event/outbox writes,
  atomic aggregate/cycle business projections, receipt lag across Manager
  crashes, at-least-once delivery without double advancement, leases,
  fencing, and worker settlement.
- `RoleAssignmentRecovery.tla` models logical-effect assignment reuse across
  regenerated attempt inputs, expired active-attempt recovery, recovery-scan
  ownership, and settlement by the terminal's immutable receipt identity. It
  also injects the duplicate-row shape produced by the retired recovery bug to
  prove that a periodic scan cannot steal an active worker's effect binding.
- `WorkerProcessLifecycle.tla` models the RAII boundary around one worker
  process group, Manager run registration, and exclusive worktree ownership.
  A terminal IPC receipt or leader exit cannot release ownership; replacement
  starts only after the complete process group is reaped and accounting closes.
- `ContinuationLifecycle.tla` models v29 resume-checkpoint format admission. Only a v8
  encrypted logical-coroutine payload may restore a worker; v7 and malformed checkpoints are
  rejected with visible deterministic errors, while only transient worker
  failures may consume retry budget.
- `StartupRecoveryLifecycle.tla` distinguishes a never-created coroutine from
  an initialized coroutine whose required checkpoint is missing. It separates
  file publication, durable session initialization, and worker acknowledgment;
  explores failure/recovery between them; and forbids fresh work before acknowledgment,
  stale-fence initialization, and silent fresh execution after initialization.
  A negative-control configuration deliberately enables the forbidden fallback.
- `LogicalCoroutineSnapshotLifecycle.tla` models worker-owned composite runtime
  state, opaque Manager routing, closed-boundary checkpoint replacement,
  crash/restore with a new fence, and joint N+5 retirement of pager/file state.
- `ResidentMailboxLifecycle.tla` models one Pal-wide mailbox and active turn.
  Reset requests cancellation, waits for the active turn to exit, keeps queued
  messages, and models a timeout without lying about resident state; it never
  creates channel-scoped personas.
- `TaskDeliveryLifecycle.tla` models Manager-owned Task delivery bindings.
  Explicit rebind changes only the current reply target; delivery moves through
  Pending -> InFlight -> Delivered (provider accepted), with failures returning
  to Pending.
  Dead targets fall back to a live recovery socket, otherwise the durable
  delivery remains pending.
- `ReplanReuseLifecycle.tla` models replan as a mechanical
  preserve/create/delete classification over stable node identities. Unchanged
  responsibility preserves the worktree, sessions, and corpus; acceptance is
  carried only when contract and incoming edges are unchanged. Replaced and
  re-added nodes receive fresh identities, and only the authored sink publishes.
- `ImplementationTopology.tla` is generated from the executable Python
  `MachineSpec` graph. It explores the same concrete source/action/target
  relation used by `TransitionEngine` and checks exhaustive classification,
  finite dynamic targets, recovery actions, control settlement, triage
  refresh, and paused-state resume.
- `BunshinRuntimeAuthority.tla` models a logical role owning its L1/L2 working
  memory while all LLM requests cross the Manager broker and shared L3 remains
  read-only for the complete role lifecycle.

The models intentionally abstract prompts, artifact contents, Git, and provider
details. Those are values carried by transitions, not additional lifecycle
owners.

Shared file-result authorization is modeled under `spec/execution`; Pal and
Bunshin use the same execution state machine.

Run every model with a pinned `tla2tools.jar`:

```bash
scripts/check_bunshin_v2_tla.sh /path/to/tla2tools.jar
```

`TLA2TOOLS_JAR` can provide the jar path and `TLC_WORKERS` controls TLC's
worker count. The default is one worker to keep the suite usable on the
Raspberry Pi development host.

Regenerate the implementation topology after changing an enum or transition:

```bash
python -c "from pathlib import Path; from pal.bunshin.v2.formal import write_implementation_topology; write_implementation_topology(Path('spec/bunshin_v2/ImplementationTopology.tla'))"
```

`pal.bunshin.v2.machine_dsl.MachineSpec` is the concrete lifecycle source of
truth. Runtime dispatch, recovery classification, control reconciliation, and
the generated TLA+ topology consume it. Dynamic target functions must use the
`target_resolver` decorator to declare their complete finite target set; the
runtime rejects any result outside that declaration.

The hand-written lifecycle modules remain higher-level protocol models for
cross-aggregate behavior, effects, leases, and temporal properties. TLC proves
those abstractions, while Python conformance and SQLite/outbox crash-window
tests prove the concrete implementation boundary. Arbitrary Python guard or
effect code is not automatically translated into TLA+.

`test_replan_preserves_changed_module_worktree_and_role_session` and
`test_replan_deletes_and_readds_module_as_a_new_identity` are the concrete
boundary tests for `ReplanReuseLifecycle`.


## Startup/recovery implementation boundary

`StartupRecoveryLifecycle` covers checkpoint-capable Pal workers, beginning with
`RoleSessionState.UNINITIALIZED`. The durable status is the historical fact;
current filesystem presence alone is not that fact. `Active` and `Suspended`
both require a checkpoint on the next admission. Existing initialized sessions
are never inferred to be uninitialized from a missing file.

The refinement boundary is explicit, rather than generated Python semantics:

- `AdmitFresh`: `RoleCheckpoints.prepare_agent_session_attempt` permits absent
  input only for `uninitialized`. `AdmitResume` restores a valid existing file,
  including one published while the session still says `uninitialized`;
  admission reconciles that status with `INITIALIZE` before process launch.
  `RejectCheckpoint` represents missing required, unreadable, invalid-envelope,
  or wrong-identity input; `ContinuationLifecycle` expands the format checks.
- `StartProcess`: durable `ACTIVATE` preserves `uninitialized`; it is not evidence
  of a created coroutine. `FailAttempt` and `CrashBeforeAck` abstract failure
  settlement through `PARK`, which also preserves `uninitialized`. Queueing a
  concrete retry alone can leave an initialized session `active`; the model
  collapses that stopped state to `Suspended`. Both require the same checkpoint
  on admission, so this abstraction preserves the admission property. Charged failures stop at
  the configured budget; an uncharged process loss does not spend that budget.
- `PublishCheckpoint`, `CommitInitialization`, `ReceiveInitializationAck` are
  separate steps: the Manager promotes the file, commits `INITIALIZE`, and
  replies to the worker's `checkpoint_initialize` operation. The worker awaits
  that reply before entering fresh semantic work. A restored worker already
  has its Manager-selected input and does not repeat the initialization ACK;
  its valid snapshot may have an earlier producer fence. Publication while
  `Running` abstracts Manager promotion at retirement, not a Manager ACK for
  every worker-local safe-point write. A crash before the DB commit leaves
  an uninitialized session with a restorable file; a crash after commit but
  before ACK leaves an initialized session requiring that file.
- Initial worker-requested publication and initialization require the current attempt/fence.
  `RejectStaleInitialization` is a rejected old-worker operation with no durable
  mutation. The accepted token is retained as a model history variable and
  checked by `InitializationRequiresCurrentFence`.
- `DamageCheckpoint` preserves the initialization fact and sequence, while
  modeling file loss or corruption between attempts. A missing checkpoint is
  never silently replaced with a fresh invocation for an initialized session.

The model does not implement encryption, SQLite transactions, IPC transport,
OS process reaping, or arbitrary Python callbacks. It does not claim end-to-end
workflow completion: its liveness properties concern admission resolution and
settlement after retry exhaustion, under the stated weak fairness assumptions.
Finite start/sequence bounds are exploration bounds, not production limits.
External/non-checkpoint harnesses and successful legacy submissions are outside
this model; the implementation conservatively treats successful submission as
initialized history for future admission rather than permitting a fresh reset.

`tests/test_bunshin_startup_recovery_model.py` checks the concrete session
transition mapping, checker coverage, and positive/negative model configurations.
When `TLA2TOOLS_JAR` is set, it runs TLC in a temporary directory. Without that
pinned jar, the TLC tests explicitly skip; the other Python checks are not a
substitute for model checking. The full shell checker includes the positive
model and requires a `FreshStartNeverForgetsInitialization` counterexample from
`StartupRecoveryLifecycleUnsafe.cfg`.
