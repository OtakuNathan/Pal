# Execution state-machine models

`FileResultAuthorization.tla` models result-owned authorization shared by Pal
and Bunshin file tools.  File content is abstracted to bounded versions and
line regions; Python refines those regions with the canonical line map and
refines the atomic mutation action with digest-checked filesystem CAS.

Run the model with the pinned TLC jar:

```bash
scripts/check_execution_tla.sh /path/to/tla2tools.jar
```

Tool-result compaction, result expiry, and logical-session retirement all use
the same `RetireResult` transition.  Pager payload retention is not mutation
authority; replaying an exact page creates a new result-owned contribution.

## Async lifecycle admission

`LifecycleAdmission.tla` composes one lifecycle writer with two read waiters and
one write waiter. `Acquire` is the blocking worker's ownership transfer;
`Enter` transfers that lease to the context body. Cancellation transitions to
`draining`, and further cancellations leave ownership there. Only `Drain`,
after acquisition completes, releases a cancelled waiter's lease. The Python
refinement is `WriterPreferredRWGate._drain_acquisition` followed by the matching
release operation. Body exit retains the existing release path.

TLC checks exclusion, exact lease ownership, no cancelled body entry, and eventual
completion under weak fairness (the external writer and admitted bodies must
finish). The finite model bounds cancellation requests at two; the implementation
loop treats every subsequent cancellation identically. It does not claim to stop
blocking threads or model event-loop destruction/process termination.

```bash
java -jar /path/to/tla2tools.jar -workers 1 -cleanup \
  -config spec/execution/LifecycleAdmission.cfg spec/execution/LifecycleAdmission.tla
python -m pytest tests/test_execution_admission_model.py tests/test_lifecycle_gate_cancellation.py
```

The independent finite Python explorer additionally injects the old transition
that abandons acquisition on a second cancellation and checks that it violates
ownership. Runtime regressions synchronize the worker, cancel twice while a
writer holds admission, verify original cancellation propagation and balanced
read/write counts, then verify both admission modes remain usable.

## Entry-first retirement and admission authority

`EntryWithdrawal.tla` composes callable entry visibility, mount incarnation,
admission leases, and resource retirement. The implementation boundary is:

1. Validate the requested operation and all affected plugin owners. Invalid
   requests and `check_detach` refusals do not change publication
2. Atomically withdraw the plugin/dependent capability subtrees and revoke their
   shared mount admission authority under the registry lock
3. Release the registry lock, then wait for the lifecycle writer. Calls already
   admitted retain their read leases and finish; no new captured binding can
   cross withdrawal
4. A failure before cleanup restores only identical, untouched owners, with fresh
   mount authority. A queued old call cannot retarget this restored entry
5. Beginning resource cleanup commits logical unload. Arbitrary `raii.v1`
   cleanup callbacks are not reversible. Cleanup failure leaves entries closed,
   reports `logical_unload_committed` and `cleanup_pending`, and retains the
   generation plus remaining callbacks for retry. Pre-commit package failure also
   restores the prior removal record and retained settings. Successful retry
   clears pending-cleanup diagnostics. It does not claim rollback or
   republish a partially closed provider

Admission is the read lease plus the live mount-token check, both before handler
execution. The token check and token revocation use the same registry lock.
Executor-queued work *after* that successful check is already admitted and may
finish. Registry freezing shares tokens, whereas every remount creates a fresh
one; unrelated registry rebuilds do not revoke a selected live target. Empty
subtrees also own a token, so stale empty handles cannot remove replacements.
The separate live token prevents old execution-runtime snapshots from admitting
retired bindings after an implementation swap. Extension staging does not revoke
published authority; publication/rollback explicitly retires outgoing authority.

The entry-first pre-drain phase is used by background package install/uninstall.
Ordinary serial lifecycle tools retain their outer lifecycle writer and use the
same entry-withdrawal-before-cleanup boundary inside the owner. Canonical
`call_registered` keeps its existing resolve-after-gate contract and atomically
checks live authority when resolving. No alias is silently retargeted for a
captured tool-record invocation.

```bash
java -jar /path/to/tla2tools.jar -workers 1 -cleanup \
  -config spec/execution/EntryWithdrawal.cfg spec/execution/EntryWithdrawal.tla
# Expected failure: removing live-incarnation admission checks
java -jar /path/to/tla2tools.jar -workers 1 -cleanup \
  -config spec/execution/EntryWithdrawalUnsafe.cfg spec/execution/EntryWithdrawal.tla
python -m pytest tests/test_plugin_entry_withdrawal.py tests/test_execution_extension_admission.py
```

The safe bounded model checks entry visibility, admission ownership, no retarget,
and drain-before-cleanup, plus queued-call and retirement completion under weak
fairness. The unsafe variant must violate `NoRetarget`. The existing
`LifecycleAdmission` model supplies the read/write gate and repeated-cancellation
ownership component. Models abstract callback effects; real tests cover a
successful irreversible callback followed by a failing callback, retained retry
ownership, and truthful applied-effect/reconcile-first diagnostics.

### Private persisted replay is not public entry retirement

Bunshin's permission-gated legacy `add_finding` replay is intentionally absent
from current discovery and schemas. It compiles a private generation directly;
it must not mount/capture/unmount a temporary public entry, because unmount now
correctly revokes the authority shared by every captured view. The private
replay entry has its own live authority. Other bindings borrowed into that
private generation keep their original tokens, so real provider withdrawal
still fences them. Existing persisted capability/profile checks and verifier
authoring serialization continue to guard the replay path. There is no general
stale-token exemption, token reactivation, or public legacy-tool exposure.

## Failed execution-extension installation

`ExtensionRollback` separates restoration of the original runtime from closing
the failed candidate. `ExecutionSlot.install` retains `_PendingCleanup` before
attempting either operation and immediately revokes failed candidate entries.
`_close_pending_candidate` retries restoration first; only after it succeeds can
candidate close run. A close failure retains the same candidate without repeating
a successful restoration. New installs remain blocked while either step is pending.

The unsafe configuration drops ownership when restoration fails and must violate
`CleanupOwned`. Runtime tests inject activation failure followed by restore failure,
check that captured candidate calls are rejected, and retry uninstall to completion.
The model assumes the lifecycle writer is held and abstracts callback effects;
it does not prove arbitrary callback termination or Python/TLA refinement.

At the plugin-host boundary, cancellation during async start or late publication
uses the same rollback path and then propagates the original `CancelledError`.
A cancelled async cleanup remains owned for retry while other callbacks drain.
`PluginPublicationLifecycle.Fail` abstracts this transition alongside ordinary
exceptions; runtime tests are required to check both Python exception categories.
