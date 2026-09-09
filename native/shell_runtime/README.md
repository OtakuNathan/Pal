# Native shell host integration

The native C++ implementation is maintained and released separately as
[pal-shell-native](https://github.com/OtakuNathan/pal-shell-native).
Read its README for release wheels, source builds, compatibility, agent-assisted
installation and rollback. The ordinary Pal wheel keeps Python as its default
shell backend; it does not contain or build the optional native extension.

Pal owns the Python adapter, tool contracts, paging and lifecycle integration in
`src/pal/execution/native_shell`. This directory retains the acceptance host,
full integration tests and historical benchmarks. Native process implementation,
CMake build and dependency patches belong to the extension repository.

## Enable

Install a compatible `pal-shell-native` wheel into Pal's actual Python environment
(or a versioned module directory on its launch `PYTHONPATH`), test it in a separate
process, then set `PAL_SHELL_BACKEND=native` in the launch environment. Report the
changes and notify the user to restart. Do not replace a loaded binary in place.
Unset the variable or set it to `python` and restart to revert. An unavailable
native module is a startup error when selected; commands never silently replay
through the fallback backend.

The switch applies to resident bootstrap and standalone Bunshin roles. Each role
owns its own runtime, processes and sessions. Bunshin can reload Python host code
through its normal remount lifecycle; replacing a loaded native module requires
a new process.

## Resident and role lifecycle

The resident keeps `run_shell` direct. `shell_session`, `shell_status`, and
`shell_recover_output` are indirect and discoverable from its hints. `shell_status`
reports the selected backend, sessions, retained output calls and notification
failures. `retry_notification` on `shell_session` explicitly retries a failed
completion turn after reconciling its previous effects.

The resident acknowledges a returned session after its tool result is written to
L1. Interrupting before that boundary terminates the undelivered session; after
it, the background session continues. Terminal files are retired after validated
output/paging and L1 delivery. Embedded callers without an active Core turn use
the return boundary. Output delivery failures retain their files and return a
concrete `shell_recover_output` action, which never repeats the command.

A completion waits for an idle resident with no queued user turns or unfinished
post-turn tasks, then runs a
new agent continuation using the captured originating delivery binding. Its input
is an L1 `runtime_context_artifact`, never an extra result on a closed tool call.
Output inherits the original budget and uses the existing pager. Tool-only calls
without a captured delivery binding retain results for explicit reads and cannot
start unsolicited model turns. Failed notifications are retained for manual retry;
acknowledgement-only retries do not repeat a successful model turn.

Reset and graceful shutdown cancel/reap native children and invalidate their
session IDs. Graceful shutdown closes native execution before saving the existing
resident L1 checkpoint. Live processes and unconsumed native files are **not**
persisted across restart; already delivered pager results retain normal checkpoint
semantics. Snapshot/restore while native sessions are outstanding is rejected.

### Bunshin roles

Roles use the same run/session contracts, output budget, pager, recovery and
cancellation implementation. Shell-enabled roles discover `shell_session`,
`shell_status` and `shell_recover_output` indirectly. The sandbox mounts only the
native extension file read-only; it does not expose the resident's runtime data.

Before the next model round, the role runner waits up to five minutes for a
noninteractive background command, with Manager heartbeats and responsive
cancellation. A PTY returns control to the model for input. Completions enter the
same role's L1 as runtime context artifacts; they never wake the resident or
another role. Output files are released after that observation is committed.
Failed deliveries use the normal explicit output-recovery tool.

Outstanding sessions or undelivered output block role writes, submission and
restart-safe checkpoints. A cooperative restart waits for a safe point; cancellation
and failed/blocked role exit reap remaining children before terminal notification.
Verification wrappers wait for terminal shell results before recording evidence,
and retain the output until the outer tool result is committed. They reserve host
write admission while their nested shell owns the native process lease.
Forced process termination does not preserve shell sessions; an earlier role
checkpoint cannot restore those processes or prove their external effects.

## Integration tests

From a Pal checkout with its test dependencies and the extension installed in the
active interpreter:

```bash
python -m pip install -e '.[test]'
PYTHONPATH="$PWD/native/shell_runtime/python:$PWD/src" \
  python -m unittest discover -s native/shell_runtime/tests -v
```

The [native workflow](../../.github/workflows/native-shell-prototype.yml) installs
the extension from a pinned revision and runs these tests on Linux and macOS.
Backend CI separately validates binary wheels, source builds, GIL lifetime and
integration against its known-compatible Pal baseline. These suites use isolated
hosts and do not call a model or restart the user's resident.

## Contract

- `run(cmd, cwd='', tty=False, wait_ms=None, timeout_ms=None, shell='/bin/bash')`
  waits up to **300,000 ms** normally, **1,000 ms** for a PTY. `wait_ms` only
  controls the initial response; expiry exposes a live session. `timeout_ms`
  is a separate optional hard deadline, measured after successful spawn.
- Completion before the initial handoff returns `session_id=0`. Background
  handles are nonzero and belong to their creating runtime. Reset invalidates
  all old handles. There is no command replay or automatic retry.
- `read(session_id, wait_ms=0)` returns a nondestructive snapshot. Multiple
  readers do not steal bytes from each other. Cancelling a read cancels its
  wait, not the process. A terminal read acknowledges consumption.
- `write(session_id, text)` queues PTY input (up to 64 KiB); acceptance means
  queued, not processed by the application. `resize(session_id, rows, columns)`
  sets the terminal size. PTY output is merged; ordinary stdout/stderr are
  separate and ordinary stdin is `/dev/null`.
- `terminate(session_id)` requests controller cancellation. It sends TERM,
  then KILL after one second if needed. Terminal status and write-gate release
  wait for actual exit, output drainage and reaping, not just FF cancellation.
- `release(session_id)` retires a completed result; running sessions must be
  terminated first. Up to 32 completed results are retained. Unacknowledged
  results are never silently evicted: the manager rejects new work when full.
- Full output is preserved in private temporary files (directory 0700, files
  0600). Ordinary stdout/stderr write directly to those files; C++ drains PTY
  output into its stdout file. There is no head/tail truncation.
- The host derives the native inline transport limit from the existing call
  budget's `_resolve_char_limit`. If the combined output bytes fit (including
  equality), native sends values; otherwise it sends file metadata. Using bytes
  here is conservative for UTF-8. The original runtime still makes the exact
  pagination decision on the validated/serialized result, including JSON
  escaping and metadata. No separate model-visible truncation policy exists.
- Standalone adapter calls default to file transport (`inline_limit=0`); the
  Pal handler passes its actual budget. With no output limit the host requests
  inline output (`-1`), matching unbudgeted full-result calls. The handler reads
  file snapshots off the asyncio thread and hands the complete decoded output
  to the existing `result_handle` / `read_tool_result` mechanism.
- `output_id` identifies a retained native output independently of the public
  session ID, including one-shot calls whose `session_id` is zero. Native files
  survive until `release_output`, completed-result eviction after consumption,
  or close/reset. Releasing an already retired output is idempotent. Paths and output ownership IDs are internal and are stripped
  from model-visible results. The native callback carries bytes only on the
  inline branch; it otherwise carries sizes and paths.
- A standalone one-shot call releases files after materialization. The Pal
  handler uses `retain_output=True, load_output=False` and releases only after
  successful validation/pager storage. File-read or pager failures preserve the
  pending handoff; the isolated host's `retry_output(call_id, ...)` retries that
  handoff without executing the command again. Background completion inherits
  the original call's budget and releases files after successful consumption.
- Existing pagination still materializes complete strings in Python. File
  transport bounds native output buffering, not Python memory or disk usage.
  Snapshots read exactly the byte count observed by native code and decode
  invalid UTF-8 with replacement, as the original shell does.
- Every shell is a write. One native owner reserves each execution context;
  other writes/control operations are rejected while it is active. Reads,
  session controls and explicit host reset remain possible. Other contexts
  are independent. No shell-command parsing decides safety or parallelism.
- Interrupting an originating turn cancels its still-foreground command.
  A session already handed back to the caller continues until completion or
  explicit termination. Cancellation during handoff cleans up the unreturned
  session. `close()` joins native workers; `reset()` closes and creates a new
  runtime. Always await close before interpreter/event-loop shutdown.

`export.def` is the lower-level request/callback ABI. Request IDs must be unique
and nonzero; zero is reserved for unsolicited completion events. The adapter
owns these IDs and acknowledgements. Use it instead of managing this protocol
from ordinary Pal code.

## Ownership and Pal integration

Each runtime owns one libuv reactor thread and one GIL executor worker. An active
child additionally has one native wait thread. Manager operations and controller
cancellation run on the reactor via the ff executor; no Python object is touched
there. The POSIX backend establishes a process group and optionally a controlling
PTY. `waitid(WNOWAIT)` keeps the leader's identity reserved while remaining owned
process-group members are signalled; `waitpid` finally reaps it.

The native awaitable retains its backend lifetime across cancellation. The
manager requires both FF settlement and OS cleanup before completing a session.
A background completion is emitted once. The GIL worker converts the result and
schedules delivery with `loop.call_soon_threadsafe`; the asyncio thread alone
resolves futures and updates Pal state. Callback failures are counted and the
terminal native result remains readable.

`pal_shell_host.py` creates a real, isolated PalCore, MemoryService and
ExecutionRuntime, adds `prototype_run_shell`, and leaves the original tool
available for comparison. The write gate checks resolved tool records, including
indirect dispatch. A separate projection factory demonstrates sharing the same
owner with an actual Bunshin registry overlay.

### On-demand session tools

The acceptance host keeps `prototype_run_shell` as its only direct native entry.
Its compiled `NextToolHint` points to the **indirect** `shell_session` tool:

```text
read_tool(name="shell_session")
call_tool(name="shell_session", args={"session_id": 123, "action": "read", "wait_ms": 300000})
call_tool(name="shell_session", args={"session_id": 123, "action": "write", "text": "answer\n"})
call_tool(name="shell_session", args={"session_id": 123, "action": "resize", "rows": 32, "columns": 100})
call_tool(name="shell_session", args={"session_id": 123, "action": "terminate"})
```

Use a real returned ID in place of `123`. `release` explicitly discards a
completed session's retained output; a successfully delivered terminal snapshot
is released automatically. Session actions validate their own argument sets,
and input/resize require a live PTY. Session controls bypass the write gate so a
command holding that gate can still receive input or be stopped; other writes
remain blocked.

Running results include concrete read/terminate affordances. Only PTY results
suggest discovering input/resize arguments; terminating results suggest waiting
for exit. These affordances remain outside the paged body, alongside
`read_tool_result`, even when the initial result is too small to contain the
session ID. One-shot and terminal results do not suggest further session actions.
Guidance distinguishes response waits from hard deadlines, discourages repeated
short polling and command replay, and explains stale handles and uncertain input
delivery. Full-output retention and pagination use the existing budget path.

A terminal session read only consumes output after successful normalization and
pager storage. This also removes a completion that was already queued while
another turn was active, avoiding duplicate observations of released files.
Failed handoffs remain recoverable with the host's existing `retry_output` method.
The same contracts are used by the opt-in resident integration described above.

The event source waits for a safe turn boundary. A background result becomes a
new runtime-observation L1 turn, never an extra tool result appended to a closed
call. The acceptance host uses an injected consumer and spends no model tokens.
Consumer errors retain the event for explicit retry; failed acknowledgement after
successful consumption does not invoke the consumer again. This is an in-memory
acceptance path, not durable exactly-once delivery across crashes.

## Boundaries

Live native processes and undelivered files do not survive restart. Delivered
pager results retain Pal's normal checkpoint semantics. The backend is not a
sandbox; Pal's existing sandbox and tool admission policies still apply.
See the extension README for supported Python/platform versions and native
process ownership limits. Benchmark reports here describe historical runs;
extracting the package does not itself establish a new performance improvement.
