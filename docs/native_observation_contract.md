# Optional execution observation contract

Pal exposes generic observation hooks; the optional `pal-shell-native` plugin owns
sessions, remote routing, event identities and all Native shell policy. The built-in
Python shell retains its existing schema and behavior.

## Request and delivery boundaries

- `prepare_model_context(memory, continuation)` runs synchronously before prompt
  assembly. An extension may append already prepared observations at a closed tool
  batch boundary. It must not wait for external state or mutate a sent request.
- `model_response_received(continuation)` schedules independent background refresh
  after a model response and after a complete tool batch. A state revision does not
  itself authorize another model request.
- `observe_tool_delivery(call, result)` records a real tool result only after its L1
  delivery commit. Acknowledgement happens separately and cannot retract delivery.
- `stagnation_payload(call, result)` can identify semantic progress without counting
  changing clocks or receipt identities as progress. The default retains the full
  result fingerprint.

The scoped Bunshin execution wrapper forwards these hooks to its installed runtime.
A Native role's ordinary no-tool response yields before completion while watched
work remains pending. The execution owner registers its waiter atomically with the
eligibility check. Cancellation remains active while parked. Resource ownership,
completion obligations and wake eligibility remain distinct; ignored live resources
and ACK-only cleanup do not manufacture another model round.

## Atomic L1 observation commit

`append_l1_user_contexts` optionally accepts a coverage namespace, its captured
coverage, and an expected turn revision. A revision mismatch or an open tool batch
rejects the observation commit. Messages and metadata share one store replacement;
failed writes cannot claim successful delivery. Existing sidecar callers retain their
original interface and interruption semantics.

Native records state revisions, event identities and delivered output offsets in
its namespace. Source revision and visible-context coverage are independent. A new
turn may reuse visible proof; compaction can require reprojecting current state
without inventing a source revision. L1 messages already committed are immutable.

## Scope and activation

This change does not modify prompt-cache profiles, write-cost estimates or checkpoint
placement. A more stable prompt is not evidence of upstream cache reuse. No paid
model calls are required for these regressions.

Native requires the matching Pal host hooks and its own wheel/palpkg. Core changes
require a new host process; installing files is not activation. Keep existing runtime
configuration and loaded binaries intact. See the Native repository's
`docs/shell_session_lifecycle.md` for the concrete owner state machine and models.
