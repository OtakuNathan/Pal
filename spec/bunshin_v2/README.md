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
- `VerifierDraftLifecycle.tla` models editable current findings and test cases,
  the atomic durable submission boundary across both authoring drafts,
  receipt/projection lag, immutable history, and evidence-bound PASS. Its
  availability invariants explicitly permit changing or deleting completed
  current findings until submission.
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
  `EnterWorkflowTriage` abstracts the completed child freeze. The runtime
  first projects workflow triage from `ACTIVE` as the existing cycle
  `REQUEST_PAUSE`, then confirms `PAUSED` after worker cleanup, and only
  resumes an assignment boundary. Outbox failure records this intent in
  its transaction; reconciliation repairs the same projection after restart.
  Triage during cancellation/restart does not replace its cancel intent.
  Already-triaged children are pause-settled and keep their own recovery cursor;
  a parent freeze must not nest a second pause over that cursor.
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

## Verifier draft/submission implementation boundary

`VerifierDraftLifecycle` is a protocol requirement, not generated Python. One
finding and one case stand for arbitrary current-owned records; finding kinds
share the same CRUD contract. Two versions represent the `work_items` and
`verification` draft rows. Pending durable receipt acceptance is the freeze
point, including the window in which either draft still projects `active`.
Marking/reconciling a draft `submitted` cannot retroactively choose a newer
payload. Neither completion of a current finding nor recording a test result
submits the assignment.

| Model action/property | Required implementation boundary | Concrete conformance coverage |
| --- | --- | --- |
| `EditFinding`, `DraftAllowsFindingCRUD` | Current-owned finding create/update/delete in `work_items`; stable identity on update, fresh identity after delete/recreate; reducer and Manager persistence validate the target | All allowed finding kinds/dispositions; completed finding edit/delete; rejected stale/deleted target; caller-forged `origin` cannot grant ownership |
| `EditCase`, `RecordEvidence`, `DraftAllowsCaseCRUD` | Case definition changes and execution-result upserts in the verification draft; current case content and evidence remain distinct | Add/replace/remove current cases; definition/content changes stale old evidence; execution can create fresh evidence; deleting an unrelated result cannot waive mandatory historical replay |
| `CommitSubmit`, `AtomicSubmissionSnapshot` | `role_gateway._draft_submit` passes both observed versions; `RoleSubmissionsStore.record_role_submission` rechecks both versions and authority inside its write transaction before accepting the receipt | Barrier-controlled finding-edit/submit and case-edit/submit races: edit first rejects stale submit; receipt first rejects either edit; do not reconcile stale submit into a newer draft |
| `ReceiptFreezesBothDrafts`, `ProjectSubmitted` | Every local/Manager draft write checks the authoritative assignment receipt in the same transaction as its write, including sibling authoring drafts | Inject failure after receipt persistence and before projection; both findings and cases remain frozen; exact receipt replay/recovery never changes sealed content |
| `MutationsUseCurrentAuthority`, `MutationsUseCurrentVersion` | Authenticated role/assignment/input identity, current generation/fence, and affected draft CAS checked at persistence | Wrong role/assignment, old generation/fence, wrong CAS, expired attempt and mutation-versus-replacement race |
| `PrepareSubmit`, exact finding comparison in `CommitSubmit` | Submitted finding projection equals authoritative current `work_items` findings, not an arbitrary authored subset | PASS payload omitting a current blocking finding is rejected even if verification-draft version is current |
| `ReplayMutation`, `MutationReplayRejectsConflicts` | Same draft-scoped operation key and semantic request hash deduplicate; altered expected CAS alone is not a new semantic request | Same-key/same-arguments retry does not advance version; same key/changed content is rejected; stale/frozen replay may reject but never writes |
| `RecoverSourceAsCurrent`, `SourceReceiptsFreezeInheritance` | Classify source with stored ownership, input identity, source/sibling projection and authoritative receipt, never payload `origin` | Owned, same-input, unsubmitted retry recovers an editable current copy; external or submitted source becomes history; pending receipt plus still-active source projection cannot resurrect editable findings/cases |
| `HistoryPreserved`, `HistoryCannotBeTargeted`, `ReceiptNeverChanges` | Separate immutable inherited/submitted source records from mutable current records; original archived retry source remains intact | Current update/delete preserves original external/prior receipts and required case references; original submitted finding/case content survives retry and inheritance |
| `PassRequiresCurrentEvidence` | PASS requires fresh Candidate/corpus/case evidence, historical replay coverage and no current blocking finding | Withdraw a mistaken current finding, rerun required tests, then PASS; no forced PASS, unresolved current failure, empty case set or stale corpus receipt |

Physical corpus writes are separate from SQLite transactions. `EditCorpus`
models them separately from case-definition changes. `acceptedCorpus` records
the content identity at submission validation/acceptance, while the sealed
receipt retains its own identity. Refining the atomic corpus comparison requires
serialization of supported corpus-writing operations with final validation and
receipt acceptance, or sealing an immutable validated corpus snapshot with an
equivalent ordering guarantee. A digest read performed earlier in the request
alone does not provide that guarantee. The model does not claim SQLite locks
ordinary filesystem writes. A later physical change leaves the receipt intact
and removes its authority to represent the current corpus; it cannot manufacture
fresh PASS evidence. Concrete race tests must establish the implementation's
chosen boundary rather than treating this model as proof of filesystem locking.

The source-classification operator exhausts active/submitted projections crossed
with absent/pending/accepted receipt states, stored ownership and matching input.
Actual payload parsing, stable record IDs, content hashes, artifact durability,
schema validation and history partitioning still require the conformance tests
above. The model deliberately has no liveness claim or promise that tests pass.
Bounded edit/version counts constrain model exploration, not user authoring.
The checked configurations allow two revisions per draft, one physical corpus
write, two generations and two fence values. One physical write exercises both
stale-before-submit and change-after-submit paths in separate traces. Replacing
generation/fence selects a new draft-key operation namespace; the model clears
only the active operation-log projection, not the archived source log. An
operation record retains semantic content and its authority scope, while replay
CAS arguments stay outside the semantic request hash.
Here `kind/value` abstracts the complete semantic request, including every
tool-argument field such as an exposed per-finding revision. Only the store's
transport `expected_version` and authenticated context are excluded; changing a
semantic revision with the same operation key must remain a conflict.

`VerifierDraftLifecycle.cfg` checks safety, edit availability and immutable
receipt/history temporal properties. Ten mutant configurations remove one
guard each. `VerifierDraftLifecycleWithdrawalWitness.cfg` deliberately checks a false invariant:
its required counterexample demonstrates a reachable delete-current-finding,
fresh-evidence, accepted-PASS path with history retained. These expected
counterexamples are part of `scripts/check_bunshin_v2_tla.sh`.

`tests/test_bunshin_verifier_draft_model.py` runs real TLC when an existing
`TLA2TOOLS_JAR` is supplied, otherwise explicitly skips TLC. Its supplemental
Python scenarios exhaust four smaller finite spaces and source classifications;
they are neither a TLA parser nor the full model nor production refinement proof.
