# foundation

Owns:
- async and process-facing infrastructure contracts
- database lifecycle, transaction scope, external-schema assumptions, raw SQL hook registry
- shared low-level primitives that do not carry business semantics
- generation-fenced ownership for fd-backed resource graphs

Does not own:
- memory semantics
- channel routing policy
- execution governance
- tasking rules

Exposes:
- `EventEnvelope`
- `PalV2Database`
- `RawSQLHookRegistry`
- `RepositoryBase`
- `FdLease`, `FdCapability`, and `FdCancellationControl`

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
