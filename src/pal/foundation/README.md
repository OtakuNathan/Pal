# foundation

Owns:
- async and process-facing infrastructure contracts
- database lifecycle, transaction scope, external-schema assumptions, raw SQL hook registry
- shared low-level primitives that do not carry business semantics
- owned worker joining through repeated cancellation, preserving worker failures as cancellation causes
- generation-fenced ownership for fd-backed resource graphs

Does not own:
- memory semantics
- channel routing policy
- execution governance
- tasking rules

Exposes:
- `WriterPreferredRWGate` (generic cancellation-safe lifecycle admission)
- `EventEnvelope`
- `PalV2Database`
- `sqlite_database.OwnedSqliteDatabase`
- `RawSQLHookRegistry`
- `RepositoryBase`
- `FdLease`, `FdCapability`, and `FdCancellationControl`
- owner-side error presentation: `exception_reason` logs complete redacted
  evidence and returns a concise cause; `diagnostic_reason` handles external
  reports. These helpers never infer execution effects or recovery policy

Cause selection follows explicit `raise ... from ...` chains. Implicit exception
context can describe an earlier operation whose recovery failed, so it remains
log evidence rather than replacing the current failure. Owner-authored public
reasons preserve operation facts such as an already-applied write, including
when reached through an explicit cause chain or an exception group.
Cause projection removes frames and redundant wrappers without a default
character cap; long causes and recovery facts remain intact. Display surfaces
that need a preview must request an explicit limit.

`fd_lease` treats an SDK client, socket, and their hidden responses or streams
as one private resource graph. I/O requires a generation-fenced
capability; operations may return detached value data only, so the raw graph
and nested handles cannot be exported. Retiring prevents new acquisition
while existing capabilities drain. Cancellation is signal-only. Forced revoke
tombstones existing capabilities and invokes resource-specific interrupts,
then waits for admitted calls to quiesce before the one physical close. A
drain timeout does not close: the still-bound graph is held strongly in
quarantine rather than risking fd reuse. A closer must explicitly prove
detachment before the slot may publish a new generation.

Subprocess ownership stays with the component that spawned the process. Its
private ``Popen``/``asyncio.subprocess.Process`` reference is the only control
authority; numeric PIDs and PGIDs are never durable capabilities. Cancellation
derives a group id from the currently owned process for one terminal signal,
then discards that authority and waits only for the direct child.

The executable lifecycle model is `spec/foundation/FdLeaseLifecycle.tla`. It
imports `FdLeaseImplementationTopology.tla`, which is generated from the exact
control and capability transition tables used by `fd_lease.py`. The check
script verifies the safe model and requires both cached-fd reuse and premature
generation publication to produce counterexamples.

Rule:
- schema migration is external to `PalV2`
- runtime initialization assumes the database has already been migrated or
  otherwise prepared before startup

`OwnedSqliteDatabase` owns every Peewee worker connection. After the caller has
drained admitted operations, owner cleanup closes them under Peewee's connection
lock and clears their actual thread state. Failed closes retain ownership for
retry and do not prevent cleanup of other connections; Memory retains its reader
lease until cleanup succeeds. Per-thread connection contexts retain Peewee's
ordinary scope, and later explicit reuse opens fresh connections. No per-query
health checks are added.

`WriterPreferredRWGate` fences tools and local control calls against lifecycle
writers. An async task can nest shared calls under its admitted reader even when
a writer is waiting; child tasks acquire independent leases. Cancellation drains
pending acquisition before releasing it, including repeated cancellation.
An exclusive async owner can nest notifications under its writer. Upgrading an
admitted shared call to exclusive access is rejected; callers must declare their
exclusive or owner-managed admission at the outer boundary.
