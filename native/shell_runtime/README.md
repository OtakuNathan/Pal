# Native shell runtime prototype

An opt-in, in-process CPython extension built with Flux Foundry, dynabridge and
statically linked libuv. This directory does **not** replace `run_shell`, alter
Pal startup, or change the installed wheel. The Python adapter and acceptance
host are deliberately separate from production modules.

## Build and test

Requirements: Linux or macOS, CMake >= 3.20, a C++17 compiler, CPython development
headers/library, a **PIC static** libuv archive, and Pal's test dependencies.
The tested dependency baseline is:

| Dependency | Revision |
| --- | --- |
| Flux Foundry | `781375ab57884cbccb84b9d91133c2b2a22a95e9` |
| dynabridge | `0762d81175a6f3172b4a142b026d8392150fca6f` plus the lifetime fix below |
| libuv | `v1.50.0` |

The bridge fix destroys queued task captures **before releasing the GIL**. A
capture can own Python references even after its callable has returned. The
local sibling checkout already contains the fix; do not apply it twice. For a
fresh checkout at the pinned revision:

```bash
git -C ../bridge apply "$PWD/native/shell_runtime/dependencies/bridge-gil-lifetime.patch"
```

From the Pal repository, with your chosen Python environment active:

```bash
python -m pip install -e '.[test]'
cmake -S native/shell_runtime -B /tmp/pal-native-shell-build \
  -DCMAKE_BUILD_TYPE=Debug \
  -DPython3_EXECUTABLE="$(command -v python)" \
  -DFLUX_FOUNDRY_INCLUDE_DIR="$PWD/../flux_foundry" \
  -DDYNABRIDGE_INCLUDE_DIR="$PWD/../bridge"
cmake --build /tmp/pal-native-shell-build --parallel 2
ctest --test-dir /tmp/pal-native-shell-build --output-on-failure
```

If libuv is not installed as a PIC static library, build its pinned source with
`-DCMAKE_POSITION_INDEPENDENT_CODE=ON -DLIBUV_BUILD_TESTS=OFF`, target `uv_a`, and
pass `-DLIBUV_STATIC_LIBRARY=/absolute/build/libuv.a` and
`-DLIBUV_INCLUDE_DIR=/absolute/libuv/include` to the prototype configure command.
The resulting `_pal_shell_runtime.<python-abi>.so` contains the native manager,
executors and libuv; it still depends on the platform C/C++ runtime and matches
one CPython ABI. It is not a portable, cross-platform single binary.

A pull-request and manually dispatched [workflow](../../.github/workflows/native-shell-prototype.yml)
builds static libuv and runs the suite on Linux and macOS, applying the pending
bridge patch only inside its isolated dependency checkout. It does not modify
the normal Pal CI or packaging. Remote CI must actually run before claiming
macOS acceptance.

## Try the adapter

```bash
export PYTHONPATH="/tmp/pal-native-shell-build:$PWD/native/shell_runtime/python:$PWD/src"
python - <<'PY'
import asyncio
from pal_shell_prototype import ShellRuntime

async def main():
    runtime = ShellRuntime()
    try:
        short = await runtime.run("printf hello")
        assert short["session_id"] == 0
        background = await runtime.run("sleep 1; printf done", wait_ms=0)
        result = await runtime.read(background["session_id"], wait_ms=5000)
        print(result["status"], result["stdout"])
    finally:
        await runtime.close()

asyncio.run(main())
PY
```

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

The event source waits for a safe turn boundary. A background result becomes a
new runtime-observation L1 turn, never an extra tool result appended to a closed
call. The acceptance host uses an injected consumer and spends no model tokens.
Consumer errors retain the event for explicit retry; failed acknowledgement after
successful consumption does not invoke the consumer again. This is an in-memory
acceptance path, not durable exactly-once delivery across crashes.

## Prototype boundaries before production cutover

- Test the configured macOS job. Local acceptance was on the Raspberry Pi/Linux
  arm64 with CPython 3.13.5, GCC 14.2 and libuv 1.50.0. A local Release comparison is recorded in
  [benchmarks/file-handoff.md](benchmarks/file-handoff.md); correctness tests alone do not
  establish performance.
- Wire the shared owner through production runtime/role construction and expose
  the session tools there. The prototype's Bunshin factory is explicit; existing
  production overlays have not been changed.
- Integrate session handoff acknowledgement with the actual tool-result commit
  boundary, choose completion scheduling policy, and reconcile existing shell
  and Bunshin timeout schemas. The adapter currently acknowledges its own return
  boundary; this alone is not a production tool-result delivery guarantee.
- This backend assumes normal POSIX child ownership: no external SIGCHLD reaper,
  and no descendants deliberately escaping the owned process/terminal groups.
  It is not a process sandbox. An escaped descendant retaining an output FD can
  prevent PTY EOF and delay cleanup. Spawn's exec-error handshake is synchronous on
  the native loop and is not yet covered by a cancellable startup deadline.
- Main CPython interpreter only; close all runtimes before finalization. No
  subinterpreter, free-threaded Python, module hot-unload or Windows support is
  claimed. Registration metadata intentionally lives until process exit.
  Native callbacks must schedule host work and must not destroy their runtime;
  calling `close()` from its callback is rejected. The adapter uses weak callback
  ownership and serializes concurrent close calls.
- No resident Pal deployment, packaging switch or migration is part
  of this prototype. Production `run_shell` remains the rollback baseline.

## Local verification — 2026-09-09

- Debug extension compiled on the Raspberry Pi; `ldd` shows no shared libuv.
- Latest UBSan build (`-fsanitize=undefined -fno-sanitize-recover=all`): all
  **38 native/host tests** and the **1,000-capture GIL lifetime test** passed.
- Existing shell tests and the original shell/full-output pager regression: **9 passed**.
- The dependency patch was applied to a fresh archive of the pinned bridge
  commit and reproduced exactly the three intended modified/new files.
- Linux/macOS remote workflow runs for prototype pull requests and supports manual dispatch.

To repeat the sanitizer run, configure another build directory with the same
arguments plus `-DCMAKE_CXX_FLAGS="-fsanitize=undefined -fno-sanitize-recover=all"`,
then build and run CTest there. This verifies undefined-behavior instrumentation;
it is not an ASan, TSan or performance result.
