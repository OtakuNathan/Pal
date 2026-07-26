# Minion V2 state-machine models

These specifications model the domain-independent orchestration contract before
the Python worker spine implements it.

- `ModuleLifecycle.tla` models one durable module with Coder and Verifier
  sessions that survive immutable Candidates, repairs, and replans for the
  Module's lifetime; this single-Module model closes them when the workflow
  terminates. A role may yield only after Manager records and settles its
  result receipt. Module deletion is composed in `ReplanReuseLifecycle.tla`.
- `DagLifecycle.tla` models dependency readiness, graph-wide pause/cancel, and
  architecture-defect freeze/replan propagation.
- `ArchitectureLifecycle.tla` models Architect/Reviewer sessions that survive
  immutable correction revisions and close only on the human terminal
  decision, plus control requests and restart recovery.
- `StandaloneReviewLifecycle.tla` models review-only execution, report
  publication, pause/cancel, and triage recovery.
- `OrchestrationLifecycle.tla` composes Workflow, Execution Epoch, parallel
  module nodes, and one workflow-level System Verifier join. It checks
  hierarchical control ownership, replan freeze, stale propagation, and
  completion safety.
- `SystemVerificationLifecycle.tla` proves that one workflow-level verifier
  session and durable harness corpus survive module repairs, while PASS remains
  impossible without real delivery-surface evidence for the complete Candidate
  Union.
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
- `ReplanReuseLifecycle.tla` models replan as a mechanical preserve/create/delete
  classification over stable Module keys. It checks that surviving Modules keep
  their worktree and logical Coder/Verifier generations, exact fingerprints
  skip new admissions, changed contracts retain assets, deleted Modules retire,
  re-added Modules receive a fresh generation, and native attempt recovery does
  not replace ownership.
- `ImplementationTopology.tla` is generated from the executable Python
  `MachineSpec` graph. It explores the same concrete source/action/target
  relation used by `TransitionEngine` and checks exhaustive classification,
  finite dynamic targets, recovery actions, control settlement, triage
  refresh, and paused-state resume.

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
