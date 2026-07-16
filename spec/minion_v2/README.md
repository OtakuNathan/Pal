# Minion V2 state-machine models

These specifications model the domain-independent orchestration contract before
the Python worker spine implements it.

- `ModuleLifecycle.tla` models one durable Node with long-lived Coder and
  Verifier role coroutines. A role may yield only after Manager records and
  settles its result receipt.
- `DagLifecycle.tla` models dependency readiness, graph-wide pause/cancel, and
  architecture-defect freeze/replan propagation.
- `ArchitectureLifecycle.tla` models Architect/Reviewer ownership, human
  decisions, revision supersession, control requests, and restart recovery.
- `StandaloneReviewLifecycle.tla` models review-only execution, report
  publication, pause/cancel, and triage recovery.
- `OrchestrationLifecycle.tla` composes Workflow, Execution Epoch, and a
  four-node fork/join DAG. It checks hierarchical control ownership, replan
  freeze, stale propagation, and completion safety.
- `DurableEffects.tla` models Action deduplication, atomic event/outbox writes,
  at-least-once effects, receipts, leases, fencing, worker settlement, and
  manager crashes.
- `ImplementationTopology.tla` is generated from the Python enums and
  transition table. It checks exhaustive state classification, transition
  endpoints, recovery actions, triage refresh, and paused-state resume.

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

TLC proves the abstract protocol. Python transition-table conformance and
SQLite/outbox crash-window tests remain required because model correctness does
not imply implementation correctness.
