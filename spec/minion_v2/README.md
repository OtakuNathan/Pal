# Minion V2 state-machine models

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
- `GraphExecutionLifecycle.tla` models an authored terminal sink, parallel
  non-sink work, checker acceptance, dependency-finding reverse propagation,
  repair barriers, and publication only from the accepted sink.
- `ProcessCapacityLifecycle.tla` proves that capacity counts only materialized
  OS-process incarnations. Durable logical sessions consume no slot, and a
  permit cannot be released before process-group reap and checkpoint closure.
- `DagLifecycle.tla` models dependency readiness, graph-wide pause/cancel, and
  architecture-defect freeze/replan propagation.
- `ArchitectureLifecycle.tla` models Architect/Reviewer sessions that survive
  immutable correction revisions and close only on the human terminal
  decision, plus control requests and restart recovery.
- `StandaloneReviewLifecycle.tla` models review-only execution, report
  publication, pause/cancel, and triage recovery.
- `OrchestrationLifecycle.tla` composes Workflow, Execution Epoch, parallel
  module nodes, and the authored terminal sink. It checks
  hierarchical control ownership, replan freeze, stale propagation, and
  completion safety.
- `DurableEffects.tla` models Action deduplication, atomic event/outbox writes,
  at-least-once effects, receipts, leases, fencing, worker settlement, and
  manager crashes.
- `RoleAssignmentRecovery.tla` models logical-effect assignment reuse across
  regenerated attempt inputs, expired active-attempt recovery, recovery-scan
  ownership, and settlement by the terminal's immutable receipt identity. It
  also injects the duplicate-row shape produced by the retired recovery bug to
  prove that a periodic scan cannot steal an active worker's effect binding.
- `WorkerProcessLifecycle.tla` models the RAII boundary around one worker
  process group, Manager run registration, and exclusive worktree ownership.
  A terminal IPC receipt or leader exit cannot release ownership; replacement
  starts only after the complete process group is reaped and accounting closes.
- `ContinuationLifecycle.tla` models fresh-v27 checkpoint admission. Only a v6
  `l1_turns` payload may start a worker; v5 and malformed checkpoints are
  rejected with visible deterministic errors, while only transient worker
  failures may consume retry budget.
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
- `FileMutationAuthorization.tla` models one logical session's read-before-
  mutate authorization. It proves that a successful self-mutation advances
  the snapshot atomically, historical read deliveries cannot roll it back,
  external writes require a fresh read, and partial reads never authorize a
  mutation.
- `MinionRuntimeAuthority.tla` models a logical role owning its L1/L2 working
  memory while all LLM requests cross the Manager broker and shared L3 remains
  read-only for the complete role lifecycle.

The models intentionally abstract prompts, artifact contents, Git, and provider
details. Those are values carried by transitions, not additional lifecycle
owners.

Run every model with a pinned `tla2tools.jar`:

```bash
scripts/check_minion_v2_tla.sh /path/to/tla2tools.jar
```

`TLA2TOOLS_JAR` can provide the jar path and `TLC_WORKERS` controls TLC's
worker count. The default is one worker to keep the suite usable on the
Raspberry Pi development host.

Regenerate the implementation topology after changing an enum or transition:

```bash
python -c "from pathlib import Path; from pal.minion.v2.formal import write_implementation_topology; write_implementation_topology(Path('spec/minion_v2/ImplementationTopology.tla'))"
```

`pal.minion.v2.machine_dsl.MachineSpec` is the concrete lifecycle source of
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
