# Socket interruption and backpressure recovery

## Ownership boundaries

- Core isolates ordinary synchronous and asynchronous output-port abort failures
  in `channel.abort_stream_failed` diagnostics and continues execution cancellation.
  It does not install a blanket host exception handler or swallow `CancelledError`.
- Channel clears its hub and shared stream outbox before invoking endpoint abort.
  An endpoint failure cannot keep channel-owned cancelled deltas alive; the
  endpoint may enqueue fresh terminal notifications for retry.
- Socket abort still clears endpoint stream tracking and cancelled updates first.
  If the atomic two-frame error/done enqueue cannot fit, only those two terminal
  updates enter the existing ordered outbox retry path. Other delivery errors keep
  their existing classification. No queue capacity increase is involved.
- Ordinary stream updates must continue to raise retryable backpressure. Swallowing
  it inside `send_stream_update` would falsely acknowledge unsent data to the
  outbox and lose updates.

## Stalled peers

Each socket frame has a 30-second combined drain/ACK deadline, controlled by the
endpoint's `delivery_timeout_seconds` attribute (not a new config.toml key).
Timeout or transport failure ends that session and wakes the reader cleanup path.
Unacknowledged reliable frames retain their delivery IDs for peer deduplication.
Single-client replacement uses the existing response-handle rebinding mechanism
so pending outbox entries remain retryable while the replacement is absent.
This does not promise delivery to a permanently absent peer or exactly-once
processing by a client that does not implement ACK/deduplication.

`_on_session_writable(session)` runs only after successful write/ACK, never while
cancelling or unwinding a failed write. Socket-backed providers can extend this
hook to retry coalesced observations without a polling task.

## Paired desktop_avatar change

The matching desktop_avatar provider change:

- drops optional core events on actual queue capacity (in addition to its soft
  observation limit);
- keeps one pending latest-state flag per session and retries its current snapshot
  on writer progress, including after a full reconnect-replay queue;
- treats tool activity as ephemeral, without durable ACK/replay;
- sends tagged/checklist messages through classified reliable backpressure.

The avatar snapshot retry depends on the new socket writer hook. Update both
repositories together; do not claim the new provider fully works against an old
resident socket implementation.

## Verification and activation

Focused regression command from the Pal checkout:

```sh
PYTHONPATH=src python -m pytest -q \
  tests/test_interrupt_backpressure.py \
  tests/test_socket_backpressure_recovery.py \
  tests/test_socket_interactions.py tests/test_socket_client.py \
  tests/test_control_plane.py tests/test_channel_lifecycle.py \
  tests/test_channel_send_message.py
```

The real-Unix-socket regression uses a temporary socket, withholds an ACK, then
checks EOF, stable delivery-ID replay, pending-outbox delivery on reconnect, and
successful ACK completion. It never connects to a running Pal.

From the matching avatar checkout, point `PYTHONPATH` at the patched Pal source
and the avatar checkout, then run `python -m pytest -q tests`.

Resident Core/Channel/socket code requires an operator-controlled host restart;
a provider reload cannot replace those resident classes. Install the matching
avatar provider through its supported package/lifecycle path. Source commits and
passing sandbox tests do not establish that either live Pal or Petra has loaded
the fix. Verify loaded versions and a safe interruption after deployment.

### Local verification record (2026-09-15)

- The focused Pal command above passed all 317 tests, including eight new
  interruption/backpressure/reconnect cases.
- The avatar suite initially passed all 82 tests. Its final repeat passed 81 and
  failed `test_raster_channel_lifecycle_and_preview` waiting for the `panic`
  animation-finished event. That test serves unchanged static client files and
  mocks WebSockets; it does not run the changed provider. This is not a full-green
  final avatar run; the six new provider backpressure regressions passed.
- The Pal bootstrap suite (`tests/test_bootstrap_and_repositories.py`) passes
  with this patch: 136 passed, exit 0, 13m17s, using the native
  `_pal_shell_runtime` 0.3.0 extension on PYTHONPATH and deterministic collection
  (`-p no:randomly`). Bootstrap is slow, not hung.
- An earlier run without that extension on PYTHONPATH reported 40 import
  failures for `_pal_shell_runtime`. A clean 04b8ec9 baseline worktree showed the
  same, so this is an environment gap, not a repair defect. That run's apparent
  "deadline" was the operator tool's own timeout, not a test failure.
- The unmodified 04b8ec9 baseline passes the same suite: 136 passed, exit 0,
  13m32s, same PYTHONPATH and fixed collection order. Patch and baseline are at
  parity, so this suite shows no regression from the repair.
- One earlier scoped baseline run exited with SIGSEGV, but only under an
  artificial `timeout 35s` kill combined with a faulthandler dump. It did not
  reproduce in either full run. Root cause remains unknown; this patch does not
  claim to fix it, and it does not block the change.
- Changed Python modules compile; both diffs pass `git diff --check`. No production
  runtime was restarted, reloaded, or deployed during these tests.

The previous writer's unbounded drain/ACK wait is a verified persistence mechanism
for backpressure, now covered by recovery tests. There is still no incident-time
trace proving why Petra's peer originally stopped consuming or acknowledging.
