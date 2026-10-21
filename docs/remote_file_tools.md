# Targeted file tools and worker authority

`read_file`, `edit_file`, `write_file`, and `run_shell` accept `target`, defaulting
to integer `0` for local execution. The remote plugin resolves configured IDs,
names and shortcuts. String matching is exact; `"5"` is not converted to `5`.
Unknown or ambiguous aliases fail closed. Results identify the numeric target
and configured name, for example `surface (5)`.

The target routing registry belongs to the remote plugin. The file authority
registry belongs to the worker that accesses the filesystem. Every remote
operation pins the target configuration digest, authenticated worker identity,
worker epoch and logical context scope. The host never canonicalizes remote
paths. An old worker can still serve shell calls; file tools require advertised
`file_protocol_version=1`.

## Delivery and mutation

The worker stages a read result without granting edits. Pal budgets the returned
text and records exactly the delivered source spans in L1. Only then does it
synchronize the result ID and manifest to the worker. A complete visible line
can authorize a focused edit; an incomplete line cannot. Every occurrence of a
`replace_all` match must be covered. Replacing an existing file requires complete
coverage of its current version. Immutable output snapshots never grant source
file authority.

Scope synchronization uses monotonic revisions. Duplicate messages are
idempotent, conflicting revisions are rejected, and retired result/operation
identities cannot be resurrected by delayed ACKs. Each result owns its own
contribution. Removing one result preserves other live references; removing the
last reference releases the staged proof. History changes, failed delivery,
compaction, transient scope completion and reset all request retirement.
The current model request's visible result IDs are synchronized as well: a
retained result omitted from that request cannot authorize a mutation. Both
local and remote file tools apply this projection; showing the retained result
again restores its contribution without reviving a retired result ID.

Pal keeps delivery intent and unresolved operation IDs in its checkpoint. It
does not keep a second authoritative file-state cache. A disconnected worker
cannot acknowledge immediate physical cleanup. Pending revisions remain dirty,
and a dependent operation must synchronize before admission. A changed worker
epoch gets a new scope without inherited grants and requires a new read.

Mutations reuse the authenticated RPC operation journal. An operation ID is
bound to the request fingerprint. A lost response is reconciled by querying that
ID; its arguments are never automatically replayed. The next file call reports
the recovered receipt before admitting another operation. The journal is scoped
to a worker epoch: loss of the worker itself is not proof that a write failed.

Large requests and results use bounded chunks with total size and SHA-256
validation. The protocol permits at most 32 MiB per transfer, subject to the
worker's retained-memory limit; `write_file` retains its existing 1,000,000-byte
content limit. Once downloaded, full worker output can be retired while a small
operation receipt remains queryable. Pal's normal output budgeting and immutable
snapshot retention own the delivered copy.

## Shared source and platform behavior

`scripts/export_file_core.py` exports the authoritative execution file modules
into the worker's private `_file_core` namespace. Only imports are rewritten;
the read, range, mutation, snapshot and authorization algorithms are identical.
Standard-library boundary types replace Pal's facade models in the standalone
package. `SOURCE.json` records the base Pal revision and exact source hashes;
`--check` detects source or vendor drift. Regenerate the export after changing a
listed source file. Workers do not install or import Pal.

POSIX uses advisory `flock`; Windows uses a one-byte `msvcrt` lock. Both use the
same lock stripes, exact pre-image check, UTF-8 byte preservation, file flush and
atomic replacement. POSIX also synchronizes the parent directory and reports
post-commit durability errors as applied, with no retry. Windows has no portable
directory fsync in this implementation; it does not claim that durability step.
Non-cooperating external writers can bypass advisory locks.

## Activation

This change affects resident execution code as well as the remote plugin and
standalone worker. Source copies and built packages do not activate it. Install
the matching Pal revision and worker bundles, then activate the resident code
through the normal idle restart boundary and reload the remote plugin. Preserve
existing `remote.toml`, keys and worker configuration. Defer activation while Pal
has active work. Pair the native repository's Pal CI reference with the published
Pal revision before releasing the plugin; the export check verifies the pairing.
