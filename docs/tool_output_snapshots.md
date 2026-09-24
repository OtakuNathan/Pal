# Tool output snapshots

Large tool output is delivered as a bounded head/tail preview and the path of an
immutable UTF-8 output file. The file contains the complete rendered result of
that invocation. Search it with `rg` or read selected lines with `read_file`.
`read_file` keeps its existing line/range schema; unusually long lines can be
examined using ordinary shell text tools. Business query pagination is unchanged.

The file is a historical copy, not a claim about the current resource. A fresh
read/query may be appropriate when current state is needed. Never automatically
repeat a side-effecting command to recover output. Failure status, effect outcome,
and next-action affordances remain outside the output preview. The preview budget
does not remove the required status/path envelope when the configured budget is
smaller than that envelope.

## Ownership

Execution owns `ResultSnapshotStore`; IR carries typed `ResultSnapshotRef` values.
Only explicit IR references acquire ownership. An arbitrary path mentioned in
output or source text does not. L1 acquires successor references before releasing
old references during replacement; prepublication validation rejects malformed
paths. Pending delivery, snapshot reads and an in-flight model request also pin
files. Once the last owner is gone, Execution deletes the managed file.

Reading a snapshot does not create another snapshot and grants no edit authority
over the original resource. File tools reject modification of a managed copy.
This is a file-tool contract, not a sandbox against arbitrary shell commands.
Source-file editing still requires a valid delivered `read_file` of that source.
When a source read itself overflows, only the source ranges actually shown in its
preview acquire authority; the full copy does not grant authority for omitted text.

Compact may return `retained_result_refs` containing IDs from its source inventory.
Unknown IDs are rejected. The summary acquires the selected references in the same
L1 replacement that retires old L; R is unaffected. Omitted references retire once
in-flight pins are released. Snapshot refs persist in runtime checkpoints; restore
reconstructs L1 ownership before sweeping orphaned managed files. No user-turn TTL
is applied. Missing files are unavailable evidence, not replay authorization.

Resident files live under `data/result-snapshots`. Bunshin uses its own writable
run directory; it does not acquire access to other roles' output. A persisted run
must retain its output directory alongside its checkpoint.

## Shell and compatibility

The built-in shell and native extension copy oversized stdout/stderr incrementally
before their original output resources retire. Native observations copy captured
byte intervals; remote transport keeps its existing incremental download and
capacity rules. Snapshot creation and L1 delivery precede output acknowledgment.
Failed delivery retries output preparation, never command execution. ACK retry
does not create another L1 event.

`read_tool_result` and the `paged` invocation variant are retired, including
legacy handle storage, restoration/migration, and pager TTL configuration. The
execution state port accepts only the current snapshot format. Output snapshots
live by L1 ownership and active pins, not a turn-count TTL. Business-query limits
and cursors remain part of their respective tool contracts.

No runtime activation or model API calls are part of this change's offline tests.

Offline streaming sample on the development host: copying 32 MiB of stdout into
an immutable snapshot used about 1.01 MiB peak traced Python allocations and took
0.44 seconds; the preview was 1,026 characters. This measures the copy/preview
path only, not process RSS, remote transport, or model behavior.
