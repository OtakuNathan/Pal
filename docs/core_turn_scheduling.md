# Core resident turn scheduling

## Extracted implicit machine

The core message pump serializes event dispatch, but handlers used to choose
their own execution admission. Channel input checked `active_turn_id` under the
channel transition lock; proactive handlers created tasks immediately and only
then registered them. `active_turns` and `turn_tasks` consequently described
several running resident turns while `active_turn_id` described only chat.
Separately, `TurnManager.resume` removed the active pointer when its generator
returned, before asynchronous post-commit work and task teardown completed.

The October 19 failure followed this legal old execution: a chat awaited I/O,
a proactive task completed and became visible in L1, then the chat assembled
an incremental request. Previously frozen chat messages plus newly visible
proactive history split a tool call from its result. The wire validator also
accepted that non-adjacent pair. Serialized message dispatch alone does not
serialize the work started by those messages.

## Resident ownership

One scheduler owns admission of resident chat and proactive turns. The event
pump remains available while effects await I/O. An admitted turn retains its
identity through all three execution phases:

| Phase | Entry | Allowed work | Exit |
| --- | --- | --- | --- |
| idle | matching task teardown completed | admit user first, otherwise proactive; admit maintenance | reserve one turn before creating its task |
| running | reservation accepted | append the owning turn's L1 records; execute effects; admit chat interjections at existing safe points | program completion, cancellation or failure |
| finalizing | program ended or cancellation requested | finish L1 closure, post-commit work, presentation and execution cleanup | matching task completion |

I/O waits do not change phase. Task completion must identify its reservation;
a stale completion cannot release a successor. Reservation failure starts no
task and creates no L1 turn. Failure before a task is created must release its
own reservation. The synchronous reducer contains no `await`; side effects
follow an accepted transition, never precede admission.

Quiescence, dreaming and compaction are orthogonal admission barriers, not
mutually exclusive replacements for execution phase. Quiescence may begin
while a turn is running and blocks successors while it drains. Dreaming is
admitted only after resident work drains. Manual compaction requires idle;
budget-driven compaction remains an explicit safe point of its owning turn.
The existing identity-fenced compaction ticket controls that operation.
Reset and shutdown hold distinct quiescence barriers; a reset's release cannot
reopen admission while shutdown still owns its barrier.

Current-turn output and tool results must still append to L1 while running.
Foreign turns cannot begin or settle into the resident history mid-turn.
Explicit compaction remains governed by its existing snapshot/install protocol.
This change does not turn the core into a second memory owner.

## Queues and exceptions

Proactive already owns a trigger mailbox. Core backpressure leaves triggers
there until admission is possible. Events already drained into the pump are
requeued if admission was lost; the handler must recheck at actual start.
Only admitted work begins a run record, produces a reply or opens L1.
Definitions and destinations are resolved at admission, so queued work does
not retain a detached plugin's continuation. User messages take priority.

Proactive does not consume user interjections. While it owns the resident slot,
new user input waits for the next chat turn. Chat preserves its current
interjection semantics. Explicit `/btw` side turns retain their detached memory
and execution branch; their local history must never publish into the resident
L1. They remain tracked for maintenance/shutdown drain checks.

## Formal validation

`spec/core/ResidentTurnScheduling.tla` models arrivals, admission, tool I/O,
program completion, teardown, cancellation, maintenance, reset, compaction
barriers and history publication. Positive invariants cover single resident
ownership, ownership through teardown, maintenance exclusion and stable history
visibility. Negative configurations must reproduce independent proactive
execution, early-idle release and foreign history publication.
Further counterexamples cover stale completion receipts and releasing another
quiescence owner's barrier. User interjections consume queued input only at a
chat safe point; they do not create a second running turn or enter proactive.

| Negative configuration | Expected violated invariant |
| --- | --- |
| `ResidentTurnSchedulingConcurrent` | `SingleResidentTurn` |
| `ResidentTurnSchedulingEarlyIdle` | `OwnershipUntilTeardown` |
| `ResidentTurnSchedulingForeignHistory` | `StableHistoryView` |
| `ResidentTurnSchedulingStaleCompletion` | `OwnershipUntilTeardown` |
| `ResidentTurnSchedulingBarrierRelease` | `BarrierOwnership` |

Run the classified suite (positive checks and expected counterexamples) with:

```sh
python scripts/run_tlc.py --jar tla2tools.jar --group core --output test-logs/core-scheduling
```

The runner pins TLC 1.7.4 by SHA-256 and checks that the generated implementation
relation still matches `PHASE_TRANSITIONS`. The scheduling model abstracts tool
groups as pending/closed; Anthropic call/result adjacency is additionally
checked by `PreparedRequest` and the runtime regression suite, not proved by
this abstraction.

TLC exhaustively checks the configured finite request population, not arbitrary
Python execution. Runtime regression tests must exercise the corresponding
production seams, including cancellation and events already in the pump.
Fair progress additionally assumes effect cancellation/teardown and maintenance
eventually finish and queued work is eventually dispatched; unlimited user
traffic may intentionally delay proactive tasks.
The liveness configuration bounds quiescence requests as well as turn arrivals.
Without that premise, two control owners can take turns holding admission
closed forever even though each individual hold eventually releases. That is
an environment starvation trace, not a safety violation or evidence that a
permanently shutting-down core should execute queued jobs.

### Local verification, 2026-10-19

- Pinned TLC core suite: all 15 classified configurations passed. The main
  scheduling check exhausted 18,090 states; the bounded progress check exhausted
  1,188 states. Expected-negative configurations produced their named violations.
- Final focused runtime/protocol regression: 340 passed, 6 skipped, 24 subtests
  passed on Linux/Python 3.13. The skips require optional `pal_shell_native`
  imports, which are unavailable on this test interpreter's import path.
- Broader `core-b`: 1,704 passed, 6 skipped, one legacy typing fixture failed
  because it registered a task without admission. The fixture now obtains a
  lease and passes in the final focused regression with its original event
  ownership assertion intact.
- Broader `core-a`: interrupted during MCP startup after 234 passed, 1 skipped;
  this is not a completed full-CI result.
- `bunshin-a`: 1,009 passed, 36 skipped, one 20-second sandbox startup timeout;
  the unchanged failing test passed on isolated rerun.
- `bunshin-b`: 1,390 passed, 26 skipped, one 30-second read-only sandbox bootstrap
  timeout. The same test also timed out in an isolated checkout of unchanged
  HEAD `9f4b768`; its timeout was not increased to obtain a pass.

Follow-up sandbox diagnostics on the local 4-GB Raspberry Pi found that the four
broad batches overlapped. With those batches stopped, the read-only bootstrap
passed with child times of 18.45 and 17.78 seconds on the modified source and
18.54 seconds on unchanged HEAD. Sampled child stacks showed Python source
compilation and jieba initialization. The sandbox sets an empty per-run
`PYTHONPYCACHEPREFIX` while disabling bytecode writes; an experimental child-only
override that reused installed read-only bytecode reduced its time to 8.16
seconds. The following original-configuration run remained at 17.78 seconds.
This identifies substantial startup overhead and makes resource contention a
plausible explanation for the earlier timeouts, but no stack was captured from
those original failures. Sandbox production settings and test deadlines were
not changed by this diagnostic.

macOS and CI's Python 3.12 matrix were not run locally. These results establish
the checked model and regression scope, not a clean full-platform CI matrix.
No intentional resident restart or hot reload was performed for verification.
A later sequential `core-a` rerun was interrupted by an unexpected host reboot;
the remaining three batches were not rerun. The host has a one-minute hardware
watchdog, and the preceding journal ends while Ollama loads approximately
1.1 GiB of model tensors. No OOM-killer entry was found, so the reboot cause is
not established. Pal restarted automatically with the host; verification of
the resulting loaded code and live behavior remains outstanding.
