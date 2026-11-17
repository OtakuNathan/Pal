# Managed L3 generation swap

Memory owns the published data generation and backend identity. Plugins provide
backend implementations; unloading a plugin withdraws availability without
changing the committed identity or selecting `null_l3`. Backend replacement uses
a managed migration instead of assigning `active_provider_id`.

## Identity and publication

L3 identities are `(l3, provider_id)` and must match the module id
`l3.<provider_id>`. Execution infrastructure references use
`<module_id>:<local_name>` and declare their owner. An occupied name rejects a
claim; release requires the original object. PluginHost checks an occupied
module id before importing or starting the candidate. Registration repeats
the ownership check atomically to cover intervening claims.

The shared ownership reducer drives module, event-source, Execution and L3
registries and the generated registration model. L3 providers are registered
with Memory rather than duplicated in Execution's infrastructure registry.

## Online migration

```sh
pal memory swap sqlite_vec_l3 --runtime-root /path/to/runtime --json
pal memory swap-status JOB_ID --runtime-root /path/to/runtime --json
pal memory swap-cancel JOB_ID --runtime-root /path/to/runtime --json
pal memory swap-resume JOB_ID --runtime-root /path/to/runtime --json
```

The target must already be mounted and expose the declared
`SqliteGenerationPort` capability. The current implementation migrates canonical
SQLite generations in the shared Memory catalog. Backends without that
capability, and unrelated catalogs, are rejected explicitly. This does not claim
to convert arbitrary third-party database formats.

The operation returns a durable job id before copying or indexing completes.
Status distinguishes private preparation, committed activation, pending indexing,
completion, and errors. `swap` can rebuild the currently selected backend into a
new generation. Backend configuration is supplied by its implementation; online
swap does not edit runtime configuration or replace the separate offline
embedding-configuration migration command.

| Phase | Production boundary |
| --- | --- |
| `preparing` | Capture source identity, target embedding configuration fingerprint and durable job ownership. |
| `copying` | Back up one consistent WAL snapshot and its change cursor into a private database. |
| `catching_up` | Prepare basic retrieval and pending target embeddings, then replay concurrent content changes. |
| `ready` | Acquire Core maintenance admission, fence all source writes, replay the tail and verify complete content and replay receipts. |
| `committed` | In one catalog transaction, compare-and-swap the head, record the target provider/profile, retire the source and update job phase. |
| `indexing` | Bind the published generation and rebuild vectors in bounded batches. |
| `complete` | The migration's pending index queue drained. Later ordinary writes can enqueue more work. |
| `superseded` | Another publication changed the source or replaced this generation. The job cannot overwrite that publication. |

SQLite triggers capture content, topics, mutations, batch receipts and revisions
in the same transaction as the write. The journal coalesces each row key to its
latest monotonically increasing change sequence. Replay reads final rows under
a consistent source snapshot, including deletions. Derived FTS/vector indexes
are excluded from content truth. Before publication, Memory hashes all content
tables and replay receipts; matching a cursor alone is insufficient.

The final fence drains admitted writers through the generation writer lock and
sets a database write guard. Triggers then reject writes even from a retained
SQLite connection. Thaw never makes a retired generation writable. Catalog
tombstones remain shared by all generations, including pinned old readers.

New embeddings are queued with the target profile and old vector indexes are
discarded. While indexing is pending, recall uses exact/lexical/topic retrieval
without synchronously calling the embedding transport. Index failure preserves
the active generation and all subsequent writes; resume continues indexing that
generation. Configuration fingerprints include endpoint configuration, model
revision, processing identity or implementation-specific dimensions. A changed
target identity blocks resume instead of mixing vector spaces.

Every generation database pins its full embedding profile in
`memory_embedding_space`, alongside the catalog binding. Its first vector fixes
the dimension too. Reload validates the current generation as well as retained
ones. A changed or unknown identity permits exact/lexical/topic reads, blocks L3
mutations and indexing, and never calls the mismatched embedding transport for
recall. A managed swap rebuilds a private generation before enabling the new
profile. Unpublished candidates receive no published provider binding during
catalog initialization.

Vector inserts and updates maintain their native index entries in the same write
transaction. Correction and Dreaming retirement remove old entries atomically,
retaining their vector blobs for revision history. Index maintenance errors roll
back the operation. Read-only readers can search newly committed vectors without
depending on a later writer query or a count mismatch to rebuild the index.

Indexing rechecks the captured identity after transport returns and validates
every response dimension before saving a batch. Repository writes also enforce
the pinned profile and dimension in the write transaction. Query replies from a
changed model are discarded. Private preparation removes source vectors, vector
indexes and embedding metadata before declaring the new space; publication
checks the database profile against the migration job and validates metadata and
dimensions. Offline migration follows the same preparation, while an interrupted
batch already belonging to the target space is retained.

Imported databases without vector provenance retain canonical memory content;
their unknown derived vectors are rebuilt. An existing managed generation whose
provenance is missing remains basic-read-only until an explicit managed swap.

## Recovery and lifetime

The catalog is the durable authority. Losing publication acknowledgement or
runtime rebinding never republishes the source. The worker re-reads the durable
job, repairs the committed binding and retains errors for status/resume. A crash
before commit leaves the source current; startup can release an abandoned fence
only after acquiring its writer lock. A live owner prevents this recovery.

Backend `bind_generation` derives indexing state from the committed catalog;
callers do not supply an indexing flag. Dreaming uses this same boundary for
publication and recovery, refreshing repository, embedding identity and index
availability together instead of retaining its source generation's state.

Job cancellation closes that job's launch admission before joining its worker,
so recovery timers cannot replace it between task completion and the durable
pause write. Worker threads and fence cleanup finish before module leases are
released. An explicit cancellation is durably paused: provider-registration
scans and host restart leave it stopped until `swap-resume`. A superseded job is
retired from catalog facts before looking up its former backend, even if that
plugin has already unloaded. Core retains only the exact source/target modules, so unrelated
plugin lifecycle operations remain available. Resident quiescence drains Memory
jobs before plugin cleanup. Module-owned startup timers discover unfinished jobs
after backend registration; headless worker hosts do not start resident jobs.

Memory's shared worker join is also used by Dreaming. Repeated cancellation
cannot detach its worker from shutdown; a candidate acquired during cancellation
is still closed, and final cleanup drains before storage ownership is released.

Each published generation records its own provider identity and embedding profile.
Existing workflow pins keep the old data and identity across restart. A reader
whose embedding configuration does not match its pinned generation uses basic
retrieval instead of querying old vectors with a different space. Current and
previous generations, live workflow pins, reader leases and unfinished migration
jobs prevent collection. Unreferenced private candidates follow retention too.

Retained generations from before embedding constructor metadata was recorded
keep basic read-only retrieval without constructing a default embedder. Present
but invalid metadata still fails explicitly. Bunshin adopts legacy workflow pins
only when a matching workflow snapshot exists, under the workflow creation lock;
independent reader pins remain independent. Standalone Manager startup upgrades
an existing catalog before reconciliation, without creating generations or
resuming incomplete embedding migrations.

## Model and implementation correspondence

`memory/swap_state.py` is the production reducer, publication guard and recovery
launch decision.
`memory/swap_formal.py` executes every finite input and generates
`spec/memory/MemorySwapReducer.tla`; CI rejects drift. The checked model imports
that relation rather than maintaining another set of phase decisions.

The bounded model includes source mutations/deletions, snapshots and catch-up,
fencing, a competing publication, cutover, retained readers, crash/recovery,
post-cutover writes and index failure. Its positive case checks data preservation,
commit authority, retired write rejection and reader retention. Four fault mutants
must violate their named invariants; a witness must reach completion after a
source mutation, crash, index failure and a new-generation write.

`MemorySwapRecovery.tla` imports the generated launch decisions to check durable
pause and stale-job retirement without the old backend. Two fault mutants expose
automatic restart of a paused job and premature backend lookup; a witness reaches
explicit resume after cancellation, restart and a paused startup scan.

`MemoryEmbeddingIsolation.tla` imports embedding access and response guards
generated from `memory/embedding_space.py`. It checks reload, unknown provenance,
in-flight responses, dimension changes and private-generation publication. Four
mutants expose mixed model writes, stale replies, mixed dimensions and copied
source vectors; a witness reaches vector retrieval after a blocked configuration
is migrated. Model identities represent distinct spaces even when widths match.

`tests/test_memory_swap.py` exercises the real catalog, database triggers, Core
admission, module leases and restart timer. Registration conformance also applies
every ownership input to the four real registries. This combines an exact
generated decision relation with executable side-effect correspondence checks;
it is not a proof of every Python statement or arbitrary external backend code.

```sh
python scripts/run_tlc.py --inventory-only
python scripts/run_tlc.py --jar tla2tools.jar --group memory --output /tmp/pal-memory-swap-check
python -m pytest -q tests/test_memory_swap.py tests/test_registration_ownership.py
```

Regenerate the relation after changing production rules, then rerun TLC:

```sh
python - <<'PY'
from pathlib import Path
from pal.memory.swap_formal import render_swap_relation
Path('spec/memory/MemorySwapReducer.tla').write_text(render_swap_relation())
PY
```

Backend migration and Dreaming now share `GenerationMaintenance`, `SwapStore`
and its generated production reducer. Switching backends announces Pal sleep before
admission proceeds into the write fence; channel notices wait for delivery. A
failed mount after commit keeps ordinary ingress closed until recovery binds the
published generation. Semantic Dreaming candidates join this transaction after
review; unreviewed migrations preserve canonical content exactly. Bunshin holds
references to old generations without copying databases, and owns workflow-reference
release independently of sleep outcomes.

Maintenance release is conditional on the exact admission token. Recovery captures
that token with its status snapshot; Core rechecks identity under its transition
lock when releasing. Original cleanup that resumes after a later owner is admitted
has no effect on that later owner. The ownership model imports this production
identity rule and covers both original-close and stale-recovery interleavings.
